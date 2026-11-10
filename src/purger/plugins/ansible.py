"""Plugin for Ansible's leftover temp folders and Galaxy response cache."""

import re
from pathlib import Path

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir, process_running
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

    Only the first two arguments are checked, since a shebang launch puts the
    interpreter first and the script second.

    Args:
        cmdline: The process argv.

    Returns:
        bool: True when the executable or script is an ansible command.
    """
    return any(_ANSIBLE_EXECUTABLE.fullmatch(Path(arg).name) for arg in cmdline[:2])


def _is_available() -> bool:
    """Report whether there is something to clean and no ansible run that could be using it.

    Returns:
        bool: True when a cache path has contents and no ansible process is running.
    """
    return bool(expand_paths(_TMP_PATHS + _GALAXY_PATHS)) and not process_running(
        _is_ansible_command
    )


PLUGIN = Plugin(
    name="ansible",
    description="Ansible leftover temp folders and Galaxy cache",
    is_available=_is_available,
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
