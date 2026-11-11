"""Helpers shared by plugins. Discovery skips this module because its name starts with `_`."""

import glob
import os


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
