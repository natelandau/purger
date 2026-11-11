"""Plugin for the Yarn global package caches."""

from purger.plugin import (
    Action,
    DeletePaths,
    Plugin,
    RiskLevel,
    program_matching,
    user_cache_folders,
)
from purger.sizing import any_path_exists


def yarn_cache_globs() -> tuple[str, ...]:
    """List the Yarn cache contents to delete, as glob-safe patterns.

    Covers the Yarn 1 cache on macOS and Linux, where it follows `XDG_CACHE_HOME`,
    then the Yarn 2+ global mirror. Deleted by path so the plugin works without
    running yarn, which a corepack shim may try to download.

    Returns:
        tuple[str, ...]: Cache content patterns, without duplicates.
    """
    yarn_classic_caches = (
        f"{folder}/*" for folder in user_cache_folders("yarn", macos_folder_name="Yarn")
    )
    return (*yarn_classic_caches, "~/.yarn/berry/cache/*")


YARN_CACHE_GLOBS = yarn_cache_globs()


def yarn_cache_exists() -> bool:
    """Report whether a Yarn cache exists, since the plugin does not need yarn installed.

    Returns:
        bool: True when any cache path has contents.
    """
    return any_path_exists(YARN_CACHE_GLOBS)


PLUGIN = Plugin(
    name="yarn",
    description="Yarn global package cache",
    skip_unless=yarn_cache_exists,
    # Package scripts such as `yarn run dev` can run for hours and leave the cache alone.
    skip_while_running=program_matching(
        r"yarn(\.c?js|-\d[\w.-]*\.cjs)?",
        ignoring_subcommands=frozenset({"run", "exec", "node"}),
    ),
    actions=(
        Action(
            name="cache",
            risk_level=RiskLevel.CAUTION,
            description="Wipe the global Yarn cache. Packages download again on next install.",
            cleanup=DeletePaths(*YARN_CACHE_GLOBS),
        ),
    ),
)
