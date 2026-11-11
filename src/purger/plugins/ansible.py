"""Plugin for Ansible's leftover temp folders and Galaxy response cache."""

import re
from pathlib import Path

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir
from purger.sizing import expand_paths


def _ansible_homes() -> tuple[str, ...]:
    """List the Ansible home folders to clean, as glob-safe patterns.

    `~/.ansible` is always included because runs that do not see `ANSIBLE_HOME`,
    such as SSH sessions where this machine is the managed host, still use it.

    Returns:
        tuple[str, ...]: Home folder patterns, without duplicates.
    """
    return tuple(dict.fromkeys(("~/.ansible", env_dir("ANSIBLE_HOME", "~/.ansible"))))


_HOMES = _ansible_homes()
_TMP_PATHS = tuple(f"{home}/tmp/*" for home in _HOMES)  # noqa: S108  # Ansible home, not /tmp
_GALAXY_PATHS = tuple(f"{home}/galaxy_cache/*" for home in _HOMES)

_ANSIBLE_EXECUTABLE = re.compile(r"ansible(-[a-z]+)?")


def _is_ansible_command(cmdline: list[str]) -> bool:
    """Report whether a process argv runs an `ansible` or `ansible-*` command.

    Only the executable and the first non-option argument after it are checked,
    since a shebang launch puts the interpreter first, then any shebang flags
    (such as `python3 -s`), then the script.

    Args:
        cmdline: The process argv.

    Returns:
        bool: True when the executable or script is an ansible command.
    """
    if not cmdline:
        return False
    script = next((arg for arg in cmdline[1:] if not arg.startswith("-")), None)
    return any(
        _ANSIBLE_EXECUTABLE.fullmatch(Path(arg).name)
        for arg in (cmdline[0], script)
        if arg is not None
    )


def _has_leftovers() -> bool:
    """Report whether there are temp folders or Galaxy responses to clean.

    Returns:
        bool: True when a cache path has contents.
    """
    return bool(expand_paths(_TMP_PATHS + _GALAXY_PATHS))


PLUGIN = Plugin(
    name="ansible",
    description="Ansible leftover temp folders and Galaxy cache",
    is_available=_has_leftovers,
    busy_process=_is_ansible_command,
    actions=(
        Action(
            name="tmp",
            tier=Tier.SAFE,
            help="Remove temp folders left by interrupted Ansible runs. Each run makes new ones.",
            paths=_TMP_PATHS,
        ),
        Action(
            name="galaxy-cache",
            tier=Tier.SAFE,
            help="Remove cached Ansible Galaxy responses. ansible-galaxy fetches them again.",
            paths=_GALAXY_PATHS,
        ),
    ),
)
