"""Command line interface: the `info` and `clean` commands."""

import json
import os
import shlex
import sys
from collections.abc import Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from functools import partial
from typing import Annotated, Any

from cyclopts import App, CycloptsError, Parameter
from nclutils import pp
from nclutils.ask import choose_one_from_list

from purger import __version__
from purger.clean_flow import CleanOptions, plan_clean
from purger.concurrency import map_as_completed
from purger.config import FALSY_VALUES, Config, ConfigError, load_config
from purger.execution import execute, mark_unfinished, record_freed
from purger.help_text import (
    APP_HELP,
    CLEAN_EPILOGUE,
    CLEAN_HELP,
    INFO_EPILOGUE,
    INFO_HELP,
    build_epilogue,
)
from purger.measure_display import MeasureDisplay
from purger.output import (
    clean_document,
    error_document,
    info_document,
    render_results,
    render_table,
)
from purger.plugin import Plugin, Tier
from purger.registry import (
    ProcessTable,
    UnknownPluginError,
    discover_plugins,
    select_plugins,
    skip_reason,
)
from purger.reports import (
    ActionReport,
    PluginReport,
    SkippedPlugin,
    SkipReason,
    Status,
)
from purger.runner import sudo_ready
from purger.sizing import NotConfiguredError, measure_plugin


class ExitCode(IntEnum):
    """Process exit codes, part of the CLI's public contract."""

    OK = 0
    ACTION_FAILED = 1
    USAGE = 2
    INTERRUPTED = 130


_ROOT_MESSAGE = (
    "purger must not run as root. Run it as your normal user: "
    "it asks for sudo only for the actions that need it."
)

# Verbosity given before the command name (`purger -v clean`), added to the command's own.
_GLOBAL_VERBOSITY: ContextVar[int] = ContextVar("purger_global_verbosity", default=0)

app = App(
    name="purger",
    version=__version__,
    help=APP_HELP,
    help_format="markdown",
    default_parameter=Parameter(negative=(), show_default=False),
)

PluginNames = Annotated[
    str,
    Parameter(help="Plugin names to include, such as `uv brew`. Omit to include every plugin."),
]
DangerousFlag = Annotated[
    bool,
    Parameter(help="Include `dangerous` actions, which can delete data for good."),
]
JsonFlag = Annotated[
    bool,
    Parameter(
        name="--json",
        help="Print exactly one JSON document on stdout instead of tables and messages.",
    ),
]
VerboseFlag = Annotated[
    int,
    Parameter(
        name=["-v", "--verbose"],
        count=True,
        help="Show more detail, such as tool output. Repeat for more (`-vv`).",
    ),
]


@dataclass
class _Scan:
    """Measured plugins plus the plugins left out of the run."""

    reports: list[PluginReport]
    skipped: list[SkippedPlugin]


def attach_plugin_catalog() -> None:
    """Add the plugin list generated from the registry to every help page.

    Discovery runs here rather than at import, so importing the CLI and the
    root refusal never depend on plugin modules loading.
    """
    plugins = list(discover_plugins().values())
    catalog = build_epilogue(static="", plugins=plugins)
    app.help_epilogue = catalog
    app.meta.help_epilogue = catalog
    app["info"].help_epilogue = build_epilogue(static=INFO_EPILOGUE, plugins=plugins)
    app["clean"].help_epilogue = build_epilogue(static=CLEAN_EPILOGUE, plugins=plugins)


def confirm(message: str) -> bool:
    """Ask a yes/no question so a person approves each risky step.

    The prompt reports Ctrl-C as None, so a cancelled prompt is raised as an
    interrupt to stop the run rather than read as No.

    Args:
        message: The question to show.

    Returns:
        bool: True only when the user chose Yes.

    Raises:
        KeyboardInterrupt: If the user cancelled the prompt.
    """
    answer = choose_one_from_list([("Yes", "yes"), ("No", "no")], message)
    if answer is None:
        raise KeyboardInterrupt
    return answer == "yes"


