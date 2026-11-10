"""Tests for plugin size-parsing and availability hooks."""

from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from nclutils.sh import CompletedCommand, ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.output import is_partial
from purger.plugins import apt, brew, docker, mise, npm, pip, pre_commit, yarn
from purger.plugins.brew import _parse_dry_run
from purger.sizing import disk_usage, measure_plugin

_DF_OUTPUT = (
    '{"Reclaimable":"1MB (50%)","Type":"Images"}\n'
    '{"Reclaimable":"0B","Type":"Containers"}\n'
    '{"Reclaimable":"2MB (100%)","Type":"Local Volumes"}\n'
    '{"Reclaimable":"0B","Type":"Build Cache"}\n'
)


def _completed(*, returncode: int = 0, stdout: str = "") -> CompletedCommand:
    return CompletedCommand(
        argv=("tool",), returncode=returncode, stdout=stdout, stderr="", duration=0.0, cwd=None
    )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Would remove: x (64B)\n==> This operation would free approximately 3GB of disk space.",
            3 * 1024**3,
        ),
        ("==> This operation would free approximately 861.6KB of disk space.", 882_278),
        ("==> This operation would free approximately 12MB of disk space.", 12 * 1024**2),
        ("==> This operation would free approximately 64B of disk space.", 64),
        ("Would remove: x (64B)", 0),
        ("", 0),
    ],
)
def test_parse_dry_run_extracts_freed_bytes(output: str, expected: int) -> None:
    """Verify the brew parser reads the freed-space summary line in binary units."""
    # Given / When
    result = _parse_dry_run(output)

    # Then
    assert result == expected


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (frozenset({"Images", "Containers", "Build Cache"}), 1_000_000),
        (frozenset({"Local Volumes"}), 2_000_000),
        (frozenset({"Build Cache"}), 0),
    ],
)
def test_reclaimable_from_sums_requested_types(types: frozenset[str], expected: int) -> None:
    """Verify the docker parser sums only the requested types."""
    # Given / When
    result = docker._reclaimable_from(_DF_OUTPUT, types)

    # Then
    assert result == expected


def test_docker_measurement_runs_system_df_once(mocker: MockerFixture) -> None:
    """Verify measuring docker reads one docker system df run for every size."""
    # Given docker system df output
    run = mocker.patch(
        "purger.sizing.run_command", autospec=True, return_value=_completed(stdout=_DF_OUTPUT)
    )

    # When
    report = measure_plugin(docker.PLUGIN, include_dangerous=True)

    # Then the command ran once and each action reads its own row types
    run.assert_called_once()
    assert run.call_args.args[0] == ["docker", "system", "df", "--format", "json"]
    assert {a.action.name: a.size_bytes for a in report.actions} == {
        "builder": 0,
        "system": 1_000_000,
        "volumes": 2_000_000,
    }
    assert report.footprint_bytes == 3_000_000


def test_docker_is_available_false_when_daemon_down(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when docker info exits non-zero."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        return_value=_completed(returncode=1),
    )

    # When / Then
    assert docker._is_available() is False


def test_docker_is_available_false_on_shell_error(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when the command cannot run."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["docker"]),
    )

    # When / Then
    assert docker._is_available() is False


def test_docker_is_available_true_when_daemon_up(mocker: MockerFixture) -> None:
    """Verify docker is available when docker info succeeds."""
    # Given
    mocker.patch("purger.runner.run_command", autospec=True, return_value=_completed())

    # When / Then
    assert docker._is_available() is True


def test_brew_autoremove_names_strip_header_and_tap() -> None:
    """Verify the autoremove dry run yields Cellar names, without the header or tap prefix."""
    # Given
    output = "==> Would autoremove 2 unneeded formulae:\nlibfoo\nuser/tap/bar\n"

    # When / Then
    assert brew._autoremove_names(output) == ["libfoo", "bar"]


