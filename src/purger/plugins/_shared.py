"""Helpers shared by plugins. Discovery skips this module because its name starts with `_`."""

import glob
import os
from collections.abc import Callable

import psutil


def env_dir(var: str, default: str) -> str:
    """Return a tool's folder from an environment variable, as a glob-safe pattern.

    Use it to build `paths` for tools that let users move their data folder,
    such as `CARGO_HOME` or `XDG_CACHE_HOME`.

    Args:
        var: The environment variable that overrides the folder.
        default: The folder when the variable is unset or empty, such as `~/.cargo`.

    Returns:
        str: The folder without a trailing slash, with glob characters in the
            variable's value escaped so they match literally.
    """
    value = os.environ.get(var, "").rstrip("/")
    return glob.escape(value) if value else default


def process_running(matches: Callable[[list[str]], bool]) -> bool:
    """Report whether any process's argv satisfies a check.

    Plugins use it to stay unavailable while their tool runs, since a live run
    may be using the files a cleanup would delete. Processes whose argv cannot
    be read are skipped.

    Args:
        matches: Receives a process argv and returns True for the tool.

    Returns:
        bool: True when some readable process argv matches.
    """
    return any(
        cmdline and matches(cmdline)
        for cmdline in (proc.info["cmdline"] for proc in psutil.process_iter(["cmdline"]))
    )
