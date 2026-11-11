"""Find build leftovers inside project folders that are safe to offer for deletion.

The leading underscore keeps plugin discovery from importing this module as a plugin.
Every result is only a candidate: deletion happens elsewhere, through `safety.delete_path`.
"""

import logging
import math
import os
import stat
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from functools import cache
from itertools import accumulate
from pathlib import Path
from typing import Literal

from nclutils.git.repo import is_git_installed
from nclutils.git.runner import run_git
from nclutils.sh import ShellCommandError

from purger.sizing import GLOB_CHARS

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400
_GIT_TIMEOUT_SECONDS = 30
_GIT_DIR = ".git"


class Category(StrEnum):
    """The kind of leftover a folder holds, one per projects action."""

    CACHES = "caches"
    DEPENDENCIES = "dependencies"
    BUILDS = "builds"
    CUSTOM = "custom"


@dataclass(frozen=True, kw_only=True)
class Rule:
    """Describe which directory names count as leftovers and what must hold for them.

    Attributes:
        pattern: fnmatch pattern for the directory name, case-sensitive.
        category: The action that owns matches.
        siblings: Any one of these must exist next to the directory. fnmatch patterns allowed.
        inside: All of these must exist inside the directory.
        git_only: Target the directory only inside a git project.
    """

    pattern: str
    category: Category
    siblings: tuple[str, ...] = ()
    inside: tuple[str, ...] = ()
    git_only: bool = False


_BUILD_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "package.json",
    "CMakeLists.txt",
    "build.gradle",
    "build.gradle.kts",
    "pubspec.yaml",
)


def _plain(category: Category, *patterns: str) -> tuple[Rule, ...]:
    """Build rules that match on the name alone."""
    return tuple(Rule(pattern=pattern, category=category) for pattern in patterns)


BUILTIN_RULES: tuple[Rule, ...] = (
    *_plain(
        Category.CACHES,
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".hypothesis",
        ".pytype",
        ".pyre",
        ".parcel-cache",
        ".turbo",
        ".zig-cache",
        ".sass-cache",
        ".angular",
        ".nx",
        ".docusaurus",
        ".elixir_ls",
        ".nyc_output",
    ),
    *_plain(Category.DEPENDENCIES, "node_modules"),
    Rule(pattern=".venv", category=Category.DEPENDENCIES, inside=("pyvenv.cfg",)),
    Rule(pattern="venv", category=Category.DEPENDENCIES, inside=("pyvenv.cfg",)),
    *_plain(Category.DEPENDENCIES, ".tox", ".nox"),
    Rule(pattern=".dart_tool", category=Category.DEPENDENCIES, siblings=("pubspec.yaml",)),
    Rule(pattern=".terraform", category=Category.DEPENDENCIES, siblings=("*.tf",)),
    *_plain(Category.DEPENDENCIES, "__pypackages__", ".direnv", ".devenv"),
    Rule(pattern="target", category=Category.BUILDS, siblings=("Cargo.toml",), git_only=True),
    Rule(pattern="build", category=Category.BUILDS, siblings=_BUILD_MARKERS, git_only=True),
    Rule(pattern="dist", category=Category.BUILDS, siblings=_BUILD_MARKERS, git_only=True),
    *_plain(Category.BUILDS, ".next", ".nuxt", ".svelte-kit"),
    Rule(pattern=".build", category=Category.BUILDS, siblings=("Package.swift",)),
    *_plain(Category.BUILDS, "*.egg-info"),
    Rule(pattern="_build", category=Category.BUILDS, siblings=("mix.exs",)),
    *_plain(Category.BUILDS, ".stack-work", "dist-newstyle"),
    Rule(pattern="zig-out", category=Category.BUILDS, siblings=("build.zig",)),
    Rule(pattern=".output", category=Category.BUILDS, siblings=("package.json",)),
    *_plain(Category.BUILDS, ".gradle", ".kotlin", ".eggs", "htmlcov"),
)


