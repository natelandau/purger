"""Plugin for old versions of installed Ruby gems."""

import glob
import logging
import re
from pathlib import Path

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import (
    Action,
    Plugin,
    PluginSettings,
    RiskLevel,
    RunCommand,
    SizeComputedBy,
    SizeOfPaths,
    once_per_run,
    program_matching,
)
from purger.safety import invoking_home
from purger.sizing import (
    SIZE_COMMAND_TIMEOUT_SECONDS,
    disk_usage,
    expand_paths,
    printed_directory,
)

logger = logging.getLogger(__name__)

GEM_DIRECTORY_COMMAND = ("gem", "env", "gemdir")
# A `gem: --user-install` line in .gemrc would otherwise point cleanup at ~/.gem, not GEM_HOME.
GEM_CLEANUP_COMMAND = ("gem", "cleanup", "--no-user-install")
_WOULD_UNINSTALL = re.compile(r"^Dry Run Mode: Would uninstall (\S+)$", re.MULTILINE)


# The plugin declares no settings, so every caller shares this one lookup.
_NO_SETTINGS = PluginSettings(plugin="ruby", values={})


@once_per_run
def _ask_rubygems_for_gem_directory(settings: PluginSettings) -> Path:  # noqa: ARG001
    """Run `gem env gemdir` and read the folder it prints."""
    output = run_command(list(GEM_DIRECTORY_COMMAND), timeout=SIZE_COMMAND_TIMEOUT_SECONDS).stdout
    return printed_directory(output, single_line=True)


def _gem_directory() -> Path:
    """Ask RubyGems for the GEM_HOME of the active Ruby, once per purger command.

    Returns:
        Path: The active Ruby's GEM_HOME with symlinks resolved, so its files are
            measured at the real paths `gem cleanup` removes.
    """
    return _ask_rubygems_for_gem_directory(_NO_SETTINGS)


def gem_directory_size() -> int:
    """Measure the active Ruby's GEM_HOME.

    Returns:
        int: Allocated bytes, or 0 when the folder does not exist.
    """
    return disk_usage([_gem_directory()])


def gem_directory_is_owned_by_user() -> bool:
    """Report whether the active Ruby keeps its gems in a folder the user owns.

    A system Ruby, such as macOS's `/Library/Ruby/Gems`, is owned by root and
    `gem cleanup` would need sudo to change it, so it is left alone.

    Returns:
        bool: True when GEM_HOME exists and has the same owner as the invoking
            user's home directory.
    """
    try:
        gem_dir = _gem_directory()
        return gem_dir.is_dir() and gem_dir.owner() == invoking_home().owner()
    except (
        ShellCommandError,
        ValueError,
        OSError,
        KeyError,
        NotImplementedError,
        RuntimeError,
    ) as exc:
        logger.debug("skipping ruby: cannot use the gem directory: %s", exc)
        return False


def _old_version_names(output: str) -> list[str]:
    """Read the gem full names, such as `rake-13.0.6`, that a `gem cleanup` dry run lists.

    Args:
        output: Standard output of a `gem cleanup` dry run.

    Returns:
        list[str]: The full name of each gem version the cleanup would remove, once
            each, since RubyGems can list a version more than once.
    """
    return list(dict.fromkeys(_WOULD_UNINSTALL.findall(output)))


def _gem_patterns(gem_dir: Path, full_name: str) -> tuple[str, ...]:
    """List every place RubyGems stores one installed gem version, as glob-safe patterns.

    Args:
        gem_dir: The GEM_HOME holding the gem.
        full_name: The gem's name, version, and platform, as RubyGems prints it.

    Returns:
        tuple[str, ...]: Patterns for the gem's files, compiled extensions, docs, and package.
    """
    root = glob.escape(str(gem_dir))
    name = glob.escape(full_name)
    return (
        f"{root}/gems/{name}",
        f"{root}/specifications/{name}.gemspec",
        f"{root}/cache/{name}.gem",
        f"{root}/extensions/*/*/{name}",
        f"{root}/doc/{name}",
        f"{root}/build_info/{name}.info",
    )


def find_old_gem_files(settings: PluginSettings) -> list[Path]:  # noqa: ARG001
    """Find the files of every gem version `gem cleanup` would remove.

    Args:
        settings: The plugin's settings, which this plugin does not use.

    Returns:
        list[Path]: The existing files and folders of each old version.
    """
    gem_dir = _gem_directory()
    # `-n` is the dry-run flag in every RubyGems release; `--dryrun` is deprecated.
    output = run_command([*GEM_CLEANUP_COMMAND, "-n"], timeout=SIZE_COMMAND_TIMEOUT_SECONDS).stdout
    return expand_paths(
        tuple(
            pattern
            for full_name in _old_version_names(output)
            for pattern in _gem_patterns(gem_dir=gem_dir, full_name=full_name)
        )
    )


PLUGIN = Plugin(
    name="ruby",
    description="Old versions of installed Ruby gems",
    requires_on_path="gem",
    skip_unless=gem_directory_is_owned_by_user,
    # Bundler installs into the same GEM_HOME that `gem cleanup` removes from.
    skip_while_running=program_matching(r"gem|bundler?"),
    total_size=SizeComputedBy(gem_directory_size),
    actions=(
        Action(
            name="old-versions",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove every gem version older than the newest that no installed gem "
                "needs. A project whose Gemfile.lock pins an older version needs "
                "`bundle install` again."
            ),
            cleanup=RunCommand(*GEM_CLEANUP_COMMAND),
            size=SizeOfPaths(found_by=find_old_gem_files),
        ),
    ),
)
