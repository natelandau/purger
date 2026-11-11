"""Plugin for tool caches and build leftovers inside the user's project folders."""

import time
from collections import Counter
from functools import partial
from pathlib import Path

from nclutils import pp

from purger.config import PluginSettings
from purger.details import tilde
from purger.plugin import Action, Detail, PathSizes, Plugin, Setting, SettingValue, Tier
from purger.plugins._projects_scan import Category, ScanOptions, project_folder, scan
from purger.safety import invoking_home, resolve_home_folder


def _entries(value: SettingValue) -> tuple[str, ...]:
    """Return a list setting's entries, or none for a value of another kind.

    The config loader rejects a value of the wrong kind before any check runs.
    """
    return value if isinstance(value, tuple) else ()


def _is_path_entry(stripped: str) -> bool:
    """Report whether an exclude entry names a path rather than a folder name."""
    return "/" in stripped or stripped.startswith("~")


def _check_no_slash(value: SettingValue) -> None:
    """Reject include entries that name more than a single folder.

    Args:
        value: The configured list of folder names.

    Raises:
        ValueError: If an entry contains a slash.
    """
    for entry in _entries(value):
        if "/" in entry:
            msg = f"entry {entry!r} must be a folder name or pattern without a slash"
            raise ValueError(msg)


def _check_exclude(value: SettingValue) -> None:
    """Reject exclude entries that are neither a folder name nor an absolute or home path.

    A relative path would resolve against the current directory, so it is refused.

    Args:
        value: The configured list of exclude entries.

    Raises:
        ValueError: If an entry is empty or is a relative path.
    """
    for entry in _entries(value):
        stripped = entry.rstrip("/")
        if not stripped or (_is_path_entry(stripped) and not stripped.startswith(("/", "~/"))):
            msg = f"entry {entry!r} must be a folder name, an absolute path, or a path starting with ~/"
            raise ValueError(msg)