@dataclass(frozen=True, kw_only=True)
class ScanOptions:
    """Everything a scan needs, so callers never share state with it.

    Attributes:
        roots: Resolved folders to scan. A root's children are depth 1.
        max_depth: Levels below each root the walk visits.
        recent_days: Skip projects with a file changed this recently. Zero turns the check off.
        exclude_paths: Folders the walk never enters.
        exclude_names: fnmatch patterns for folder names that are never targeted.
        include: fnmatch patterns for extra folder names, reported as custom.
        now: The current time in epoch seconds, so results are reproducible.
    """

    roots: tuple[Path, ...]
    max_depth: int
    recent_days: int
    exclude_paths: tuple[Path, ...]
    exclude_names: tuple[str, ...]
    include: tuple[str, ...]
    now: float


@dataclass(frozen=True)
class _Candidate:
    """A directory that matched a rule, before the project checks.

    `siblings` holds every name in the parent as the walk listed it.
    """

    path: Path
    rule: Rule
    siblings: tuple[str, ...]


def _is_glob(pattern: str) -> bool:
    """Return True when fnmatch would treat any character of the pattern as special."""
    return not GLOB_CHARS.isdisjoint(pattern)


@dataclass(frozen=True)
class _Patterns:
    """fnmatch patterns split so literal names match by set lookup."""

    literals: frozenset[str]
    globs: tuple[str, ...]

    def match(self, name: str) -> bool:
        """Return True when the name matches any pattern, case-sensitively."""
        return name in self.literals or any(fnmatchcase(name, pattern) for pattern in self.globs)


@cache
def _compile(patterns: tuple[str, ...]) -> _Patterns:
    """Split patterns once, since rules and settings reuse the same tuples all scan long."""
    return _Patterns(
        literals=frozenset(pattern for pattern in patterns if not _is_glob(pattern)),
        globs=tuple(pattern for pattern in patterns if _is_glob(pattern)),
    )


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    """Return True when the name matches any fnmatch pattern, case-sensitively."""
    return _compile(patterns).match(name)


@dataclass(frozen=True)
class _Rules:
    """Rules indexed for lookup, keeping each rule's position for first-match order."""

    literal: Mapping[str, tuple[int, Rule]]
    globbed: tuple[tuple[int, Rule], ...]

    @classmethod
    def build(cls, rules: Sequence[Rule]) -> "_Rules":
        """Index literal patterns by name and keep glob patterns in order."""
        literal: dict[str, tuple[int, Rule]] = {}
        globbed: list[tuple[int, Rule]] = []
        for index, rule in enumerate(rules):
            if _is_glob(rule.pattern):
                globbed.append((index, rule))
            else:
                literal.setdefault(rule.pattern, (index, rule))
        return cls(literal=literal, globbed=tuple(globbed))

    def first(self, name: str) -> Rule | None:
        """Return the first rule whose pattern matches the name.

        The first match owns the name even when its checks later fail, so an `include`
        entry can never override a built-in marker check.
        """
        hit = self.literal.get(name)
        for index, rule in self.globbed:
            if hit is not None and index > hit[0]:
                break
            if fnmatchcase(name, rule.pattern):
                return rule
        return None if hit is None else hit[1]


def _lstat(path: Path) -> os.stat_result | Literal[False] | None:
    """Return the path's lstat, False when it does not exist, or None when it cannot be read."""
    try:
        return path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        logger.debug("cannot stat %s: %s", path, exc)
        return None


def _exists(path: Path) -> bool | None:
    """Return whether the path exists, or None when that cannot be determined."""
    info = _lstat(path)
    return None if info is None else info is not False


def _probe_git(directory: Path) -> bool | None:
    """Return whether the directory holds `.git`, or None when that cannot be determined."""
    return _exists(directory / _GIT_DIR)


