"""Decide which cleanup actions run, based on risk tier and user flags."""

from collections.abc import Callable
from dataclasses import dataclass

from nclutils.strings import human_size

from purger.plugin import Tier
from purger.reports import ActionReport, PluginReport, Status, sum_known


@dataclass(frozen=True, kw_only=True)
class CleanOptions:
    """User flags that shape a clean run.

    Attributes:
        dangerous: Allow DANGEROUS actions to be offered.
        dry_run: Plan only; never approve or run anything.
        yes: Skip the single SAFE confirmation. Never affects CAUTION or DANGEROUS.
        interactive: Whether a human can answer prompts. Defaults to False so a
            caller that forgets to set it never gets prompts it cannot answer.
    """

    dangerous: bool = False
    dry_run: bool = False
    yes: bool = False
    interactive: bool = False


@dataclass(frozen=True)
class _Entry:
    plugin_name: str
    report: ActionReport


def safe_prompt(items: list[tuple[str, ActionReport]]) -> str:
    """Build the single confirmation text for all SAFE actions.

    The prompt names tools rather than actions, so users never need to know
    which commands purger runs. A tool is marked `(sudo)` when any of its
    actions runs with sudo, so the user knows before agreeing to the batch.
    """
    sudo_tools: dict[str, bool] = {}
    for plugin, report in items:
        sudo_tools[plugin] = sudo_tools.get(plugin, False) or report.action.needs_sudo
    names = ", ".join(
        f"{plugin} (sudo)" if needs_sudo else plugin for plugin, needs_sudo in sudo_tools.items()
    )
    text = f"Run safe cleanups for {names}"
    sizes = [report.size_bytes for _, report in items]
    total = sum_known(sizes) if None not in sizes else None
    if total is not None:
        text += f" (about {human_size(total)})"
    return f"{text}?"


def risky_prompt(plugin_name: str, report: ActionReport) -> str:
    """Build the per-action confirmation text for a CAUTION or DANGEROUS action.

    The help sentence explains the cost in plain words, so it stands in for the
    action name.
    """
    action = report.action
    text = plugin_name
    if report.size_bytes is not None:
        # Before the help, which can hold several sentences the size must not attach to.
        text += f" (about {human_size(report.size_bytes)})"
    text += f": {action.help.strip().rstrip('.')}"
    if action.tier is Tier.DANGEROUS:
        text += ". This may permanently delete data"
    if action.needs_sudo:
        text += ". Requires sudo"
    return f"{text}. Continue?"


def plan_clean(
    reports: list[PluginReport],
    opts: CleanOptions,
    *,
    confirm: Callable[[str], bool],
    sudo_ready: Callable[[], bool],
) -> list[ActionReport]:
    """Decide which actions run and set a status on every other action.

    Centralizing the decision enforces one safety rule: `--yes` or a
    non-interactive run can never approve a CAUTION or DANGEROUS action.

    Args:
        reports: Measured plugin reports.
        opts: User flags.
        confirm: Ask a yes/no question. It may raise KeyboardInterrupt to stop the run.
        sudo_ready: Report whether sudo can run without a prompt.

    Returns:
        list[ActionReport]: Approved actions in plugin order; their status stays None.
    """
    candidates = _filter_candidates(reports=reports, opts=opts)

    if opts.dry_run:
        for entry in candidates:
            entry.report.status = Status.PLANNED
        return []

    approved: list[_Entry] = []
    safe = [e for e in candidates if e.report.action.tier is Tier.SAFE]
    if safe:
        approved.extend(_decide_safe(safe=safe, opts=opts, confirm=confirm, sudo_ready=sudo_ready))

    for tier in (Tier.CAUTION, Tier.DANGEROUS):
        for entry in (e for e in candidates if e.report.action.tier is tier):
            if opts.interactive and confirm(
                risky_prompt(plugin_name=entry.plugin_name, report=entry.report)
            ):
                approved.append(entry)
            else:
                entry.report.status = Status.DECLINED

    approved_ids = {id(entry) for entry in approved}
    return [entry.report for entry in candidates if id(entry) in approved_ids]


def _decide_safe(
    safe: list[_Entry],
    opts: CleanOptions,
    *,
    confirm: Callable[[str], bool],
    sudo_ready: Callable[[], bool],
) -> list[_Entry]:
    """Resolve the SAFE group and return the approved entries."""
    if opts.yes:
        granted = True
    elif opts.interactive:
        granted = confirm(safe_prompt([(e.plugin_name, e.report) for e in safe]))
    else:
        granted = False

    if not granted:
        for entry in safe:
            entry.report.status = Status.DECLINED
        return []

    if opts.interactive:
        return safe

    approved: list[_Entry] = []
    sudo_ok: bool | None = None
    for entry in safe:
        if entry.report.action.needs_sudo:
            if sudo_ok is None:
                sudo_ok = sudo_ready()
            if not sudo_ok:
                entry.report.status = Status.SKIPPED
                continue
        approved.append(entry)
    return approved


def _nothing_to_clean(action_report: ActionReport, plugin_report: PluginReport) -> bool:
    """Judge emptiness by the action's own size, falling back to the plugin footprint."""
    if action_report.size_bytes is not None:
        return action_report.size_bytes == 0
    return plugin_report.footprint_bytes == 0


def _filter_candidates(reports: list[PluginReport], opts: CleanOptions) -> list[_Entry]:
    """Mark undeliverable actions and return the ones still up for approval."""
    candidates: list[_Entry] = []
    for plugin_report in reports:
        for action_report in plugin_report.actions:
            if action_report.action.tier is Tier.DANGEROUS and not opts.dangerous:
                action_report.status = Status.DECLINED
            elif _nothing_to_clean(action_report=action_report, plugin_report=plugin_report):
                action_report.status = Status.NOTHING_TO_CLEAN
            else:
                candidates.append(
                    _Entry(plugin_name=plugin_report.plugin.name, report=action_report)
                )
    return candidates
