"""Plugin for old versions of installed Ruby gems."""

import glob
import logging
import re
from pathlib import Path

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import program_matcher
from purger.sizing import SIZE_CMD_TIMEOUT_SECONDS, printed_dir_usage

logger = logging.getLogger(__name__)

_GEM_DIR_CMD = ("gem", "env", "gemdir")
_WOULD_UNINSTALL = re.compile(r"^Dry Run Mode: Would uninstall (\S+)$", re.MULTILINE)


def _parse_gem_dir(output: str) -> Path:
    """Read the GEM_HOME that `gem env gemdir` prints.

    Args:
        output: Standard output of `gem env gemdir`.

    Returns:
        Path: The directory `gem cleanup` removes gems from.

    Raises:
        ValueError: If the output is not exactly one absolute path.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1 or not Path(lines[0]).is_absolute():
        msg = f"expected one absolute directory from `gem env gemdir`, got {output!r}"
        raise ValueError(msg)
    return Path(lines[0])


def _gem_dir() -> Path:
    """Ask RubyGems for the GEM_HOME of the active Ruby.

    Returns:
        Path: The active Ruby's GEM_HOME.
    """
    output = run_command(list(_GEM_DIR_CMD), timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout
    return _parse_gem_dir(output)


def _is_available() -> bool:
    """Report whether the active Ruby keeps its gems in a folder the user owns.

    A system Ruby, such as macOS's `/Library/Ruby/Gems`, is owned by root and
    `gem cleanup` would need sudo to change it, so it is left alone.

    Returns:
        bool: True when GEM_HOME exists and has the same owner as the home directory.
    """
    try:
        gem_dir = _gem_dir()
        return gem_dir.is_dir() and gem_dir.owner() == Path.home().owner()
    except (ShellCommandError, ValueError, OSError, KeyError, NotImplementedError) as exc:
        logger.debug("skipping ruby: cannot use the gem directory: %s", exc)
        return False


def _old_version_names(output: str) -> list[str]:
    """Read the gem full names, such as `rake-13.0.6`, that a `gem cleanup` dry run lists.

    Args:
        output: Standard output of `gem cleanup --dryrun`.

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
    output = run_command(["gem", "cleanup", "--dryrun"], timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout
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
            command=("gem", "cleanup"),
            paths=_old_version_paths,
        ),
    ),
)
