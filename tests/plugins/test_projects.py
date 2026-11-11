"""Tests for the project scanner."""

import os
import time
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nclutils.sh import ShellCommandError
from pytest_mock import MockerFixture

from purger import safety
from purger.config import Config, ConfigError, PluginSettings, load_config
from purger.plugin import SettingValue, Tier
from purger.plugins import _projects_scan as scanner
from purger.plugins import projects
from purger.plugins._projects_scan import BUILTIN_RULES, Category, ScanOptions, scan
from purger.plugins.projects import PLUGIN
from purger.sizing import NotConfiguredError, measure_footprint, measure_plugin

NOW = 1_800_000_000.0
DAY = 86400
GIT_LIST = ["git", "ls-files", "-z"]
OLD = NOW - 30 * DAY
FRESH = NOW - 60


class GitMock:
    """Stand-in for `run_git` and `is_git_installed` that records git calls."""

    def __init__(self) -> None:
        self.stdout = ""
        self.returncode = 0
        self.raises = False
        self.calls: list[tuple[list[str], Path]] = []
        self.envs: list[dict[str, str]] = []
        self.decode_error = False

    def run(self, *args: str, **kwargs: object) -> SimpleNamespace:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        self.calls.append((["git", *args], cwd))
        env = kwargs["env"]
        assert isinstance(env, dict)
        self.envs.append(env)
        if self.decode_error:
            b"\xff".decode("utf-8")
        if self.raises:
            msg = "git failed"
            raise ShellCommandError(msg)
        return SimpleNamespace(ok=self.returncode == 0, stdout=self.stdout)


@pytest.fixture
def git(mocker: MockerFixture) -> GitMock:
    """Mock git as installed and listing no tracked files."""
    mock = GitMock()
    mocker.patch.object(scanner, "run_git", side_effect=mock.run)
    mocker.patch.object(scanner, "is_git_installed", return_value=True)
    return mock


def age(path: Path, mtime: float = OLD) -> None:
    """Set the mtime of every file and directory under path, deepest first."""
    for dirpath, dirnames, filenames in path.walk(top_down=False):
        for name in [*filenames, *dirnames]:
            os.utime(dirpath / name, (mtime, mtime), follow_symlinks=False)
    os.utime(path, (mtime, mtime))


def touch(path: Path, text: str = "x") -> Path:
    """Create a file with its parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_repo(path: Path) -> None:
    """Create the markers git uses to recognize a repository root."""
    touch(path / ".git" / "HEAD", "ref: refs/heads/main")


def options(root: Path, **overrides: object) -> ScanOptions:
    """Build scan options with defaults that keep tests short."""
    fields: dict[str, object] = {
        "roots": (root,),
        "max_depth": 6,
        "recent_days": 7,
        "exclude_paths": (),
        "exclude_names": (),
        "include": (),
        "now": NOW,
    }
    fields.update(overrides)
    return ScanOptions(**fields)  # type: ignore[arg-type]


def empty() -> dict[Category, list[Path]]:
    """Return the result of a scan that found nothing."""
    return {category: [] for category in Category}


def found(**by_category: list[Path]) -> dict[Category, list[Path]]:
    """Return an expected result with the named categories filled in."""
    result = empty()
    for name, paths in by_category.items():
        result[Category(name)] = sorted(paths)
    return result


def test_builtin_rules_follow_the_action_table() -> None:
    """Verify the built-in rules list every pattern once, in table order."""
    # Given / When
    patterns = [rule.pattern for rule in BUILTIN_RULES]

    # Then
    assert len(patterns) == len(set(patterns)) == 16 + 10 + 17
    assert patterns[0] == ".mypy_cache"
    assert patterns[16] == "node_modules"
    assert patterns[26] == "target"
    assert [rule.category for rule in BUILTIN_RULES[:16]] == [Category.CACHES] * 16
    assert [rule.category for rule in BUILTIN_RULES[16:26]] == [Category.DEPENDENCIES] * 10
    assert [rule.category for rule in BUILTIN_RULES[26:]] == [Category.BUILDS] * 17


def test_finds_python_caches_in_old_project(tmp_path: Path, git: GitMock) -> None:
    """Verify caches are found in a project that has been idle past the age gate."""
    # Given
    touch(tmp_path / "proj" / "main.py")
    touch(tmp_path / "proj" / ".mypy_cache" / "a.json")
    touch(tmp_path / "proj" / "src" / "pkg" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(
        caches=[
            tmp_path / "proj" / ".mypy_cache",
            tmp_path / "proj" / "src" / "pkg" / "__pycache__",
        ]
    )
    assert git.calls == []


def test_never_descends_into_matched_directory(
    tmp_path: Path, git: GitMock, mocker: MockerFixture
) -> None:
    """Verify nothing below a matched directory is listed, and only the outer folder is targeted."""
    # Given
    touch(tmp_path / "proj" / ".cache" / ".mypy_cache" / "a.json")
    age(tmp_path)
    listed: list[Path] = []
    real_walk = Path.walk

    def spy(self: Path, *args: Any, **kwargs: Any) -> Iterator[tuple[Path, list[str], list[str]]]:
        for dirpath, dirnames, filenames in real_walk(self, *args, **kwargs):
            listed.append(dirpath)
            yield dirpath, dirnames, filenames

    mocker.patch.object(Path, "walk", spy)

    # When
    result = scan(options(tmp_path, include=(".cache",)))

    # Then
    assert result == found(custom=[tmp_path / "proj" / ".cache"])
    assert not [p for p in listed if (tmp_path / "proj" / ".cache") in (p, *p.parents)]


def test_excluded_name_hides_nested_matches(tmp_path: Path, git: GitMock) -> None:
    """Verify an excluded name is neither targeted nor searched."""
    # Given
    touch(tmp_path / "proj" / ".venv" / "pyvenv.cfg")
    touch(tmp_path / "proj" / ".venv" / "lib" / "site-packages" / "x" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path, exclude_names=(".venv",)))

    # Then
    assert result == empty()


def test_exclude_path_is_never_entered(tmp_path: Path, git: GitMock) -> None:
    """Verify an excluded path and everything under it is skipped."""
    # Given
    touch(tmp_path / "keep" / "__pycache__" / "m.pyc")
    touch(tmp_path / "skip" / "__pycache__" / "m.pyc")
    touch(tmp_path / "skip" / "deep" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path, exclude_paths=(tmp_path / "skip",)))

    # Then
    assert result == found(caches=[tmp_path / "keep" / "__pycache__"])


def test_respects_max_depth(tmp_path: Path, git: GitMock) -> None:
    """Verify a cache at max_depth is found and one below it is not."""
    # Given
    touch(tmp_path / "a" / "b" / "__pycache__" / "m.pyc")
    touch(tmp_path / "a" / "b" / "c" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path, max_depth=3))

    # Then
    assert result == found(caches=[tmp_path / "a" / "b" / "__pycache__"])


def test_recent_file_beyond_max_depth_protects_project(tmp_path: Path, git: GitMock) -> None:
    """Verify activity deeper than max_depth still protects the project."""
    # Given
    touch(tmp_path / "proj" / ".mypy_cache" / "a.json")
    deep = touch(tmp_path / "proj" / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "f.txt")
    age(tmp_path)
    os.utime(deep, (FRESH, FRESH))

    # When
    result = scan(options(tmp_path, max_depth=2))

    # Then
    assert result == empty()


def test_recent_days_zero_turns_age_gate_off(tmp_path: Path, git: GitMock) -> None:
    """Verify recent_days of zero lets a freshly edited project through."""
    # Given
    touch(tmp_path / "proj" / ".mypy_cache" / "a.json")
    touch(tmp_path / "proj" / "main.py")
    age(tmp_path, FRESH)

    # When
    gated = scan(options(tmp_path))
    ungated = scan(options(tmp_path, recent_days=0))

    # Then
    assert gated == empty()
    assert ungated == found(caches=[tmp_path / "proj" / ".mypy_cache"])


def test_files_inside_matched_directories_do_not_count_as_activity(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify fresh files inside a cache do not keep its project alive."""
    # Given
    touch(tmp_path / "proj" / "main.py")
    touch(tmp_path / "proj" / "__pycache__" / "m.pyc")
    age(tmp_path)
    os.utime(tmp_path / "proj" / "__pycache__" / "m.pyc", (FRESH, FRESH))

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(caches=[tmp_path / "proj" / "__pycache__"])