def _split_exclude(
    entries: tuple[str, ...], home: Path
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Split validated exclude entries into expanded paths and folder names."""
    paths: list[Path] = []
    names: list[str] = []
    for entry in entries:
        stripped = entry.rstrip("/")
        if _is_path_entry(stripped):
            paths.append(_expand(raw=stripped, home=home))
        else:
            names.append(stripped)
    return tuple(paths), tuple(names)


def _check_at_least(minimum: int, value: SettingValue) -> None:
    """Reject a number below the minimum.

    Args:
        minimum: The smallest accepted value.
        value: The configured value.

    Raises:
        ValueError: If the value is not an integer of at least the minimum.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        msg = f"must be at least {minimum}"
        raise ValueError(msg)


def _expand(raw: str, home: Path) -> Path:
    """Expand a leading `~` against the invoking user's home."""
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def _roots(entries: tuple[str, ...], home: Path, *, warn: bool = True) -> tuple[Path, ...]:
    """Return the configured project roots that are safe to scan.

    A root must be an absolute folder strictly inside home, on home's device.
    Each bad root warns once. Roots nested under another kept root are dropped
    so overlapping roots scan once.

    Args:
        entries: The configured `paths` setting.
        home: The invoking user's home, resolved.
        warn: Warn about each bad root. Off when the scan already warned.

    Returns:
        tuple[Path, ...]: Resolved, non-nested roots.
    """
    kept: list[Path] = []
    for raw in dict.fromkeys(entries):
        root = resolve_home_folder(_expand(raw=raw, home=home), home_resolved=home)
        if isinstance(root, str):
            if warn:
                pp.warning(f"Ignoring projects path {raw}: {root}")
            continue
        if root not in kept:
            kept.append(root)
    return tuple(root for root in kept if not any(other in root.parents for other in kept))


def _prepare(settings: PluginSettings) -> dict[str, tuple[Path, ...]] | None:
    """Scan the project roots once so every action shares the result.

    Args:
        settings: The plugin's settings for this run.

    Returns:
        dict[str, tuple[Path, ...]] | None: The folders each action offers, by
            action name, or None when no usable root is set.
    """
    try:
        home = invoking_home().resolve()
    except RuntimeError:
        # Without a home folder no root can be inside it.
        return None
    roots = _roots(settings.str_list("paths"), home=home)
    if not roots:
        return None
    exclude_paths, exclude_names = _split_exclude(entries=settings.str_list("exclude"), home=home)
    options = ScanOptions(
        roots=roots,
        max_depth=settings.integer("max_depth"),
        recent_days=settings.integer("recent_days"),
        exclude_paths=exclude_paths,
        exclude_names=exclude_names,
        include=settings.str_list("include"),
        now=time.time(),
    )
    found = scan(options)
    # Each category's value is the name of the action that offers it.
    return {category.value: tuple(found.get(category, ())) for category in Category}


def _items(paths: list[Path]) -> tuple[str, ...]:
    """Name what a project holds, counting folders that repeat, such as `18 __pycache__`."""
    counts = Counter(path.name for path in paths)
    return tuple(name if n == 1 else f"{n} {name}" for name, n in sorted(counts.items()))


def _details(sizes: PathSizes, settings: PluginSettings) -> list[Detail]:
    """Group the found folders by project, so a person can see which projects a clean touches.

    With one root, labels are relative to it. With several, labels are home
    paths, so same-named projects under different roots stay apart.

    Args:
        sizes: Each found folder's measured size.
        settings: The plugin's settings for this run.

    Returns:
        list[Detail]: One row per project.
    """
    roots = _roots(settings.str_list("paths"), home=invoking_home().resolve(), warn=False)
    groups: dict[Path, list[Path]] = {}
    for path in sizes:
        root = max((r for r in roots if r in path.parents), key=lambda r: len(r.parts))
        groups.setdefault(project_folder(path, root), []).append(path)
    rows: list[Detail] = []
    for project, paths in groups.items():
        if len(roots) > 1:
            label = tilde(project)
        else:
            label = str(project.relative_to(roots[0])) if project != roots[0] else project.name
        rows.append(
            Detail(
                label=label,
                size_bytes=sum(sizes[path] for path in paths),
                items=_items(paths),
                paths=tuple(sorted(paths)),
                key=str(project),
            )
        )
    return rows


PLUGIN = Plugin(
    name="projects",
    description="Tool caches and build leftovers inside your projects",
    settings=(
        Setting(
            name="paths",
            kind="str_list",
            default=(),
            help="Folders that hold your projects. Each must be inside your home folder.",
        ),
        Setting(
            name="max_depth",
            kind="int",
            default=6,
            help="How many folder levels below each path to search.",
            check=partial(_check_at_least, 1),
        ),
        Setting(
            name="recent_days",
            kind="int",
            default=7,
            help="Leave a project alone when a file in it changed this many days ago or fewer. 0 turns the check off.",
            check=partial(_check_at_least, 0),
        ),
        Setting(
            name="exclude",
            kind="str_list",
            default=(),
            help="Folder names, patterns, or absolute paths that purger never removes or searches for leftovers. A path starts with / or ~/.",
            check=_check_exclude,
        ),
        Setting(
            name="include",
            kind="str_list",
            default=(),
            help="Extra folder names or patterns, such as *.generated, to offer for removal. No slashes.",
            check=_check_no_slash,
        ),
    ),
    prepare=_prepare,
    actions=(
        Action(
            name="caches",
            tier=Tier.SAFE,
            help="Remove tool caches in your projects, such as .mypy_cache and __pycache__. Tools rebuild them on their next run.",
            prepared=True,
            details=_details,
        ),
        Action(
            name="dependencies",
            tier=Tier.CAUTION,
            help="Remove installed dependencies, such as node_modules and .venv, from projects untouched for a while. Reinstall them before you work on a project again.",
            prepared=True,
            details=_details,
        ),
        Action(
            name="builds",
            tier=Tier.CAUTION,
            help="Remove build output, such as target and dist, from projects untouched for a while. The next build starts from scratch.",
            prepared=True,
            details=_details,
        ),
        Action(
            name="custom",
            tier=Tier.CAUTION,
            help="Remove the folders named in the include setting. purger cannot tell how costly they are to rebuild.",
            prepared=True,
            details=_details,
        ),
    ),
)