def test_brew_autoremove_size_measures_cellar_racks(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the autoremove size sums only the racks of formulae it would remove."""
    # Given a Cellar with one removable and one kept formula
    (tmp_path / "libfoo" / "1.0").mkdir(parents=True)
    (tmp_path / "libfoo" / "1.0" / "lib").write_bytes(b"x" * 10_000)
    (tmp_path / "kept").mkdir()
    (tmp_path / "kept" / "big").write_bytes(b"x" * 50_000)
    outputs = {
        ("brew", "--cellar"): f"{tmp_path}\n",
        ("brew", "autoremove", "--dry-run"): "==> Would autoremove 1 unneeded formula:\nlibfoo\n",
    }
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=lambda argv, **_: _completed(stdout=outputs[tuple(argv)]),
    )

    # When
    size = brew._autoremove_size()

    # Then
    assert size == disk_usage([tmp_path / "libfoo"])


def test_brew_autoremove_size_zero_when_nothing_removable(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify an empty dry run measures zero, since brew prints nothing then."""
    # Given
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=lambda argv, **_: _completed(
            stdout=f"{tmp_path}\n" if "--cellar" in argv else ""
        ),
    )

    # When / Then
    assert brew._autoremove_size() == 0


def test_mise_prunable_install_paths() -> None:
    """Verify every prunable version's install path is read from the JSON."""
    # Given
    output = (
        '{"node": [{"version": "20.0.0", "install_path": "/m/node/20.0.0"},'
        ' {"version": "21.0.0", "install_path": "/m/node/21.0.0"}],'
        ' "go": [{"version": "1.0"}]}'
    )

    # When / Then
    assert mise._prunable_install_paths(output) == [
        Path("/m/node/20.0.0"),
        Path("/m/node/21.0.0"),
    ]


def test_mise_prunable_install_paths_empty() -> None:
    """Verify nothing prunable measures nothing."""
    # Given / When / Then
    assert mise._prunable_install_paths("{}") == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"PRE_COMMIT_HOME": "/pch"}, Path("/pch")),
        ({"XDG_CACHE_HOME": "/xdg"}, Path("/xdg/pre-commit")),
        ({}, Path("/home/me/.cache/pre-commit")),
    ],
)
def test_pre_commit_cache_dir_lookup_order(
    env: dict[str, str], expected: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """Verify the cache directory follows pre-commit's own lookup order."""
    # Given
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mocker.patch("purger.plugins.pre_commit.invoking_home", return_value=Path("/home/me"))

    # When / Then
    assert pre_commit._cache_dir() == expected


@pytest.mark.parametrize("module", [pip, yarn], ids=["pip", "yarn"])
def test_path_cache_plugins_available_only_with_a_cache(
    module: ModuleType, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify path-only plugins apply only when a cache holds something, without the tool installed."""
    # Given an empty home
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    assert module.PLUGIN.is_available is not None
    before = module.PLUGIN.is_available()

    # When a Linux-style cache entry appears
    entry = tmp_path / ".cache" / module.PLUGIN.name / "entry"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"x")

    # Then
    assert before is False
    assert module.PLUGIN.is_available() is True


def test_npm_footprint_counts_only_cleanable_parts(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the npm footprint leaves out logs, so its two cleanups cover all of it."""
    # Given an npm cache with the package cache, the npx cache, and logs
    for part, size in (("_cacache", 10_000), ("_npx", 10_000), ("_logs", 50_000)):
        (tmp_path / part).mkdir()
        (tmp_path / part / "f").write_bytes(b"x" * size)
    mocker.patch(
        "purger.sizing.run_command", autospec=True, return_value=_completed(stdout=f"{tmp_path}\n")
    )

    # When
    report = measure_plugin(npm.PLUGIN, include_dangerous=True)

    # Then
    assert report.footprint_bytes == disk_usage([tmp_path / "_cacache", tmp_path / "_npx"])
    assert is_partial(report) is False


def test_apt_footprint_matches_clean(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify apt is exact: its footprint counts only the package files that clean removes."""
    # Given an apt archive with a package, a lock file, and a partial download directory
    archives = tmp_path / "archives"
    (archives / "partial").mkdir(parents=True)
    (archives / "pkg.deb").write_bytes(b"x" * 10_000)
    (archives / "lock").write_bytes(b"x" * 100)

    def moved(paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(path.replace("/var/cache/apt/archives", str(archives)) for path in paths)

    plugin = replace(
        apt.PLUGIN,
        paths=moved(apt.PLUGIN.paths),
        actions=tuple(replace(a, paths=moved(a.paths)) for a in apt.PLUGIN.actions),
    )

    # When
    report = measure_plugin(plugin, include_dangerous=False)

    # Then
    assert report.footprint_bytes == disk_usage([archives / "pkg.deb"])
    assert is_partial(report) is False