def _reason_text(reason: SkipReason) -> str:
    match reason:
        case SkipReason.PLATFORM:
            return f"not supported on {sys.platform}"
        case SkipReason.MISSING_BINARY:
            return "its tool is not installed"
        case SkipReason.UNAVAILABLE:
            return "its tool is not available or is busy (for example, not running, or running now)"
        case SkipReason.DISABLED:
            return "turned off by the skip setting in the config file"
        case SkipReason.NOT_CONFIGURED:
            return "it needs settings in the config file, such as search paths"


def _emit_json(document: dict[str, Any]) -> None:
    """Write a JSON document to stdout without rich, so it is never wrapped or styled."""
    sys.stdout.write(json.dumps(document, indent=2) + "\n")


def _error(code: str, message: str, *, exit_code: ExitCode, json_output: bool) -> int:
    if json_output:
        _emit_json(error_document(code=code, message=message))
    else:
        pp.error(message)
    return exit_code


def _measure_one(
    plugin: Plugin, *, dangerous: bool, processes: ProcessTable, config: Config
) -> PluginReport | SkippedPlugin:
    reason = skip_reason(plugin, processes=processes)
    if reason is not None:
        return SkippedPlugin(name=plugin.name, reason=reason)
    try:
        return measure_plugin(
            plugin, include_dangerous=dangerous, settings=config.settings_for(plugin)
        )
    except NotConfiguredError:
        return SkippedPlugin(name=plugin.name, reason=SkipReason.NOT_CONFIGURED)


def _measure(
    plugins: Sequence[Plugin],
    *,
    dangerous: bool,
    display: MeasureDisplay | None,
    config: Config,
) -> _Scan:
    """Check and measure plugins concurrently, keeping results in plugin order.

    Plugins spend most of their time waiting on tool commands, so measuring
    them side by side shortens the wait.
    """
    results: dict[int, PluginReport | SkippedPlugin] = {}
    for index, result in map_as_completed(
        partial(_measure_one, dangerous=dangerous, processes=ProcessTable(), config=config),
        items=plugins,
    ):
        results[index] = result
        if display is not None:
            display.finish(name=plugins[index].name, result=result)
    scan = _Scan(reports=[], skipped=[])
    for _, result in sorted(results.items()):
        if isinstance(result, SkippedPlugin):
            scan.skipped.append(result)
        else:
            scan.reports.append(result)
    return scan


def _scan(names: Sequence[str], *, dangerous: bool, json_output: bool, config: Config) -> _Scan:
    """Select and measure plugins, with a live row per plugin unless output is JSON.

    Raises:
        UnknownPluginError: If a requested plugin name is not registered.
    """
    selection = select_plugins(
        registry=discover_plugins(), names=names, skip=frozenset(config.skip)
    )
    if json_output:
        scan = _measure(selection.plugins, dangerous=dangerous, display=None, config=config)
    else:
        with MeasureDisplay([plugin.name for plugin in selection.plugins]) as display:
            scan = _measure(selection.plugins, dangerous=dangerous, display=display, config=config)
    scan.skipped.extend(selection.disabled)
    return scan


def _begin(
    names: Sequence[str], *, dangerous: bool, json_output: bool, verbose: int
) -> _Scan | int:
    """Configure output and measure the selected plugins.

    Returns:
        _Scan | int: The measurements, or an exit code when the run cannot continue.
    """
    pp.configure(verbosity=verbose, quiet=json_output)
    try:
        config = load_config(discover_plugins())
    except ConfigError as exc:
        return _error(
            code="config_error",
            message=str(exc),
            exit_code=ExitCode.USAGE,
            json_output=json_output,
        )
    for warning in config.warnings:
        pp.warning(warning)
    try:
        return _scan(names, dangerous=dangerous, json_output=json_output, config=config)
    except UnknownPluginError as exc:
        return _error(
            code="unknown_plugin",
            message=str(exc),
            exit_code=ExitCode.USAGE,
            json_output=json_output,
        )
    except KeyboardInterrupt:
        return _error(
            code="interrupted",
            message="Interrupted while measuring caches. Nothing was cleaned.",
            exit_code=ExitCode.INTERRUPTED,
            json_output=json_output,
        )