def _probe_repo(directory: Path) -> bool | None:
    """Return whether the directory is a git repository root, or None when that cannot be determined.

    Mirrors git's own detection: `.git` is a file (worktree or submodule) or a
    directory holding `HEAD`. A `.git` directory without `HEAD` belongs to some
    other tool and does not make a project.
    """
    git_entry = directory / _GIT_DIR
    info = _lstat(git_entry)
    if info is None or info is False:
        return info
    if not stat.S_ISDIR(info.st_mode):
        return True
    return _exists(git_entry / "HEAD")


def _resolve(path: Path) -> Path | None:
    """Return the resolved path, or None when it cannot be resolved."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        logger.debug("cannot resolve %s: %s", path, exc)
        return None


def _inside_ok(candidate: Path, names: tuple[str, ...]) -> bool:
    """Return True when every required entry exists inside the directory."""
    try:
        for name in names:
            (candidate / name).stat()
    except OSError:
        return False
    return True


def _qualifies(child: Path, rule: Rule, siblings: tuple[str, ...]) -> bool:
    """Return True when a directory passes its rule's sibling and inside checks.

    Args:
        child: The directory whose name matched the rule.
        rule: The first rule matching the directory's name.
        siblings: Every name in the directory's parent.
    """
    return (
        not rule.siblings or any(_matches(name, patterns=rule.siblings) for name in siblings)
    ) and _inside_ok(child, names=rule.inside)


def _is_same_device_non_link(info: os.stat_result, device: int) -> bool:
    """Return True when an lstat result is not a symlink and lives on the device."""
    return not stat.S_ISLNK(info.st_mode) and info.st_dev == device


def _log_unreadable(exc: OSError) -> None:
    """Log a directory a walk cannot list."""
    logger.debug("cannot read directory: %s", exc)


@dataclass(frozen=True)
class _Excluded:
    """Excluded folders, matched by resolved path and by inode.

    Matching by inode catches a folder spelled through a symlink or with different
    letter case on a case-insensitive filesystem.
    """

    paths: tuple[Path, ...]
    identities: frozenset[tuple[int, int]]

    @classmethod
    def build(cls, paths: tuple[Path, ...]) -> "_Excluded":
        """Resolve each path and record the identity of those that exist."""
        resolved = tuple(_resolve(path) or path.absolute() for path in paths)
        identities: set[tuple[int, int]] = set()
        for path in resolved:
            try:
                info = path.stat()
            except OSError:
                continue
            identities.add((info.st_dev, info.st_ino))
        return cls(paths=resolved, identities=frozenset(identities))

    def covers(self, path: Path, info: os.stat_result | None = None) -> bool:
        """Return True when the path is an excluded folder or inside one.

        Args:
            path: The path to check.
            info: The path's lstat result when the caller already has it.
        """
        if any(path.is_relative_to(excluded) for excluded in self.paths):
            return True
        if not self.identities:
            return False
        if info is None:
            try:
                info = path.lstat()
            except OSError:
                return True
        return (info.st_dev, info.st_ino) in self.identities


@dataclass(frozen=True)
class _Context:
    """The settings and caches every step of one scan shares."""

    options: ScanOptions
    rules: _Rules
    excluded: _Excluded
    repo_probes: dict[Path, bool | None] = field(default_factory=dict)

    @classmethod
    def build(cls, options: ScanOptions) -> "_Context":
        """Combine the built-in rules with the user's includes and resolve the exclusions."""
        rules = (
            *BUILTIN_RULES,
            *(Rule(pattern=pattern, category=Category.CUSTOM) for pattern in options.include),
        )
        return cls(
            options=options,
            rules=_Rules.build(rules),
            excluded=_Excluded.build(options.exclude_paths),
        )

    def probe_repo(self, directory: Path) -> bool | None:
        """Return `_probe_repo` for the directory, probing each directory once per scan."""
        if directory not in self.repo_probes:
            self.repo_probes[directory] = _probe_repo(directory)
        return self.repo_probes[directory]