@pytest.mark.parametrize(
    ("candidate", "markers", "category", "expected"),
    [
        (".venv", [], "dependencies", False),
        (".venv", [".venv/pyvenv.cfg"], "dependencies", True),
        ("venv", [], "dependencies", False),
        ("venv", ["venv/pyvenv.cfg"], "dependencies", True),
        ("target", [], "builds", False),
        ("target", ["Cargo.toml"], "builds", True),
        (".terraform", [], "dependencies", False),
        (".terraform", ["main.tf"], "dependencies", True),
        ("zig-out", [], "builds", False),
        ("zig-out", ["build.zig"], "builds", True),
        ("build", [], "builds", False),
        ("build", ["build.gradle.kts"], "builds", True),
        (".dart_tool", ["pubspec.yaml"], "dependencies", True),
        ("_build", ["mix.exs"], "builds", True),
        (".output", ["package.json"], "builds", True),
        (".build", ["Package.swift"], "builds", True),
        ("Build", ["build.gradle"], "builds", False),
    ],
)
def test_marker_checks(
    tmp_path: Path,
    git: GitMock,
    candidate: str,
    markers: list[str],
    category: str,
    expected: bool,
) -> None:
    """Verify generic names are targeted only next to or around their markers."""
    # Given a git project
    project = tmp_path / "proj"
    make_repo(project)
    touch(project / candidate / "file")
    for marker in markers:
        touch(project / marker)
    age(tmp_path)
    git.stdout = "".join(f"{marker}\0" for marker in markers if "/" not in marker)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == (found(**{category: [project / candidate]}) if expected else empty())


def test_build_dist_target_only_in_git_projects(tmp_path: Path, git: GitMock) -> None:
    """Verify build, dist, and target are ignored outside a git project."""
    # Given
    touch(tmp_path / "plain" / "package.json")
    touch(tmp_path / "plain" / "build" / "o.js")
    touch(tmp_path / "plain" / "dist" / "o.js")
    touch(tmp_path / "plain" / "Cargo.toml")
    touch(tmp_path / "plain" / "target" / "o")
    make_repo(tmp_path / "repo")
    touch(tmp_path / "repo" / "package.json")
    touch(tmp_path / "repo" / "dist" / "o.js")
    age(tmp_path)
    git.stdout = "package.json\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(builds=[tmp_path / "repo" / "dist"])
    assert git.calls[0][0] == GIT_LIST
    assert git.calls[0][1] == tmp_path / "repo"


def test_git_tracked_candidate_is_dropped(tmp_path: Path, git: GitMock) -> None:
    """Verify a candidate holding tracked files is dropped and a sibling survives."""
    # Given
    project = tmp_path / "repo"
    make_repo(project)
    touch(project / "package.json")
    touch(project / "build" / "keep.txt")
    touch(project / "dist" / "o.js")
    touch(project / "build2" / "x")
    age(tmp_path)
    git.stdout = "package.json\0build/keep.txt\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(builds=[project / "dist"])
    assert git.calls[0][0] == GIT_LIST


def test_git_prefix_match_needs_a_path_boundary(tmp_path: Path, git: GitMock) -> None:
    """Verify tracked `build-notes/a` does not protect `build`."""
    # Given
    project = tmp_path / "repo"
    make_repo(project)
    touch(project / "package.json")
    touch(project / "build" / "o.js")
    age(tmp_path)
    git.stdout = "package.json\0build-notes/a\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(builds=[project / "build"])