def _report_skipped(skipped: list[SkippedPlugin], names: Sequence[str]) -> None:
    """Warn about plugins the user named that cannot run here."""
    requested = set(names)
    for item in skipped:
        if item.name in requested:
            pp.warning(f"{item.name} is not available on this machine: {_reason_text(item.reason)}")


def _print_table(reports: list[PluginReport]) -> None:
    if reports:
        pp.console().print(render_table(reports))
    else:
        pp.info("No supported caches found on this machine.")


def _labeled(
    reports: list[PluginReport], *, separator: str = "/"
) -> Iterable[tuple[str, ActionReport]]:
    for plugin_report in reports:
        for action_report in plugin_report.actions:
            yield (
                f"{plugin_report.plugin.name}{separator}{action_report.action.name}",
                action_report,
            )


def _warn_not_run(reports: list[PluginReport]) -> None:
    """Explain which actions a non-interactive run left alone, and why."""
    safe: list[str] = []
    risky: list[str] = []
    sudo: list[str] = []
    for label, report in _labeled(reports):
        if report.status is Status.SKIPPED:
            sudo.append(label)
        elif report.status is Status.DECLINED:
            (safe if report.action.tier is Tier.SAFE else risky).append(label)
    if safe:
        pp.warning(f"Not run because purger cannot prompt here (pass --yes): {', '.join(safe)}")
    if risky:
        pp.warning(f"Not run because they need an interactive confirmation: {', '.join(risky)}")
    if sudo:
        pp.warning(f"Not run because sudo needs a password: {', '.join(sudo)}")


def _print_plan(reports: list[PluginReport]) -> None:
    planned = [(label, r) for label, r in _labeled(reports) if r.status is Status.PLANNED]
    if not planned:
        pp.info("Nothing to clean.")
        return
    for label, report in planned:
        action = report.action
        prefix = f"{label} ({action.tier.value})"
        if action.command is not None:
            sudo = "sudo " if action.needs_sudo else ""
            pp.dryrun(f"{prefix}: would run {sudo}{shlex.join(action.command)}")
        else:
            for path in report.resolved_paths:
                pp.dryrun(f"{prefix}: would delete {path}")


def _print_results(reports: list[PluginReport], ran: list[PluginReport]) -> None:
    if not ran:
        pp.info("Nothing was cleaned.")
        return
    pp.console().print(render_results(ran))
    for label, report in _labeled(reports, separator=": "):
        if report.status is Status.FAILED:
            pp.error(f"{label} failed: {report.error}")


def _show_clean(scan: _Scan, *, json_output: bool, show: Callable[[], None]) -> None:
    """Emit the clean document for agents, or call show to tell a person the outcome."""
    if json_output:
        _emit_json(clean_document(reports=scan.reports, skipped=scan.skipped))
    else:
        show()


def _run_approved(
    scan: _Scan,
    approved: list[ActionReport],
    *,
    dangerous: bool,
    json_output: bool,
    verbose: bool,
    interactive: bool,
) -> int:
    """Run approved actions, report the outcome, and choose the exit code."""
    execution = execute(
        reports=scan.reports,
        approved=approved,
        verbose=verbose and not json_output,
        interactive=interactive,
    )
    interrupted = execution.interrupted or record_freed(
        execution.ran, dangerous=dangerous, show_progress=not json_output
    )
    _show_clean(
        scan,
        json_output=json_output,
        show=lambda: _print_results(reports=scan.reports, ran=execution.ran),
    )

    if interrupted:
        pp.warning("Interrupted. The remaining work was skipped.")
        return ExitCode.INTERRUPTED
    if any(report.status is Status.FAILED for report in approved):
        return ExitCode.ACTION_FAILED
    return ExitCode.OK


@app.meta.default
def _launcher(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    verbose: VerboseFlag = 0,
) -> int:
    """Accept -v/--verbose before the command name, then run the command."""
    token = _GLOBAL_VERBOSITY.set(verbose)
    try:
        return app(tokens)
    finally:
        _GLOBAL_VERBOSITY.reset(token)


