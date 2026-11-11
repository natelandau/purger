"""Plugin for the Poetry package cache and the virtualenvs Poetry manages."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir
from purger.sizing import expand_paths


def _cache_roots() -> tuple[str, ...]:
    """List the folders Poetry may keep its cache in, as glob-safe patterns.

    `POETRY_CACHE_DIR` overrides the platform default, and on Linux the default
    follows `XDG_CACHE_HOME`. The defaults are kept alongside an override so a
    cache written before it was set still gets cleaned.

    Returns:
        tuple[str, ...]: Cache folders, without duplicates.
    """
    xdg_cache = env_dir("XDG_CACHE_HOME", "~/.cache")
    defaults = ("~/Library/Caches/pypoetry", "~/.cache/pypoetry", f"{xdg_cache}/pypoetry")
    return tuple(dict.fromkeys((env_dir("POETRY_CACHE_DIR", defaults[0]), *defaults)))


def _cache_patterns() -> tuple[str, ...]:
    """List the downloaded package data to delete, as glob-safe patterns.

    Covers the repository caches and the downloaded wheels and sdists in
    `artifacts`. Deleted by path so a cache left behind after Poetry itself is
    gone still gets cleaned.

    Returns:
        tuple[str, ...]: Cache content patterns, without duplicates.
    """
    return tuple(f"{root}/{part}/*" for root in _cache_roots() for part in ("cache", "artifacts"))


def _virtualenv_patterns() -> tuple[str, ...]:
    """List the virtualenvs Poetry created, as glob-safe patterns.

    `POETRY_VIRTUALENVS_PATH` overrides the default `virtualenvs` folder inside
    the cache folder. A path set only in Poetry's config file is not found.

    Returns:
        tuple[str, ...]: Virtualenv patterns, without duplicates.
    """
    roots = _cache_roots()
    override = env_dir("POETRY_VIRTUALENVS_PATH", f"{roots[0]}/virtualenvs")
    return tuple(dict.fromkeys((f"{override}/*", *(f"{root}/virtualenvs/*" for root in roots))))


_CACHE_PATHS = _cache_patterns()
_VIRTUALENV_PATHS = _virtualenv_patterns()


def _has_data() -> bool:
    """Report whether Poetry left a cache or virtualenvs, since the plugin does not need poetry installed.

    Returns:
        bool: True when any cache or virtualenv path has contents.
    """
    return bool(expand_paths(_CACHE_PATHS + _VIRTUALENV_PATHS))


PLUGIN = Plugin(
    name="poetry",
    description="Poetry package cache and virtualenvs",
    is_available=_has_data,
    actions=(
        Action(
            name="cache",
            tier=Tier.CAUTION,
            help="Wipe the Poetry package cache. Packages download again on next install.",
            paths=_CACHE_PATHS,
        ),
        Action(
            name="virtualenvs",
            tier=Tier.CAUTION,
            help="Remove the virtualenvs Poetry created. Each project needs `poetry install` again.",
            paths=_VIRTUALENV_PATHS,
        ),
    ),
)