@pytest.mark.parametrize("failure", ["nonzero", "error", "missing"])
def test_git_failure_drops_project_candidates(
    tmp_path: Path, git: GitMock, mocker: MockerFixture, failure: str
) -> None:
    """Verify any git failure drops every candidate in that project only."""
    # Given a git project and a plain project
    project = tmp_path / "repo"
    make_repo(project)
    touch(project / "__pycache__" / "m.pyc")
    touch(tmp_path / "plain" / "__pycache__" / "m.pyc")
    age(tmp_path)
    if failure == "nonzero":
        git.returncode = 128
    elif failure == "error":
        git.raises = True
    else:
        mocker.patch.object(scanner, "is_git_installed", return_value=False)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(caches=[tmp_path / "plain" / "__pycache__"])


def test_candidate_holding_git_dir_is_dropped(tmp_path: Path, git: GitMock) -> None:
    """Verify a matched folder that is itself a repository is never offered."""
    # Given a node_modules that holds a repository, and a plain cache beside it
    project = tmp_path / "proj"
    make_repo(project)
    touch(project / "node_modules" / ".git" / "HEAD")
    touch(project / ".pytest_cache" / "x")
    age(tmp_path)

    # When
    result = scan(options(tmp_path))

    # Then only the git check can have dropped node_modules
    assert result == found(caches=[project / ".pytest_cache"])
    assert git.calls[0][0] == GIT_LIST


def test_symlinked_directories_are_not_followed(tmp_path: Path, git: GitMock) -> None:
    """Verify a symlink is neither entered nor targeted."""
    # Given
    outside = tmp_path / "outside"
    touch(outside / "__pycache__" / "m.pyc")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "__pycache__").symlink_to(outside / "__pycache__", target_is_directory=True)
    age(tmp_path)

    # When
    result = scan(options(root))

    # Then
    assert result == empty()


def test_include_matching_builtin_keeps_builtin_category(tmp_path: Path, git: GitMock) -> None:
    """Verify an included name that is already built in stays in its own category."""
    # Given
    touch(tmp_path / "proj" / "node_modules" / "pkg" / "index.js")
    touch(tmp_path / "proj" / "scratch" / "x")
    age(tmp_path)

    # When
    result = scan(options(tmp_path, include=("node_modules", "scratch")))

    # Then
    assert result == found(
        dependencies=[tmp_path / "proj" / "node_modules"], custom=[tmp_path / "proj" / "scratch"]
    )


def test_include_does_not_bypass_builtin_marker_check(tmp_path: Path, git: GitMock) -> None:
    """Verify including a built-in name never targets a folder that failed its marker check."""
    # Given
    touch(tmp_path / "proj" / "venv" / "notes.txt")
    age(tmp_path)

    # When
    result = scan(options(tmp_path, include=("venv",)))

    # Then
    assert result == empty()


def test_worktree_git_file_marks_project(tmp_path: Path, git: GitMock) -> None:
    """Verify a `.git` file makes a git project and its mtime counts as activity."""
    # Given
    project = tmp_path / "wt"
    touch(project / ".git", "gitdir: /elsewhere")
    touch(project / "package.json")
    touch(project / "dist" / "o.js")
    age(tmp_path)
    git.stdout = "package.json\0"

    # When the git file is old
    old = scan(options(tmp_path))
    os.utime(project / ".git", (FRESH, FRESH))
    recent = scan(options(tmp_path))

    # Then
    assert old == found(builds=[project / "dist"])
    assert git.calls[0][1] == project
    assert recent == empty()


def test_worktree_index_activity_protects_project(tmp_path: Path, git: GitMock) -> None:
    """Verify staging in a worktree counts as activity through its `gitdir` index."""
    # Given
    project = tmp_path / "wt"
    git_dir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    touch(git_dir / "index")
    touch(git_dir / "HEAD")
    touch(project / ".git", "gitdir: ../main/.git/worktrees/wt")
    touch(project / "__pycache__" / "m.pyc")
    age(tmp_path)
    git.stdout = ""

    # When the worktree's index is old, then freshly written
    old = scan(options(tmp_path))
    os.utime(git_dir / "index", (FRESH, FRESH))
    recent = scan(options(tmp_path))

    # Then
    assert old == found(caches=[project / "__pycache__"])
    assert recent == empty()