@app.command(help=INFO_HELP)
def info(
    *plugins: PluginNames,
    dangerous: DangerousFlag = False,
    json_output: JsonFlag = False,
    verbose: VerboseFlag = 0,
) -> int:
    """Show cached data per plugin so users can decide what to clean.

    Args:
        plugins: Plugin names to include; empty means all.
        dangerous: Include DANGEROUS actions.
        json_output: Print one JSON document instead of human output.
        verbose: Verbosity level.

    Returns:
        int: The process exit code.
    """
    verbosity = verbose + _GLOBAL_VERBOSITY.get()
    scan = _begin(plugins, dangerous=dangerous, json_output=json_output, verbose=verbosity)
    if isinstance(scan, int):
        return scan

    if json_output:
        _emit_json(info_document(reports=scan.reports, skipped=scan.skipped))
    else:
        _print_table(scan.reports)
    _report_skipped(skipped=scan.skipped, names=plugins)
    return ExitCode.OK


@app.command(help=CLEAN_HELP)
def clean(
    *plugins: PluginNames,
    dangerous: DangerousFlag = False,
    dry_run: Annotated[
        bool, Parameter(help="List what would be cleaned. Asks nothing and changes nothing.")
    ] = False,
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"],
            help="Skip the single confirmation for `safe` actions. Never skips `caution` or `dangerous` prompts.",
        ),
    ] = False,
    json_output: JsonFlag = False,
    verbose: VerboseFlag = 0,
) -> int:
    """Remove caches after tiered confirmation so risky deletions always need a person.

    Args:
        plugins: Plugin names to include; empty means all.
        dangerous: Offer DANGEROUS actions.
        dry_run: Plan only.
        yes: Skip the SAFE confirmation.
        json_output: Print one JSON document instead of human output.
        verbose: Verbosity level.

    Returns:
        int: The process exit code.
    """
    verbosity = verbose + _GLOBAL_VERBOSITY.get()
    scan = _begin(plugins, dangerous=dangerous, json_output=json_output, verbose=verbosity)
    if isinstance(scan, int):
        return scan

    if not json_output:
        _print_table(scan.reports)
    _report_skipped(skipped=scan.skipped, names=plugins)

    interactive = sys.stdin.isatty() and not json_output
    opts = CleanOptions(dangerous=dangerous, dry_run=dry_run, yes=yes, interactive=interactive)
    try:
        approved = plan_clean(
            reports=scan.reports, opts=opts, confirm=confirm, sudo_ready=sudo_ready
        )
    except KeyboardInterrupt:
        mark_unfinished(report for _, report in _labeled(scan.reports))
        _show_clean(
            scan,
            json_output=json_output,
            show=lambda: pp.warning("Interrupted. Nothing was cleaned."),
        )
        return ExitCode.INTERRUPTED

    if not interactive:
        _warn_not_run(scan.reports)

    if dry_run:
        _show_clean(scan, json_output=json_output, show=lambda: _print_plan(scan.reports))
        return ExitCode.OK

    return _run_approved(
        scan,
        approved,
        dangerous=dangerous,
        json_output=json_output,
        verbose=verbosity > 0,
        interactive=interactive,
    )


def _wants_json(tokens: Sequence[str]) -> bool:
    """Report whether the command line asks for JSON output, as `--json` or `--json=<truthy>`."""
    for arg in tokens:
        if arg == "--json":
            return True
        if arg.startswith("--json=") and arg.partition("=")[2].lower() not in FALSY_VALUES:
            return True
    return False


def main() -> None:
    """Run the CLI, refusing to start as root so caches are cleaned for the real user.

    With `--json`, argument errors are printed as the JSON error document on
    stdout, so agents get a parseable answer even when the command line is wrong.
    """
    tokens = sys.argv[1:]
    json_output = _wants_json(tokens)
    if os.geteuid() == 0:
        _error(
            code="running_as_root",
            message=_ROOT_MESSAGE,
            exit_code=ExitCode.USAGE,
            json_output=json_output,
        )
        sys.exit(ExitCode.USAGE)
    attach_plugin_catalog()
    if not json_output:
        app.meta(tokens)
        return
    try:
        app.meta(tokens, exit_on_error=False, print_error=False)
    except CycloptsError as exc:
        _emit_json(error_document(code="usage", message=str(exc)))
        sys.exit(ExitCode.USAGE)
