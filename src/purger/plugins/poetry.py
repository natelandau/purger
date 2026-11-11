"""Plugin for the Poetry package cache and the virtualenvs Poetry manages."""

from purger.plugin import (
    Action,
    DeletePaths,
    Plugin,
    RiskLevel,
    directory_from_environment,
    user_cache_folders,
)
from purger.sizing import any_path_exists


def _cache_roots() -> tuple[str, ...]:
    """List the folders Poetry may keep its cache in, as glob-safe patterns.

    `POETRY_CACHE_DIR` overrides the platform default, and on Linux the default
    follows `XDG_CACHE_HOME`. The defaults are kept alongside an override so a
    cache written before it was set still gets cleaned.

    Returns:
        tuple[str, ...]: Cache folders, without duplicates.
    """
    defaults = user_cache_folders("pypoetry")
    return tuple(
        dict.fromkeys(
            (directory_from_environment("POETRY_CACHE_DIR", default=defaults[0]), *defaults)
        )
    )


def poetry_cache_globs() -> tuple[str, ...]:
    """List the downloaded package data to delete, as glob-safe patterns.

    Covers the repository caches and the downloaded wheels and sdists in
    `artifacts`. Deleted by path so a cache left behind after Poetry itself is
    gone still gets cleaned.

    Returns:
        tuple[str, ...]: Cache content patterns, without duplicates.
    """
    return tuple(f"{root}/{part}/*" for root in _cache_roots() for part in ("cache", "artifacts"))


def poetry_virtualenv_globs() -> tuple[str, ...]:
    """List the virtualenvs Poetry created, as glob-safe patterns.

    `POETRY_VIRTUALENVS_PATH` overrides the default `virtualenvs` folder inside
    the cache folder. A path set only in Poetry's config file is not found.

    Returns:
        tuple[str, ...]: Virtualenv patterns, without duplicates.
    """
    roots = _cache_roots()
    override = directory_from_environment(
        "POETRY_VIRTUALENVS_PATH", default=f"{roots[0]}/virtualenvs"
    )
    return tuple(dict.fromkeys((f"{override}/*", *(f"{root}/virtualenvs/*" for root in roots))))


POETRY_CACHE_GLOBS = poetry_cache_globs()
POETRY_VIRTUALENV_GLOBS = poetry_virtualenv_globs()


def poetry_left_data() -> bool:
    """Report whether Poetry left a cache or virtualenvs, since the plugin does not need poetry installed.

    Returns:
        bool: True when any cache or virtualenv path has contents.
    """
    return any_path_exists(POETRY_CACHE_GLOBS + POETRY_VIRTUALENV_GLOBS)


PLUGIN = Plugin(
    name="poetry",
    description="Poetry package cache and virtualenvs",
    skip_unless=poetry_left_data,
    actions=(
        Action(
            name="cache",
            risk_level=RiskLevel.CAUTION,
            description="Wipe the Poetry package cache. Packages download again on next install.",
            cleanup=DeletePaths(*POETRY_CACHE_GLOBS),
        ),
        Action(
            name="virtualenvs",
            risk_level=RiskLevel.CAUTION,
            description="Remove the virtualenvs Poetry created. Each project needs `poetry install` again.",
            cleanup=DeletePaths(*POETRY_VIRTUALENV_GLOBS),
        ),
    ),
)
