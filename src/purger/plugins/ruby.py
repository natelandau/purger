"""Plugin for old versions of installed Ruby gems."""

import glob
import logging
import re
from functools import cache
from pathlib import Path

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import program_matcher
from purger.safety import invoking_home
from purger.sizing import SIZE_CMD_TIMEOUT_SECONDS, printed_dir, printed_dir_usage

logger = logging.getLogger(__name__)

_GEM_DIR_CMD = ("gem", "env", "gemdir")
# A `gem: --user-install` line in .gemrc would otherwise point cleanup at ~/.gem, not GEM_HOME.
_CLEANUP_CMD = ("gem", "cleanup", "--no-user-install")
_WOULD_UNINSTALL = re.compile(r"^Dry Run Mode: Would uninstall (\S+)$", re.MULTILINE)


@cache
def _gem_dir() -> Path:
    """Ask RubyGems for the GEM_HOME of the active Ruby, once per run.

    Returns:
        Path: The active Ruby's GEM_HOME with symlinks resolved, so its files are
            measured at the real paths `gem cleanup` removes.
    """
    output = run_command(list(_GEM_DIR_CMD), timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout
    return printed_dir(output, single_line=True)


def _is_available() -> bool:
    """Report whether the active Ruby keeps its gems in a folder the user owns.

    A system Ruby, such as macOS's `/Library/Ruby/Gems`, is owned by root and
    `gem cleanup` would need sudo to change it, so it is left alone.

    Returns:
        bool: True when GEM_HOME exists and has the same owner as the invoking
            user's home directory.
    """
    try:
        gem_dir = _gem_dir()
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


def _old_version_paths() -> tuple[str, ...]:
    """List the files of every gem version `gem cleanup` would remove.

    Returns:
        tuple[str, ...]: Glob-safe patterns for each old version's files.
    """
    gem_dir = _gem_dir()
    # `-n` is the dry-run flag in every RubyGems release; `--dryrun` is deprecated.
    output = run_command([*_CLEANUP_CMD, "-n"], timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout
    return tuple(
        pattern
        for full_name in _old_version_names(output)
        for pattern in _gem_patterns(gem_dir=gem_dir, full_name=full_name)
    )


PLUGIN = Plugin(
    name="ruby",
    description="Old versions of installed Ruby gems",
    requires="gem",
    is_available=_is_available,
    # Bundler installs into the same GEM_HOME that `gem cleanup` removes from.
    busy_process=program_matcher(r"gem|bundler?"),
    size_cmd=_GEM_DIR_CMD,
    parse_size=printed_dir_usage,
    actions=(
        Action(
            name="old-versions",
            tier=Tier.CAUTION,
            help=(
                "Remove every gem version older than the newest that no installed gem "
                "needs. A project whose Gemfile.lock pins an older version needs "
                "`bundle install` again."
            ),
            command=_CLEANUP_CMD,
            paths=_old_version_paths,
        ),
    ),
)