def _walk_root(root: Path, context: _Context) -> list[_Candidate]:
    """Walk one root, pruning as it goes, and return the directories that qualify."""
    try:
        root_device = root.stat().st_dev
    except OSError as exc:
        logger.debug("cannot read root %s: %s", root, exc)
        return []
    if any(context.excluded.covers(path) for path in (root, *root.parents)):
        return []

    root_depth = len(root.parts)
    candidates: list[_Candidate] = []
    for dirpath, dirnames, filenames in root.walk(
        top_down=True, follow_symlinks=False, on_error=_log_unreadable
    ):
        siblings = (*dirnames, *filenames)
        keep: list[str] = []
        for name in dirnames:
            if name == _GIT_DIR:
                continue
            child = dirpath / name
            info = _lstat(child)
            if (
                not isinstance(info, os.stat_result)
                or context.excluded.covers(child, info=info)
                or not _is_same_device_non_link(info, device=root_device)
                or _matches(name, patterns=context.options.exclude_names)
            ):
                continue
            rule = context.rules.first(name)
            if rule is None:
                keep.append(name)
            elif _qualifies(child, rule=rule, siblings=siblings):
                candidates.append(_Candidate(path=child, rule=rule, siblings=siblings))
        # A root's children are depth 1, so children at the limit are matched but never listed.
        at_limit = len(dirpath.parts) - root_depth + 1 >= context.options.max_depth
        dirnames[:] = [] if at_limit else keep
    return candidates


def _find_project(
    candidate: Path, root: Path, context: _Context
) -> tuple[Path | None, Path] | None:
    """Return the candidate's git root, if any, and the project folder whose activity counts.

    A repository above the scan root owns the candidate for the git guard, so every
    ancestor is searched. Activity is measured in the repository when it sits at or
    below the root, and otherwise in the root's child holding the candidate (or the
    root itself), so unrelated sibling projects cannot protect each other. Return
    None when an ancestor cannot be probed, because the candidate might belong to a
    repository.
    """
    local = _root_child(candidate, root)
    for ancestor in candidate.parents:
        has_git = context.probe_repo(ancestor)
        if has_git is None:
            return None
        if has_git:
            return ancestor, (local if ancestor in root.parents else ancestor)
    return None, local


def project_folder(path: Path, root: Path) -> Path:
    """Return the project a found folder belongs to, so detail rows can group by project.

    Mirrors `_find_project`: a repository at or below the root wins, and
    otherwise the root's child holding the path, or the root itself.

    Args:
        path: A folder the scan found, below `root`.
        root: The scan root that holds it.

    Returns:
        Path: The project folder.
    """
    for ancestor in path.parents:
        if ancestor != root and root not in ancestor.parents:
            break
        if (ancestor / ".git").exists():
            return ancestor
    return _root_child(path, root)


def _root_child(path: Path, root: Path) -> Path:
    """Return the root's child that holds the path, or the root for a direct child."""
    relative = path.relative_to(root).parts
    return root / relative[0] if len(relative) > 1 else root


def _mtime(path: Path) -> float:
    """Return the path's mtime, or negative infinity when it does not exist.

    Raises:
        OSError: If the path exists but cannot be read.
    """
    try:
        return path.lstat().st_mtime
    except FileNotFoundError:
        return -math.inf


def _worktree_git_dir(git_file: Path) -> Path | None:
    """Return the git directory a worktree's `.git` file points to, or None if unreadable.

    Raises:
        OSError: If the file cannot be read.
    """
    content = git_file.read_text(encoding="utf-8", errors="replace").strip()
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    target = Path(content.removeprefix(prefix).strip())
    return target if target.is_absolute() else git_file.parent / target


def _git_is_recent(project: Path, cutoff: float) -> bool:
    """Return True when the project's git index, HEAD, or worktree `.git` file is recent.

    A worktree keeps its index under the main repository, so its `.git` file is
    followed there; staging in a worktree then counts as activity too. Anything
    that cannot be read counts as recent.
    """
    git_entry = project / _GIT_DIR
    try:
        if git_entry.is_file():
            if git_entry.lstat().st_mtime >= cutoff:
                return True
            git_dir = _worktree_git_dir(git_entry)
            if git_dir is None:
                return True
        else:
            git_dir = git_entry
        return any(_mtime(git_dir / name) >= cutoff for name in ("index", "HEAD"))
    except OSError:
        return True


