"""Plugin for tool caches and build leftovers inside the user's project folders."""

import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from nclutils import pp

from purger.details import tilde
from purger.plugin import (
    Action,
    DeletePaths,
    PathSizes,
    Plugin,
    PluginSettings,
    RiskLevel,
    Setting,
    SettingValue,
    SizeBreakdownRow,
    at_least,
    once_per_run,
)
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


def reject_entries_with_a_slash(value: SettingValue) -> None:
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


def reject_relative_exclude_entries(value: SettingValue) -> None:
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


def has_usable_project_folder(settings: PluginSettings) -> bool:
    """Report whether at least one configured project folder can be scanned.

    Warns once about each rejected folder, since this check runs once per command.

    Args:
        settings: The plugin's settings for this run.

    Returns:
        bool: False when no home folder is known or every configured folder is rejected.
    """
    try:
        home = invoking_home().resolve()
    except RuntimeError:
        return False
    return bool(_roots(settings.get_string_list("paths"), home=home))


@once_per_run
def project_folders(settings: PluginSettings) -> Mapping[Category, list[Path]]:
    """Scan the project roots once per run, so every action shares the result.

    Args:
        settings: The plugin's settings for this run.

    Returns:
        Mapping[Category, list[Path]]: The folders found in each category.
    """
    home = invoking_home().resolve()
    exclude_paths, exclude_names = _split_exclude(
        entries=settings.get_string_list("exclude"), home=home
    )
    options = ScanOptions(
        # The configuration check already warned about each rejected path.
        roots=_roots(settings.get_string_list("paths"), home=home, warn=False),
        max_depth=settings.get_integer("max_depth"),
        recent_days=settings.get_integer("recent_days"),
        exclude_paths=exclude_paths,
        exclude_names=exclude_names,
        include=settings.get_string_list("include"),
        now=time.time(),
    )
    return scan(options)


def project_caches(settings: PluginSettings) -> tuple[Path, ...]:
    """Return the tool caches the scan found, such as `.mypy_cache`."""
    return tuple(project_folders(settings).get(Category.CACHES, ()))


def project_dependencies(settings: PluginSettings) -> tuple[Path, ...]:
    """Return the installed dependencies the scan found, such as `node_modules`."""
    return tuple(project_folders(settings).get(Category.DEPENDENCIES, ()))


def project_builds(settings: PluginSettings) -> tuple[Path, ...]:
    """Return the build output the scan found, such as `target`."""
    return tuple(project_folders(settings).get(Category.BUILDS, ()))


def project_custom_folders(settings: PluginSettings) -> tuple[Path, ...]:
    """Return the folders the scan found that the `include` setting names."""
    return tuple(project_folders(settings).get(Category.CUSTOM, ()))


def _items(paths: list[Path]) -> tuple[str, ...]:
    """Name what a project holds, counting folders that repeat, such as `18 __pycache__`."""
    counts = Counter(path.name for path in paths)
    return tuple(name if n == 1 else f"{n} {name}" for name, n in sorted(counts.items()))


def one_row_per_project(sizes: PathSizes, settings: PluginSettings) -> list[SizeBreakdownRow]:
    """Group the found folders by project, so a person can see which projects a clean touches.

    With one root, labels are relative to it. With several, labels are home
    paths, so same-named projects under different roots stay apart.

    Args:
        sizes: Each found folder's measured size.
        settings: The plugin's settings for this run.

    Returns:
        list[SizeBreakdownRow]: One row per project.
    """
    roots = _roots(settings.get_string_list("paths"), home=invoking_home().resolve(), warn=False)
    groups: dict[Path, list[Path]] = {}
    for path in sizes:
        root = max((r for r in roots if r in path.parents), key=lambda r: len(r.parts))
        groups.setdefault(project_folder(path, root), []).append(path)
    rows: list[SizeBreakdownRow] = []
    for project, paths in groups.items():
        if len(roots) > 1:
            label = tilde(project)
        else:
            label = str(project.relative_to(roots[0])) if project != roots[0] else project.name
        rows.append(
            SizeBreakdownRow(
                label=label,
                size_bytes=sum(sizes[path] for path in paths),
                item_names=_items(paths),
                paths=tuple(sorted(paths)),
                merge_key=str(project),
            )
        )
    return rows


PLUGIN = Plugin(
    name="projects",
    description="Tool caches and build leftovers inside your projects",
    skip_unless_configured=has_usable_project_folder,
    settings=(
        Setting(
            name="paths",
            value_type=list[str],
            default=[],
            description="Folders that hold your projects. Each must be inside your home folder.",
            required=True,
        ),
        Setting(
            name="max_depth",
            value_type=int,
            default=6,
            description="How many folder levels below each path to search.",
            validate=at_least(1),
        ),
        Setting(
            name="recent_days",
            value_type=int,
            default=7,
            description="Leave a project alone when a file in it changed this many days ago or fewer. 0 turns the check off.",
            validate=at_least(0),
        ),
        Setting(
            name="exclude",
            value_type=list[str],
            default=[],
            description="Folder names, patterns, or absolute paths that purger never removes or searches for leftovers. A path starts with / or ~/.",
            validate=reject_relative_exclude_entries,
        ),
        Setting(
            name="include",
            value_type=list[str],
            default=[],
            description="Extra folder names or patterns, such as *.generated, to offer for removal. No slashes.",
            validate=reject_entries_with_a_slash,
        ),
    ),
    actions=(
        Action(
            name="caches",
            risk_level=RiskLevel.SAFE,
            description="Remove tool caches in your projects, such as .mypy_cache and __pycache__. Tools rebuild them on their next run.",
            cleanup=DeletePaths(found_by=project_caches),
            size_breakdown=one_row_per_project,
        ),
        Action(
            name="dependencies",
            risk_level=RiskLevel.CAUTION,
            description="Remove installed dependencies, such as node_modules and .venv, from projects untouched for a while. Reinstall them before you work on a project again.",
            cleanup=DeletePaths(found_by=project_dependencies),
            size_breakdown=one_row_per_project,
        ),
        Action(
            name="builds",
            risk_level=RiskLevel.CAUTION,
            description="Remove build output, such as target and dist, from projects untouched for a while. The next build starts from scratch.",
            cleanup=DeletePaths(found_by=project_builds),
            size_breakdown=one_row_per_project,
        ),
        Action(
            name="custom",
            risk_level=RiskLevel.CAUTION,
            description="Remove the folders named in the include setting. purger cannot tell how costly they are to rebuild.",
            cleanup=DeletePaths(found_by=project_custom_folders),
            size_breakdown=one_row_per_project,
        ),
    ),
)