def test_unparsable_worktree_git_file_counts_as_recent(tmp_path: Path, git: GitMock) -> None:
    """Verify a `.git` file without a `gitdir:` line protects its project."""
    # Given
    project = tmp_path / "wt"
    touch(project / ".git", "not a pointer")
    touch(project / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()


def test_git_index_activity_protects_project(tmp_path: Path, git: GitMock) -> None:
    """Verify a recent commit or checkout protects an otherwise idle project."""
    # Given
    project = tmp_path / "repo"
    touch(project / ".git" / "index")
    touch(project / ".git" / "HEAD")
    touch(project / "__pycache__" / "m.pyc")
    age(tmp_path)
    os.utime(project / ".git" / "HEAD", (FRESH, FRESH))

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()


def test_unreadable_directory_is_skipped(tmp_path: Path, git: GitMock) -> None:
    """Verify an unreadable folder does not stop the scan of the others."""
    # Given
    touch(tmp_path / "good" / "__pycache__" / "m.pyc")
    locked = tmp_path / "locked"
    touch(locked / "__pycache__" / "m.pyc")
    age(tmp_path)
    locked.chmod(0)

    try:
        # When
        result = scan(options(tmp_path))
    finally:
        locked.chmod(0o755)

    # Then
    assert result == found(caches=[tmp_path / "good" / "__pycache__"])


def test_unreadable_file_activity_counts_as_recent(
    tmp_path: Path, git: GitMock, mocker: MockerFixture
) -> None:
    """Verify an error while measuring activity protects the project."""
    # Given
    touch(tmp_path / "proj" / "main.py")
    touch(tmp_path / "proj" / "__pycache__" / "m.pyc")
    age(tmp_path)
    real_lstat = Path.lstat

    def failing(self: Path) -> os.stat_result:
        if self.name == "main.py":
            raise PermissionError(self)
        return real_lstat(self)

    mocker.patch.object(Path, "lstat", failing)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()


def test_results_are_sorted_deduplicated_and_cover_every_category(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify two roots that overlap report each folder once, sorted."""
    # Given
    touch(tmp_path / "b" / "__pycache__" / "m.pyc")
    touch(tmp_path / "a" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path, roots=(tmp_path, tmp_path / "a")))

    # Then
    assert list(result) == list(Category)
    assert result[Category.CACHES] == [
        tmp_path / "a" / "__pycache__",
        tmp_path / "b" / "__pycache__",
    ]


def test_scan_never_runs_real_git(tmp_path: Path) -> None:
    """Verify a plain project needs no git at all."""
    # Given
    touch(tmp_path / "proj" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result[Category.CACHES] == [tmp_path / "proj" / "__pycache__"]


def test_non_git_project_is_the_root_child_so_any_activity_protects_it(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify a fresh file anywhere in a non-git project protects its caches."""
    # Given
    touch(tmp_path / "proj" / "src" / "pkg" / "__pycache__" / "m.pyc")
    touch(tmp_path / "proj" / "docs" / "index.md")
    touch(tmp_path / "other" / "__pycache__" / "m.pyc")
    age(tmp_path)
    os.utime(tmp_path / "proj" / "docs" / "index.md", (FRESH, FRESH))

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(caches=[tmp_path / "other" / "__pycache__"])


def test_root_inside_a_git_repository_still_gets_the_git_guard(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify a repository above the scan root guards candidates below it."""
    # Given
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / "sub" / "node_modules" / "tracked.js")
    touch(repo / "sub" / "__pycache__" / "m.pyc")
    age(tmp_path)
    git.stdout = "sub/node_modules/tracked.js\0"

    # When
    result = scan(options(repo / "sub"))

    # Then
    assert result == found(caches=[repo / "sub" / "__pycache__"])
    assert git.calls[0][1] == repo
    assert git.calls[0][0] == GIT_LIST


def test_overlapping_roots_cannot_bypass_the_git_guard(tmp_path: Path, git: GitMock) -> None:
    """Verify a folder reached through two roots is dropped when git tracks it."""
    # Given
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / "sub" / "node_modules" / "tracked.js")
    age(tmp_path)
    git.stdout = "sub/node_modules/tracked.js\0"

    # When
    result = scan(options(tmp_path, roots=(tmp_path, repo / "sub")))

    # Then
    assert result == empty()


def test_overlapping_roots_use_the_most_protective_project(tmp_path: Path, git: GitMock) -> None:
    """Verify activity in a wider project protects a folder a narrower root also reaches."""
    # Given
    touch(tmp_path / "a" / "x" / "__pycache__" / "m.pyc")
    touch(tmp_path / "a" / "y" / "fresh.txt")
    age(tmp_path)
    os.utime(tmp_path / "a" / "y" / "fresh.txt", (FRESH, FRESH))

    # When
    result = scan(options(tmp_path, roots=(tmp_path, tmp_path / "a")))

    # Then
    assert result == empty()


def test_exclude_path_through_a_symlink_is_never_entered(tmp_path: Path, git: GitMock) -> None:
    """Verify excluding a folder by a symlink spelling still keeps the walk out."""
    # Given
    root = tmp_path / "root"
    touch(root / "skip" / "__pycache__" / "m.pyc")
    touch(root / "keep" / "__pycache__" / "m.pyc")
    link = tmp_path / "link"
    link.symlink_to(root / "skip", target_is_directory=True)
    age(tmp_path)

    # When
    result = scan(options(root, exclude_paths=(link,)))

    # Then
    assert result == found(caches=[root / "keep" / "__pycache__"])


def test_exclude_path_with_different_case_is_never_entered(tmp_path: Path, git: GitMock) -> None:
    """Verify an exclude spelled in another letter case matches on a case-insensitive filesystem."""
    # Given
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("filesystem is case-sensitive")
    root = tmp_path / "root"
    touch(root / "skip" / "__pycache__" / "m.pyc")
    touch(root / "keep" / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    result = scan(options(root, exclude_paths=(root / "SKIP",)))

    # Then
    assert result == found(caches=[root / "keep" / "__pycache__"])


def test_git_guard_lists_every_tracked_file(tmp_path: Path, git: GitMock) -> None:
    """Verify the guard lists all tracked files, so a folder with an odd name is still matched."""
    # Given
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / ":stuff" / "node_modules" / "a.js")
    age(tmp_path)
    git.stdout = ":stuff/node_modules/a.js\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()
    assert git.calls[0][0] == GIT_LIST


def test_git_guard_drops_candidates_on_undecodable_output(tmp_path: Path, git: GitMock) -> None:
    """Verify output git cannot be decoded drops the project's candidates."""
    # Given
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / "__pycache__" / "m.pyc")
    age(tmp_path)
    git.decode_error = True

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()


def test_git_guard_ignores_inherited_git_variables(
    tmp_path: Path, git: GitMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GIT_DIR and its siblings never reach the guard's git process."""
    # Given
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/elsewhere")
    monkeypatch.setenv("PURGER_KEEP", "1")
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / "__pycache__" / "m.pyc")
    age(tmp_path)

    # When
    scan(options(tmp_path))

    # Then
    assert git.envs
    assert not [key for key in git.envs[0] if key.startswith("GIT_")]
    assert git.envs[0]["PURGER_KEEP"] == "1"


def test_git_index_spelled_in_another_case_still_drops_the_candidate(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify a tracked path whose case differs from the folder on disk protects it."""
    # Given
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / "package.json")
    touch(repo / "build" / "x")
    touch(repo / "dist" / "o.js")
    age(tmp_path)
    git.stdout = "package.json\0Build/x\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(builds=[repo / "dist"])


def test_unsearchable_directory_does_not_abort_the_scan(tmp_path: Path, git: GitMock) -> None:
    """Verify a listable but unsearchable folder is pruned while other candidates remain."""
    # Given
    touch(tmp_path / "good" / "__pycache__" / "m.pyc")
    locked = tmp_path / "locked"
    touch(locked / "sub" / "__pycache__" / "m.pyc")
    age(tmp_path)
    locked.chmod(0o444)

    try:
        # When
        result = scan(options(tmp_path))
    finally:
        locked.chmod(0o755)

    # Then
    assert result == found(caches=[tmp_path / "good" / "__pycache__"])


def test_project_with_unsearchable_directory_counts_as_recent(tmp_path: Path, git: GitMock) -> None:
    """Verify activity that cannot be measured protects the whole project."""
    # Given
    touch(tmp_path / "proj" / "__pycache__" / "m.pyc")
    touch(tmp_path / "proj" / "data" / "sub" / "f.txt")
    age(tmp_path)
    (tmp_path / "proj" / "data").chmod(0o444)

    try:
        # When
        result = scan(options(tmp_path))
    finally:
        (tmp_path / "proj" / "data").chmod(0o755)

    # Then
    assert result == empty()


def test_unsearchable_marker_directory_does_not_abort_the_scan(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify a locked `.venv` fails its marker check and protects its project instead of raising."""
    # Given
    touch(tmp_path / "proj" / ".venv" / "pyvenv.cfg")
    touch(tmp_path / "proj" / ".pytest_cache" / "x")
    other = touch(tmp_path / "other" / ".pytest_cache" / "x").parent
    age(tmp_path)
    (tmp_path / "proj" / ".venv").chmod(0o444)

    try:
        # When
        result = scan(options(tmp_path))
    finally:
        (tmp_path / "proj" / ".venv").chmod(0o755)

    # Then
    assert result == found(caches=[other])


def test_locked_candidate_that_may_hold_git_is_dropped(tmp_path: Path, git: GitMock) -> None:
    """Verify a candidate whose `.git` probe is denied is never offered."""
    # Given
    project = tmp_path / "proj"
    touch(project / "node_modules" / ".git" / "HEAD")
    touch(project / ".pytest_cache" / "x")
    age(tmp_path)
    (project / "node_modules").chmod(0o444)

    try:
        # When
        result = scan(options(tmp_path))
    finally:
        (project / "node_modules").chmod(0o755)

    # Then
    assert result == found(caches=[project / ".pytest_cache"])


def test_ancestor_whose_git_probe_fails_drops_the_candidate(
    tmp_path: Path, git: GitMock, mocker: MockerFixture
) -> None:
    """Verify a candidate is dropped when an ancestor's `.git` cannot be probed."""
    # Given
    project = tmp_path / "proj"
    touch(project / "sub" / "__pycache__" / "m.pyc")
    touch(tmp_path / "other" / "__pycache__" / "m.pyc")
    age(tmp_path)
    real_lstat = Path.lstat

    def denied(self: Path) -> os.stat_result:
        if self == project / ".git":
            raise PermissionError(self)
        return real_lstat(self)

    mocker.patch.object(Path, "lstat", denied)

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(caches=[tmp_path / "other" / "__pycache__"])


def test_git_guard_matches_across_unicode_normalization(tmp_path: Path, git: GitMock) -> None:
    """Verify a decomposed folder name on disk is guarded by a precomposed tracked path."""
    # Given
    repo = tmp_path / "repo"
    make_repo(repo)
    folder = repo / "re\u0301sume\u0301"
    touch(folder / "package.json")
    touch(folder / "build" / "x")
    age(tmp_path)
    git.stdout = "r\u00e9sum\u00e9/package.json\0r\u00e9sum\u00e9/build/x\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()


def test_repo_above_root_does_not_unlock_build_in_untracked_project(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify a repository above the root cannot make build count in a project it does not track."""
    # Given a home folder that is a dotfiles repository tracking nothing under repos
    make_repo(tmp_path)
    root = tmp_path / "repos"
    touch(root / "foo" / "package.json")
    touch(root / "foo" / "build" / "real_source.c")
    age(tmp_path)
    git.stdout = ".bashrc\0"

    # When
    result = scan(options(root))

    # Then
    assert result == empty()


def test_build_next_to_tracked_marker_is_kept(tmp_path: Path, git: GitMock) -> None:
    """Verify an untracked build folder next to a tracked marker is offered."""
    # Given
    project = tmp_path / "repo"
    make_repo(project)
    touch(project / "package.json")
    build = touch(project / "build" / "o.js").parent
    age(tmp_path)
    git.stdout = "package.json\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == found(builds=[build])


def test_target_in_monorepo_below_root_needs_tracked_cargo_toml(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify target is offered when the scan root sits below a repository that tracks Cargo.toml."""
    # Given
    repo = tmp_path / "mono"
    make_repo(repo)
    root = repo / "crates"
    touch(root / "core" / "Cargo.toml")
    target = touch(root / "core" / "target" / "o").parent
    age(tmp_path)
    git.stdout = "crates/core/Cargo.toml\0"

    # When
    result = scan(options(root))

    # Then
    assert result == found(builds=[target])


@pytest.mark.parametrize(
    ("folder", "exclude_names"),
    [("target", ()), (".venv", (".venv",))],
)
def test_fresh_file_in_non_candidate_match_protects_project(
    tmp_path: Path, git: GitMock, folder: str, exclude_names: tuple[str, ...]
) -> None:
    """Verify recent edits inside a marker-failed or name-excluded match keep the project alive."""
    # Given
    project = tmp_path / "proj"
    touch(project / "node_modules" / "x.js")
    touch(project / folder / "notes.txt")
    touch(project / folder / "pyvenv.cfg")
    age(tmp_path)
    os.utime(project / folder / "notes.txt", (FRESH, FRESH))

    # When
    result = scan(options(tmp_path, exclude_names=exclude_names))

    # Then
    assert result == empty()


def test_repo_above_root_is_listed_once_per_scan(tmp_path: Path, git: GitMock) -> None:
    """Verify git lists a repository above the root once, however many projects it holds."""
    # Given
    make_repo(tmp_path)
    root = tmp_path / "repos"
    first = touch(root / "a" / ".mypy_cache" / "data").parent
    second = touch(root / "b" / ".mypy_cache" / "data").parent
    age(tmp_path)

    # When
    result = scan(options(root))

    # Then
    assert result == found(caches=[first, second])
    assert [cwd for _, cwd in git.calls] == [tmp_path]


def test_nested_projects_share_activity_answers_without_mixing_skips(
    tmp_path: Path, git: GitMock, mocker: MockerFixture
) -> None:
    """Verify a root-level project and a child project agree with separate scans and share walks."""
    # Given a cache directly in the root, and a child project whose own cache holds a fresh file
    touch(tmp_path / "__pycache__" / "m.pyc")
    app = tmp_path / "app"
    touch(app / "main.py")
    touch(app / "src" / "pkg" / "mod.py")
    app_cache = touch(app / ".mypy_cache" / "fresh.json").parent
    age(tmp_path)
    os.utime(app_cache / "fresh.json", (FRESH, FRESH))
    listed: list[Path] = []
    real_walk = Path.walk

    def spy(self: Path, *args: Any, **kwargs: Any) -> Iterator[tuple[Path, list[str], list[str]]]:
        for dirpath, dirnames, filenames in real_walk(self, *args, **kwargs):
            listed.append(dirpath)
            yield dirpath, dirnames, filenames

    mocker.patch.object(Path, "walk", spy)

    # When
    result = scan(options(tmp_path))

    # Then the root project sees the child's fresh cache, while the child skips its own
    assert result == found(caches=[app_cache])
    assert listed.count(app / "src" / "pkg") <= 2


def test_rule_lookup_keeps_first_match_across_literal_and_glob_patterns() -> None:
    """Verify the earliest rule wins whether its pattern is literal or a glob."""
    # Given
    glob_first = scanner._Rules.build(
        (
            scanner.Rule(pattern="*.out", category=Category.BUILDS),
            scanner.Rule(pattern="a.out", category=Category.CUSTOM),
        )
    )
    literal_first = scanner._Rules.build(
        (
            scanner.Rule(pattern="a.out", category=Category.CUSTOM),
            scanner.Rule(pattern="*.out", category=Category.BUILDS),
        )
    )

    # When / Then
    assert glob_first.first("a.out") == scanner.Rule(pattern="*.out", category=Category.BUILDS)
    assert literal_first.first("a.out") == scanner.Rule(pattern="a.out", category=Category.CUSTOM)
    assert literal_first.first("b.out") == scanner.Rule(pattern="*.out", category=Category.BUILDS)
    assert literal_first.first("a.OUT") is None


@pytest.mark.parametrize("text", ["Build/Dist", "plain", "MiXeD/Ünïcode", "re\u0301sume\u0301"])
def test_fold_ascii_fast_path_matches_full_normalization(text: str) -> None:
    """Verify folding gives the same result as full Unicode normalization."""
    # Given
    expected = unicodedata.normalize("NFC", unicodedata.normalize("NFD", text).casefold())

    # When / Then
    assert scanner._fold(text) == expected


def test_each_ancestor_is_probed_once_per_scan(
    tmp_path: Path, git: GitMock, mocker: MockerFixture
) -> None:
    """Verify candidates that share ancestors probe each ancestor for a repository once."""
    # Given
    touch(tmp_path / "proj" / "a" / "__pycache__" / "m.pyc")
    touch(tmp_path / "proj" / "b" / "__pycache__" / "m.pyc")
    age(tmp_path)
    probe = mocker.patch.object(scanner, "_probe_repo", wraps=scanner._probe_repo)

    # When
    scan(options(tmp_path))

    # Then
    probed = [call.args[0] for call in probe.call_args_list]
    assert len(probed) == len(set(probed))
    assert tmp_path / "proj" in probed


STALE = time.time() - 30 * DAY


@pytest.fixture
def home(tmp_path: Path, mocker: MockerFixture) -> Path:
    """Provide a fake home folder."""
    path = tmp_path / "home"
    path.mkdir()
    mocker.patch.object(projects, "invoking_home", return_value=path)
    return path


def configure(**values: SettingValue) -> PluginSettings:
    """Resolve the projects settings for a run that configures the given values."""
    return Config(values={"projects": values}).settings_for(PLUGIN)


def make_cache(project: Path, name: str = ".mypy_cache") -> Path:
    """Create an old project folder holding one tool cache and return the cache."""
    cache = project / name
    touch(cache / "data")
    touch(project / "main.py")
    age(project, STALE)
    return cache


def test_not_configured_without_paths(home: Path) -> None:
    """Verify the plugin reports itself not configured when no paths are set."""
    # Given
    settings = configure()

    # When / Then
    assert projects._prepare(settings) is None
    with pytest.raises(NotConfiguredError):
        measure_plugin(PLUGIN, include_dangerous=False, settings=settings)


def test_not_configured_without_a_home_folder(mocker: MockerFixture) -> None:
    """Verify the plugin reports itself not configured when no home folder is known."""
    # Given
    mocker.patch.object(projects, "invoking_home", side_effect=RuntimeError("no home"))

    # When / Then
    assert projects._prepare(configure(paths=("~/repos",))) is None


@pytest.mark.parametrize("kind", ["relative", "outside", "home", "missing", "symlink", "dotdot"])
def test_bad_roots_are_ignored_with_a_warning(
    home: Path, tmp_path: Path, mocker: MockerFixture, kind: str
) -> None:
    """Verify a bad root is dropped with a warning that names it and the reason."""
    # Given
    (tmp_path / "elsewhere").mkdir()
    (home / "repos").mkdir()
    (home / "link").symlink_to(tmp_path / "elsewhere")
    bad, reason = {
        "relative": ("repos", "not an absolute path"),
        "outside": (str(tmp_path / "elsewhere"), "not inside your home folder"),
        "home": (str(home), "not inside your home folder"),
        "missing": (str(home / "nowhere"), "not an existing folder"),
        "symlink": (str(home / "link"), "not inside your home folder"),
        "dotdot": (f"{home}/repos/..", "not inside your home folder"),
    }[kind]
    warning = mocker.patch.object(projects.pp, "warning")
    mocker.patch.object(projects, "scan", return_value=empty())

    # When
    roots = projects._roots((bad,), home=home.resolve())

    # Then
    assert roots == ()
    assert projects._prepare(configure(paths=(bad,))) is None
    assert any(bad in call.args[0] and reason in call.args[0] for call in warning.call_args_list)

    # When a good root is added
    good = home / "repos"

    # Then
    assert projects._prepare(configure(paths=(bad, str(good)))) is not None


def test_bad_root_warns_once_per_run(home: Path, mocker: MockerFixture) -> None:
    """Verify a bad root warns once in a run, even when listed twice, and again in the next run."""
    # Given
    warning = mocker.patch.object(projects.pp, "warning")
    bad = str(home / "nowhere")
    settings = configure(paths=(bad, bad))

    # When
    projects._prepare(settings)

    # Then
    assert warning.call_count == 1

    # When a second run starts
    projects._prepare(settings)

    # Then
    assert warning.call_count == 2


def test_tilde_expands_against_the_invoking_home(home: Path) -> None:
    """Verify a leading tilde means the invoking user's home."""
    # Given
    (home / "repos").mkdir()

    # When / Then
    assert projects._roots(("~/repos",), home=home.resolve()) == ((home / "repos").resolve(),)


def test_root_on_another_device_is_rejected(home: Path, mocker: MockerFixture) -> None:
    """Verify a root whose device differs from home is dropped with a warning."""
    # Given
    (home / "repos").mkdir()
    warning = mocker.patch.object(projects.pp, "warning")
    real = safety._device
    mocker.patch.object(
        safety, "_device", side_effect=lambda p: 999 if p.name == "repos" else real(p)
    )

    # When / Then
    assert projects._roots((str(home / "repos"),), home=home.resolve()) == ()
    assert warning.call_count == 1


def test_overlapping_roots_scan_once(home: Path, git: GitMock) -> None:
    """Verify nested roots are dropped so a cache is listed once."""
    # Given
    work = home / "repos" / "work"
    cache = make_cache(work / "app")
    entries = (str(home / "repos"), str(work))

    # When
    report = measure_plugin(PLUGIN, include_dangerous=False, settings=configure(paths=entries))

    # Then
    assert projects._roots(entries, home=home.resolve()) == ((home / "repos").resolve(),)
    caches = next(a for a in report.actions if a.action.name == "caches")
    assert caches.resolved_paths == [cache.resolve()]


def test_measure_plugin_reports_each_category(home: Path, git: GitMock) -> None:
    """Verify each action lists the paths of its own category with its tier."""
    # Given
    project = home / "repos" / "app"
    cache = make_cache(project)
    deps = project / "node_modules"
    touch(deps / "pkg" / "index.js")
    custom = project / "scratch"
    touch(custom / "file")
    age(project, STALE)
    settings = configure(paths=(str(home / "repos"),), include=("scratch",))

    # When
    report = measure_plugin(PLUGIN, include_dangerous=False, settings=settings)

    # Then
    by_name = {a.action.name: a for a in report.actions}
    assert list(by_name) == ["caches", "dependencies", "builds", "custom"]
    assert by_name["caches"].resolved_paths == [cache.resolve()]
    assert by_name["dependencies"].resolved_paths == [deps.resolve()]
    assert by_name["builds"].resolved_paths == []
    assert by_name["custom"].resolved_paths == [custom.resolve()]
    assert [a.action.tier for a in report.actions] == [
        Tier.SAFE,
        Tier.CAUTION,
        Tier.CAUTION,
        Tier.CAUTION,
    ]


def test_glob_characters_in_names_survive(home: Path, git: GitMock) -> None:
    """Verify a project folder with glob characters in its name is returned exactly."""
    # Given
    cache = make_cache(home / "repos" / "a[1]")
    settings = configure(paths=(str(home / "repos"),))

    # When
    report = measure_plugin(PLUGIN, include_dangerous=False, settings=settings)

    # Then
    caches = next(a for a in report.actions if a.action.name == "caches")
    assert caches.resolved_paths == [cache.resolve()]


def test_measure_footprint_remeasures_without_rescanning(
    home: Path, git: GitMock, mocker: MockerFixture
) -> None:
    """Verify the footprint after a cleanup re-measures the scanned paths without a new scan."""
    # Given
    project = home / "repos" / "app"
    first = make_cache(project)
    second = make_cache(project, ".ruff_cache")
    touch(first / "big", "x" * 100_000)
    touch(second / "big", "x" * 100_000)
    age(project, STALE)
    spy = mocker.patch.object(projects, "scan", wraps=projects.scan)
    report = measure_plugin(
        PLUGIN, include_dangerous=False, settings=configure(paths=(str(home / "repos"),))
    )
    before = report.footprint_bytes
    assert before is not None

    # When
    for child in second.iterdir():
        child.unlink()
    second.rmdir()
    after = measure_footprint(PLUGIN, include_dangerous=False, prepared_paths=report.prepared_paths)

    # Then
    assert after is not None
    assert after < before
    assert spy.call_count == 1
    assert report.prepared_paths["caches"] == tuple(sorted((first.resolve(), second.resolve())))


def test_failed_scan_leaves_the_plugin_with_no_paths(
    home: Path, git: GitMock, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a scan that raises is logged and the plugin still shows, offering nothing."""
    # Given
    make_cache(home / "repos" / "app")
    mocker.patch.object(projects, "scan", side_effect=RuntimeError("boom"))

    # When
    report = measure_plugin(
        PLUGIN, include_dangerous=False, settings=configure(paths=(str(home / "repos"),))
    )

    # Then
    assert report.prepared_paths == {}
    assert all(action.resolved_paths == [] for action in report.actions)
    assert report.footprint_bytes == 0
    assert any("prepare for projects failed" in r.getMessage() for r in caplog.records)


def test_exclude_splits_paths_and_names(home: Path, mocker: MockerFixture) -> None:
    """Verify exclude entries with a slash or tilde become paths, the rest names."""
    # Given
    (home / "repos").mkdir()
    settings = configure(
        paths=(str(home / "repos"),),
        exclude=("~/repos/keep/", "/abs/dir", "vendor/", "node_modules", ".venv*"),
    )
    spy = mocker.patch.object(projects, "scan", return_value=empty())

    # When
    projects._prepare(settings)

    # Then
    scan_options = spy.call_args.args[0]
    assert scan_options.exclude_paths == (home / "repos" / "keep", Path("/abs/dir"))
    assert scan_options.exclude_names == ("vendor", "node_modules", ".venv*")
    assert scan_options.max_depth == 6
    assert scan_options.recent_days == 7


@pytest.mark.parametrize("entry", ["repos/legacy", "~keep", "/", ""])
def test_relative_exclude_is_a_config_error(tmp_path: Path, entry: str) -> None:
    """Verify an exclude entry that is a relative path is rejected when the config loads."""
    # Given
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[plugins.projects]\nexclude = ["{entry}"]\n')

    # When / Then
    with pytest.raises(ConfigError, match="exclude"):
        load_config({"projects": PLUGIN}, env={"PURGER_CONFIG": str(config_file)})


def test_prepare_returns_paths_for_its_own_settings_only(home: Path, mocker: MockerFixture) -> None:
    """Verify each prepare call scans the roots of the settings it was given, sharing no state."""
    # Given
    (home / "a").mkdir()
    (home / "b").mkdir()

    def fake_scan(options: ScanOptions) -> dict[Category, list[Path]]:
        return {**empty(), Category.CACHES: [options.roots[0] / "cache"]}

    mocker.patch.object(projects, "scan", side_effect=fake_scan)
    first = configure(paths=(str(home / "a"),))
    second = configure(paths=(str(home / "b"),))

    # When
    from_first = projects._prepare(first)
    from_second = projects._prepare(second)

    # Then
    assert from_first is not None
    assert from_second is not None
    assert from_first["caches"] == ((home / "a").resolve() / "cache",)
    assert from_second["caches"] == ((home / "b").resolve() / "cache",)
    assert set(from_first) == {a.name for a in PLUGIN.actions if a.prepared}


def test_builds_lists_untracked_dist_in_a_git_project(home: Path, git: GitMock) -> None:
    """Verify the builds action offers an untracked dist folder next to a build marker."""
    # Given
    project = home / "repos" / "lib"
    make_repo(project)
    touch(project / "package.json", "{}")
    dist = touch(project / "dist" / "bundle.js").parent
    age(project, STALE)
    git.stdout = "package.json\0"
    settings = configure(paths=(str(home / "repos"),))

    # When
    report = measure_plugin(PLUGIN, include_dangerous=False, settings=settings)

    # Then
    builds = next(a for a in report.actions if a.action.name == "builds")
    assert builds.resolved_paths == [dist.resolve()]


def test_include_with_slash_is_a_config_error(tmp_path: Path) -> None:
    """Verify an include entry holding a slash is rejected when the config loads."""
    # Given
    config_file = tmp_path / "config.toml"
    config_file.write_text('[plugins.projects]\ninclude = ["a/b"]\n')

    # When / Then
    with pytest.raises(ConfigError, match="a/b"):
        load_config({"projects": PLUGIN}, env={"PURGER_CONFIG": str(config_file)})


@pytest.mark.parametrize(
    ("name", "value"),
    [("max_depth", 0), ("max_depth", -1), ("recent_days", -1)],
)
def test_numeric_settings_reject_bad_values(tmp_path: Path, name: str, value: int) -> None:
    """Verify depth and recent days reject out-of-range values."""
    # Given
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"[plugins.projects]\n{name} = {value}\n")

    # When / Then
    with pytest.raises(ConfigError):
        load_config({"projects": PLUGIN}, env={"PURGER_CONFIG": str(config_file)})


def test_dot_git_directory_without_head_is_not_a_repository(tmp_path: Path, git: GitMock) -> None:
    """Verify a `.git` directory that holds no HEAD above the root does not make a git project."""
    # Given
    touch(tmp_path / ".git" / "gk" / "config")
    root = tmp_path / "repos"
    cache = touch(root / "app" / ".mypy_cache" / "data").parent
    touch(root / "app" / "main.py")
    age(tmp_path)

    # When
    result = scan(options(root))

    # Then
    assert result == found(caches=[cache])
    assert git.calls == []


def test_dot_git_directory_with_head_is_a_repository(tmp_path: Path, git: GitMock) -> None:
    """Verify a `.git` directory holding HEAD above the root still gets the git guard."""
    # Given
    make_repo(tmp_path)
    root = tmp_path / "repos"
    cache = touch(root / "app" / ".mypy_cache" / "data").parent
    age(tmp_path)

    # When
    result = scan(options(root))

    # Then
    assert result == found(caches=[cache])
    assert [cwd for _, cwd in git.calls] == [tmp_path]


def test_git_repo_above_root_measures_activity_per_root_child(tmp_path: Path, git: GitMock) -> None:
    """Verify a fresh file in a sibling project does not protect another project's cache."""
    # Given
    make_repo(tmp_path)
    root = tmp_path / "repos"
    old = touch(root / "old" / ".mypy_cache" / "data").parent
    touch(root / "old" / "main.py")
    touch(root / "busy" / ".mypy_cache" / "data")
    touch(root / "busy" / "main.py")
    age(tmp_path)
    os.utime(root / "busy" / "main.py", (FRESH, FRESH))

    # When
    result = scan(options(root))

    # Then
    assert result == found(caches=[old])


def test_git_repo_above_root_fresh_file_in_same_child_protects(
    tmp_path: Path, git: GitMock
) -> None:
    """Verify a fresh file deep in the same root child protects its cache."""
    # Given
    make_repo(tmp_path)
    root = tmp_path / "repos"
    touch(root / "app" / ".mypy_cache" / "data")
    touch(root / "app" / "docs" / "index.md")
    age(tmp_path)
    os.utime(root / "app" / "docs" / "index.md", (FRESH, FRESH))

    # When
    result = scan(options(root))

    # Then
    assert result == found()


def test_git_guard_uses_canonical_caseless_matching(tmp_path: Path, git: GitMock) -> None:
    """Verify a tracked path equal to the folder only under canonical caseless matching protects it."""
    # Given a folder whose precomposed name casefolds to a different order of marks
    repo = tmp_path / "repo"
    make_repo(repo)
    touch(repo / "\u1f80\u0302" / "node_modules" / "x.js")
    age(tmp_path)
    git.stdout = "\u1f00\u0302\u0399/node_modules/x.js\0"

    # When
    result = scan(options(tmp_path))

    # Then
    assert result == empty()