# A directory, the device its search stays on, and the candidates below it to skip.
type _Subtree = tuple[Path, int, frozenset[Path]]


@dataclass
class _Activity:
    """Answer whether projects changed recently, searching each subtree once per scan.

    A subtree's answer depends only on its directory, device, and the candidates
    skipped below it, so projects that nest share the answers for common subtrees.
    """

    cutoff: float
    known: dict[_Subtree, bool] = field(default_factory=dict)

    def is_recent(self, project: Path, candidates: AbstractSet[Path]) -> bool:
        """Return True when anything in the project changed since the cutoff, or cannot be read.

        Only the candidates themselves are skipped. Every other folder counts, including
        pattern matches that failed their checks, because they may hold the user's own work.
        """
        if _git_is_recent(project, cutoff=self.cutoff):
            return True
        try:
            device = project.stat().st_dev
        except OSError:
            return True
        return self._subtree_recent((project, device, frozenset(candidates)))

    def _subtree_recent(self, top: _Subtree) -> bool:
        """Search a subtree depth first, recording every finished directory's answer.

        Any recent or unreadable entry makes every directory above it recent, so the
        search stops at the first one.
        """
        if top in self.known:
            return self.known[top]
        children = self._visit(top)
        if children is None:
            self.known[top] = True
            return True
        stack: list[tuple[_Subtree, Iterator[_Subtree]]] = [(top, iter(children))]
        while stack:
            subtree, pending = stack[-1]
            child = next(pending, None)
            if child is None:
                self.known[subtree] = False
                stack.pop()
                continue
            answer = self.known.get(child)
            if answer is None:
                grandchildren = self._visit(child)
                if grandchildren is not None:
                    stack.append((child, iter(grandchildren)))
                    continue
                answer = self.known[child] = True
            if answer:
                self.known.update((open_subtree, True) for open_subtree, _ in stack)
                return True
        return False

    def _visit(self, subtree: _Subtree) -> list[_Subtree] | None:
        """Check one directory's own entries and return the subdirectories to search.

        Returns:
            list[_Subtree] | None: The subdirectories, or None when an entry is recent
                or anything cannot be read.
        """
        directory, device, skip = subtree
        listing = next(
            directory.walk(top_down=True, follow_symlinks=False, on_error=_log_unreadable), None
        )
        if listing is None:
            return None
        _, dirnames, filenames = listing
        for name in filenames:
            info = _lstat(directory / name)
            if not isinstance(info, os.stat_result) or info.st_mtime >= self.cutoff:
                return None
        children: list[_Subtree] = []
        for name in dirnames:
            child = directory / name
            if name == _GIT_DIR or child in skip:
                continue
            info = _lstat(child)
            if not isinstance(info, os.stat_result):
                return None
            if _is_same_device_non_link(info, device=device):
                below = frozenset(path for path in skip if child in path.parents)
                children.append((child, device, below))
        return children


def _fold(path: str) -> str:
    """Normalize a path so spellings that differ only in Unicode form or case compare equal."""
    if path.isascii():
        return path.casefold()
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", path).casefold())


@dataclass(frozen=True)
class _Tracked:
    """What git tracks in one repository, as folded paths relative to its root."""

    files: frozenset[str]
    paths: frozenset[str]


