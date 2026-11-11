"""Helpers shared by plugins. Discovery skips this module because its name starts with `_`."""

import glob
import os
import re
from collections.abc import Callable
from pathlib import Path


def env_dir(var: str, default: str) -> str:
    """Return a tool's folder from an environment variable, as a glob-safe pattern.

    Use it to build `paths` for tools that let users move their data folder,
    such as `CARGO_HOME` or `XDG_CACHE_HOME`.

    Args:
        var: The environment variable that overrides the folder.
        default: The folder when the variable is unset or empty, such as `~/.cargo`.

    Returns:
        str: The folder without a trailing slash, with glob characters in the
            variable's value escaped so they match literally. A relative value
            counts as unset, since it would resolve against the working directory.
    """
    value = os.environ.get(var, "").rstrip("/")
    return glob.escape(value) if value.startswith(("/", "~")) else default


def program_matcher(
    pattern: str, *, ignore_subcommands: frozenset[str] = frozenset()
) -> Callable[[list[str]], bool]:
    """Build a `busy_process` check that matches processes running a named program.

    Only the executable and the first non-option argument after it are checked,
    since an interpreter launch (`node /usr/local/bin/npm`, `python3 -m pip`,
    `python3 -s /usr/bin/ansible-playbook`) puts the program after the
    interpreter and its flags.

    Args:
        pattern: A regex that must match the whole file name of the program.
        ignore_subcommands: Subcommands that leave the tool's files alone, such
            as `exec` for npm, so a long-running one does not block cleanup.

    Returns:
        Callable[[list[str]], bool]: A check that receives a process argv.
    """
    program = re.compile(pattern)

    def runs_program(cmdline: list[str]) -> bool:
        words = _argv_words(cmdline)
        if not words:
            return False
        program_at = [0, *[i for i, arg in enumerate(words) if i and not arg.startswith("-")][:1]]
        for index in program_at:
            if program.fullmatch(Path(words[index]).name):
                subcommand = next((a for a in words[index + 1 :] if not a.startswith("-")), None)
                return subcommand not in ignore_subcommands
        return False

    return runs_program


def _argv_words(cmdline: list[str]) -> list[str]:
    """Return a process argv, splitting one that a rewritten process title joined.

    A program that sets its title, as npm does, leaves the whole command line
    in the first argument, followed only by empty ones.
    """
    if cmdline and not any(cmdline[1:]):
        return cmdline[0].split()
    return cmdline