def _list_tracked(repo: Path) -> _Tracked | None:
    """Return the repository's tracked files, or None when git cannot answer.

    Paths are folded for Unicode form and case, because the index may spell a
    folder differently than the filesystem does.
    """
    if not is_git_installed():
        return None
    try:
        result = run_git(
            "ls-files",
            "-z",
            cwd=repo,
            # GIT_DIR and friends would point git at a different repository.
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (ShellCommandError, UnicodeDecodeError) as exc:
        logger.debug("git ls-files failed in %s: %s", repo, exc)
        return None
    if not result.ok:
        return None
    files = frozenset(_fold(entry) for entry in result.stdout.split("\0") if entry)
    # Every tracked file and each of its parent directories.
    tracked: set[str] = set()
    for entry in files:
        tracked.update(accumulate(entry.split("/"), lambda head, part: f"{head}/{part}"))
    return _Tracked(files=files, paths=frozenset(tracked))


def _marker_tracked(candidate: _Candidate, repo: Path, tracked: _Tracked) -> bool:
    """Return True when a sibling marker that satisfies the rule is tracked by the repository.

    A repository can sit far above the project, such as a home folder kept in git,
    and only a tracked marker proves the project itself belongs to it.
    """
    parent = candidate.path.parent.relative_to(repo)
    return any(
        _fold((parent / name).as_posix()) in tracked.files
        for name in candidate.siblings
        if _matches(name, patterns=candidate.rule.siblings)
    )


def _guarded(
    repo: Path, candidates: dict[Path, _Candidate], listings: dict[Path, _Tracked | None]
) -> list[Path]:
    """Return the candidates git allows, or none when git cannot answer.

    A candidate is dropped when it holds a tracked file, and a `git_only` candidate
    also needs a tracked sibling marker. `listings` caches each repository's tracked
    files for the rest of the scan.
    """
    if repo not in listings:
        listings[repo] = _list_tracked(repo)
    tracked = listings[repo]
    if tracked is None:
        return []
    return [
        path
        for path, candidate in candidates.items()
        if _fold(path.relative_to(repo).as_posix()) not in tracked.paths
        and (not candidate.rule.git_only or _marker_tracked(candidate, repo=repo, tracked=tracked))
    ]


def scan(options: ScanOptions) -> dict[Category, list[Path]]:
    """Find leftover folders in every root that are idle and untracked.

    Run this to learn what is safe to offer for deletion. A folder is returned only if
    its project has seen no file change within `recent_days` and git tracks nothing in
    it. A `git_only` folder also needs a git-tracked marker beside it. Any doubt, such
    as an unreadable file or a failing git, drops the folder.

    Args:
        options: The roots, limits, and filters for this scan.

    Returns:
        Every category mapped to its sorted, deduplicated folders.
    """
    context = _Context.build(options)
    activity = _Activity(cutoff=options.now - options.recent_days * _SECONDS_PER_DAY)

    rejected: set[Path] = set()
    # (git root, activity project) -> candidates
    projects: dict[tuple[Path | None, Path], dict[Path, _Candidate]] = {}
    for root in options.roots:
        for candidate in _walk_root(root, context=context):
            found_project = _find_project(candidate.path, root=root, context=context)
            if found_project is None:
                rejected.add(candidate.path)
                continue
            projects.setdefault(found_project, {})[candidate.path] = candidate

    # Overlapping roots can reach one folder through different projects, and any
    # project that rejects it wins.
    accepted: dict[Path, Category] = {}
    listings: dict[Path, _Tracked | None] = {}
    for (git_root, project), found in projects.items():
        survivors = {
            path: candidate
            for path, candidate in found.items()
            if (git_root is not None or not candidate.rule.git_only) and _probe_git(path) is False
        }
        rejected.update(found.keys() - survivors.keys())
        if not survivors:
            continue
        if options.recent_days > 0 and activity.is_recent(project, candidates=survivors.keys()):
            rejected.update(survivors)
            continue
        kept = (
            list(survivors)
            if git_root is None
            else _guarded(git_root, candidates=survivors, listings=listings)
        )
        rejected.update(survivors.keys() - set(kept))
        accepted.update({path: survivors[path].rule.category for path in kept})

    result: dict[Category, list[Path]] = {category: [] for category in Category}
    for path, category in sorted(accepted.items()):
        if path not in rejected:
            result[category].append(path)
    return result
