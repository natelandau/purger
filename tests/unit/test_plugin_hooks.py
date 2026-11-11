"""Tests for plugin size-parsing and availability hooks."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from nclutils.sh import CompletedCommand, ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.output import is_partial
from purger.plugins import (
    ansible,
    apt,
    brew,
    bun,
    cocoapods,
    conda,
    corepack,
    docker,
    maestro,
    mise,
    npm,
    pacman,
    pip,
    playwright,
    pnpm,
    podman,
    poetry,
    pre_commit,
    rip2,
    simulators,
    thumbnails,
    trash,
    yarn,
)
from purger.plugins.brew import _parse_dry_run
from purger.plugins.npm import _explain_npx_failure
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


def _patch_docker(
    mocker: MockerFixture, *, context_host: str = "unix:///var/run/docker.sock", info_rc: int = 0
) -> MagicMock:
    """Answer the docker context query and docker info, and return the docker info mock."""
    mocker.patch(
        "purger.plugins.docker.run_command",
        autospec=True,
        return_value=_completed(stdout=f"{context_host}\n"),
    )
    return mocker.patch(
        "purger.runner.run_command", autospec=True, return_value=_completed(returncode=info_rc)
    )


def test_docker_is_available_false_when_daemon_down(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when docker info exits non-zero."""
    # Given
    _patch_docker(mocker, info_rc=1)

    # When / Then
    assert docker._is_available() is False


def test_docker_is_available_false_on_shell_error(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when the command cannot run."""
    # Given
    mocker.patch(
        "purger.plugins.docker.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["docker"]),
    )

    # When / Then
    assert docker._is_available() is False


@pytest.mark.parametrize(
    "context_host",
    [
        "unix:///var/run/docker.sock",
        "unix:///Users/me/.orbstack/run/docker.sock",
        "unix:///Users/me/.docker/run/docker.sock",
    ],
)
def test_docker_is_available_true_for_a_local_engine(
    mocker: MockerFixture, context_host: str
) -> None:
    """Verify docker is available when the context is a unix socket and docker info succeeds."""
    # Given
    _patch_docker(mocker, context_host=context_host)

    # When / Then
    assert docker._is_available() is True


@pytest.mark.parametrize(
    "context_host",
    ["tcp://10.0.0.5:2375", "ssh://me@build-host", "tcp://127.0.0.1:2375", ""],
)
def test_docker_is_unavailable_for_a_remote_engine(
    mocker: MockerFixture, context_host: str
) -> None:
    """Verify a TCP or SSH context, even on loopback, never reaches docker info or a prune."""
    # Given
    info = _patch_docker(mocker, context_host=context_host)

    # When / Then
    assert docker._is_available() is False
    info.assert_not_called()


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


def test_brew_footprint_counts_versions_both_actions_remove_once(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify an old version that cleanup and autoremove both remove is counted once."""
    # Given a removable formula whose old version cleanup also removes
    old = tmp_path / "libfoo" / "1.0"
    current = tmp_path / "libfoo" / "2.0"
    for version in (old, current):
        version.mkdir(parents=True)
        (version / "lib").write_bytes(b"x" * 10_000)
    cleanup_output = f"Would remove: {old} (1 file, 1KB)\n==> This operation would free approximately 1KB of disk space.\n"
    outputs = {
        ("brew", "--cellar"): f"{tmp_path}\n",
        ("brew", "autoremove", "--dry-run"): "==> Would autoremove 1 unneeded formula:\nlibfoo\n",
        brew._CLEANUP_DRY_RUN: cleanup_output,
    }
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=lambda argv, **_: _completed(stdout=outputs[tuple(argv)]),
    )
    mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        side_effect=lambda argv, **_: _completed(stdout=outputs[tuple(argv)]),
    )

    # When
    report = measure_plugin(brew.PLUGIN, include_dangerous=False)

    # Then the footprint is cleanup's total plus the formula folder, less the old version
    rack = old.parent
    assert report.footprint_bytes == 1024 + disk_usage([rack]) - disk_usage([old])
    sizes = {r.action.name: r.size_bytes for r in report.actions}
    assert sizes == {"cleanup": 1024, "autoremove": disk_usage([rack])}


def test_brew_footprint_falls_back_to_cleanup_when_autoremove_fails(
    mocker: MockerFixture,
) -> None:
    """Verify the footprint keeps cleanup's total when the autoremove dry run fails."""
    # Given
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["brew"]),
    )
    output = "==> This operation would free approximately 2KB of disk space.\n"

    # When / Then
    assert brew._footprint(output) == 2048


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
    module: ModuleType, tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify path-only plugins apply only when a cache holds something, without the tool installed."""
    # Given an empty home and no XDG_CACHE_HOME pointing outside it
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    mocker.patch.object(module, "_CACHE_PATHS", module._cache_patterns())
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


@pytest.mark.parametrize("module", [pip, yarn], ids=["pip", "yarn"])
def test_path_cache_plugins_follow_xdg_cache_home(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pip and Yarn 1 caches are found under XDG_CACHE_HOME, where both write on Linux."""
    # Given
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/me/xdg")

    # When
    patterns = module._cache_patterns()

    # Then
    assert f"/home/me/xdg/{module.PLUGIN.name}/*" in patterns
    assert f"~/.cache/{module.PLUGIN.name}/*" in patterns


def test_poetry_available_with_cache_or_virtualenvs(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Poetry applies when it left a cache or a virtualenv, without poetry installed."""
    # Given an empty home and no environment overrides pointing outside it
    for var in ("XDG_CACHE_HOME", "POETRY_CACHE_DIR", "POETRY_VIRTUALENVS_PATH"):
        monkeypatch.delenv(var, raising=False)
    mocker.patch.object(poetry, "_CACHE_PATHS", poetry._cache_patterns())
    mocker.patch.object(poetry, "_VIRTUALENV_PATHS", poetry._virtualenv_patterns())
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    assert poetry.PLUGIN.is_available is not None
    before = poetry.PLUGIN.is_available()

    # When a Linux-style virtualenv appears
    venv = tmp_path / ".cache" / "pypoetry" / "virtualenvs" / "app-abc123-py3.13"
    venv.mkdir(parents=True)

    # Then
    assert before is False
    assert poetry.PLUGIN.is_available() is True


def test_poetry_cache_paths_follow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify POETRY_CACHE_DIR and XDG_CACHE_HOME add cache folders without dropping the defaults."""
    # Given
    monkeypatch.setenv("POETRY_CACHE_DIR", "/data/poetry")
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/me/xdg")

    # When
    patterns = poetry._cache_patterns()

    # Then
    assert "/data/poetry/cache/*" in patterns
    assert "/data/poetry/artifacts/*" in patterns
    assert "/home/me/xdg/pypoetry/artifacts/*" in patterns
    assert "~/Library/Caches/pypoetry/cache/*" in patterns
    assert "~/.cache/pypoetry/artifacts/*" in patterns
    assert not any("virtualenvs" in pattern for pattern in patterns)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"POETRY_VIRTUALENVS_PATH": "/venvs"}, "/venvs/*"),
        ({"POETRY_CACHE_DIR": "/data/poetry"}, "/data/poetry/virtualenvs/*"),
        ({}, "~/Library/Caches/pypoetry/virtualenvs/*"),
    ],
)
def test_poetry_virtualenv_paths_follow_environment(
    env: dict[str, str], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the virtualenv folder follows Poetry's overrides, first in the list, and keeps the defaults."""
    # Given
    for var in ("XDG_CACHE_HOME", "POETRY_CACHE_DIR", "POETRY_VIRTUALENVS_PATH"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # When
    patterns = poetry._virtualenv_patterns()

    # Then
    assert patterns[0] == expected
    assert "~/.cache/pypoetry/virtualenvs/*" in patterns
    assert len(patterns) == len(set(patterns))


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

    def moved(paths: tuple[str, ...] | Callable[[], tuple[str, ...]]) -> tuple[str, ...]:
        assert isinstance(paths, tuple)
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


@pytest.mark.parametrize(
    ("stderr", "has_hint"),
    [
        ("npm error code ENOTEMPTY\nnpm error syscall rmdir", True),
        ("npm error code EACCES", False),
    ],
)
def test_explain_npx_failure_hints_only_on_enotempty(stderr: str, *, has_hint: bool) -> None:
    """Verify the npx hint names a concurrent npx run only for a not-empty directory error."""
    # Given / When
    hint = _explain_npx_failure(stderr)

    # Then
    assert (hint is not None) is has_hint
    if hint is not None:
        assert "npx" in hint


def test_trash_available_when_readable(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the trash plugin applies when the Trash folder can be listed."""
    # Given
    (tmp_path / ".Trash").mkdir()
    mocker.patch("purger.plugins.trash.invoking_home", return_value=tmp_path)

    # When / Then
    assert trash._is_available() is True


def test_trash_available_when_missing(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a home without a Trash folder counts as available with nothing to clean."""
    # Given
    mocker.patch("purger.plugins.trash.invoking_home", return_value=tmp_path)

    # When / Then
    assert trash._is_available() is True


def test_trash_unavailable_without_permission(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a Trash that macOS refuses to list reports unavailable instead of empty."""
    # Given
    mocker.patch("purger.plugins.trash.invoking_home", return_value=tmp_path)
    mocker.patch.object(Path, "iterdir", side_effect=PermissionError)

    # When / Then
    assert trash._is_available() is False


_RIP2_HELP = (
    "Subcommands:\n"
    "  completions  Generate shell completions file\n"
    "  graveyard    Print the graveyard path\n"
)
_OLD_RIP_HELP = "rip: a safe and ergonomic alternative to rm\n    -d, --decompose\n"


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [(0, _RIP2_HELP, True), (0, _OLD_RIP_HELP, False), (1, _RIP2_HELP, False)],
    ids=["rip2", "old-rip", "help-fails"],
)
def test_rip2_available_only_for_rip2(
    returncode: int, stdout: str, expected: bool, mocker: MockerFixture
) -> None:
    """Verify the plugin applies only when `rip` is rip2, since the original rip would bury a file named graveyard."""
    # Given
    mocker.patch(
        "purger.plugins.rip2.run_command",
        autospec=True,
        return_value=_completed(returncode=returncode, stdout=stdout),
    )
    assert rip2.PLUGIN.is_available is not None

    # When / Then
    assert rip2.PLUGIN.is_available() is expected


def test_rip2_measures_printed_graveyard(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the footprint is the size of the directory `rip graveyard` prints."""
    # Given
    (tmp_path / "buried").write_bytes(b"x" * 10_000)
    mocker.patch(
        "purger.sizing.run_command", autospec=True, return_value=_completed(stdout=f"{tmp_path}\n")
    )

    # When
    report = measure_plugin(rip2.PLUGIN, include_dangerous=False)

    # Then
    assert report.footprint_bytes == disk_usage([tmp_path])


@pytest.mark.parametrize("leftover", [True, False], ids=["leftovers", "nothing-to-clean"])
def test_ansible_available_only_with_leftovers(
    leftover: bool, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the plugin applies only with leftovers to clean."""
    # Given
    if leftover:
        stale = tmp_path / ".ansible" / "tmp" / "ansible-tmp-1747399837.59-86917-2270"
        stale.mkdir(parents=True)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    mocker.patch.object(ansible, "_TMP_PATHS", ("~/.ansible/tmp/*",))
    mocker.patch.object(ansible, "_GALAXY_PATHS", ("~/.ansible/galaxy_cache/*",))
    assert ansible.PLUGIN.is_available is not None

    # When / Then
    assert ansible.PLUGIN.is_available() is leftover


def test_ansible_skips_while_ansible_runs() -> None:
    """Verify a running ansible command makes the plugin busy."""
    # When / Then
    assert ansible.PLUGIN.busy_process is ansible._is_ansible_command


@pytest.mark.parametrize(
    "cmdline",
    [
        ["/usr/bin/python3", "/Users/me/.local/bin/ansible-playbook", "site.yml"],
        ["/usr/bin/python3", "-sP", "/usr/bin/ansible-playbook", "site.yml"],
        ["/opt/homebrew/bin/ansible", "all", "-m", "ping"],
        ["ansible-galaxy", "install", "-r", "requirements.yml"],
    ],
)
def test_ansible_command_detected(cmdline: list[str]) -> None:
    """Verify ansible commands count as running however they were started."""
    # Given / When / Then
    assert ansible._is_ansible_command(cmdline) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        ["/usr/bin/python3", "/Users/me/.local/bin/purger", "clean", "ansible"],
        ["vim", "/Users/me/.ansible/notes"],
        ["/usr/bin/python3", "/Users/me/.local/share/ansible/script.py"],
        [],
    ],
)
def test_ansible_command_not_detected(cmdline: list[str]) -> None:
    """Verify purger itself, unrelated commands, and unreadable argv do not count as ansible."""
    # Given / When / Then
    assert ansible._is_ansible_command(cmdline) is False


@pytest.mark.parametrize(
    ("ansible_home", "expected"),
    [
        (None, ("~/.ansible",)),
        ("", ("~/.ansible",)),
        ("~/.ansible/", ("~/.ansible",)),
        ("/Users/me/.local/share/ansible", ("~/.ansible", "/Users/me/.local/share/ansible")),
    ],
)
def test_ansible_homes_follow_ansible_home(
    ansible_home: str | None, expected: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an ANSIBLE_HOME is cleaned alongside the default, which is listed once."""
    # Given
    monkeypatch.delenv("ANSIBLE_HOME", raising=False)
    if ansible_home is not None:
        monkeypatch.setenv("ANSIBLE_HOME", ansible_home)

    # When / Then
    assert ansible._ansible_homes() == expected


def test_pnpm_stale_metadata_dirs_keep_only_listed_cache_folders(tmp_path: Path) -> None:
    """Verify only listed folders that exist inside the cache are sized, not the summary line."""
    # Given
    (tmp_path / "v11" / "metadata").mkdir(parents=True)
    (tmp_path / "v11" / "metadata-full").mkdir(parents=True)
    output = (
        "Dry run: 3 directories would be deleted.\n"
        "v11/metadata/https%3A+registry.npmjs.org\n"
        "v11/metadata\n"
        "v11/metadata-full\n"
        "../outside\n"
    )
    (tmp_path.parent / "outside").mkdir(exist_ok=True)

    # When
    dirs = pnpm._stale_metadata_dirs(output, cache_dir=tmp_path)

    # Then
    root = tmp_path.resolve()
    assert dirs == [root / "v11" / "metadata", root / "v11" / "metadata-full"]


def test_pnpm_metadata_prune_measures_listed_folders(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the metadata-prune size is the folders the dry run lists under the cache path."""
    # Given
    stale = tmp_path / "v11" / "metadata"
    stale.mkdir(parents=True)
    (stale / "pkg.json").write_bytes(b"x" * 10_000)
    current = tmp_path / "v12" / "metadata"
    current.mkdir(parents=True)
    (current / "pkg.json").write_bytes(b"x" * 50_000)
    mocker.patch(
        "purger.plugins.pnpm.run_command",
        autospec=True,
        side_effect=[
            _completed(stdout=f"{tmp_path}\n"),
            _completed(stdout="Dry run: 1 directories would be deleted.\nv11/metadata\n"),
        ],
    )
    action = next(a for a in pnpm.PLUGIN.actions if a.name == "metadata-prune")
    assert action.size is not None

    # When
    size = action.size()

    # Then
    assert size == disk_usage([stale])


@pytest.mark.parametrize(
    ("xdg_state_home", "expected"),
    [
        (None, ("~/.maestro/tests/*", "~/.local/state/maestro/tests/*")),
        ("/Users/me/state/", ("~/.maestro/tests/*", "/Users/me/state/maestro/tests/*")),
    ],
)
def test_maestro_output_follows_xdg_state_home(
    xdg_state_home: str | None, expected: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify both the documented and the XDG test output folders are cleaned."""
    # Given
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if xdg_state_home is not None:
        monkeypatch.setenv("XDG_STATE_HOME", xdg_state_home)

    # When / Then
    assert maestro._test_output_patterns() == expected


@pytest.mark.parametrize("has_output", [True, False], ids=["output", "nothing-to-clean"])
def test_maestro_available_only_with_output(
    has_output: bool, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the plugin applies only with run output to clean."""
    # Given
    if has_output:
        (tmp_path / ".maestro" / "tests" / "2026-11-10_041643").mkdir(parents=True)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    mocker.patch.object(maestro, "_TEST_OUTPUT_PATHS", ("~/.maestro/tests/*",))
    assert maestro.PLUGIN.is_available is not None

    # When / Then
    assert maestro.PLUGIN.is_available() is has_output


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/usr/bin/java", "-classpath", "/x/lib/*", "maestro.cli.AppKt", "test"], True),
        (["/bin/zsh"], False),
    ],
    ids=["maestro", "other"],
)
def test_maestro_busy_while_maestro_runs(cmdline: list[str], expected: bool) -> None:
    """Verify only a Maestro JVM process makes the plugin busy."""
    # Given
    assert maestro.PLUGIN.busy_process is not None

    # When / Then
    assert maestro.PLUGIN.busy_process(cmdline) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "==> finished dry run: 3 candidates (disk space saved: 120.5 MiB)\n",
            int(120.5 * 1024**2),
        ),
        ("==> finished dry run: 1 candidates (disk space saved: 812 KiB)\n", 812 * 1024),
        ("==> no candidate packages found for pruning\n", 0),
    ],
)
def test_pacman_reads_dry_run_savings(output: str, expected: int) -> None:
    """Verify the paccache dry-run summary converts to bytes, and no candidates means zero."""
    # Given / When / Then
    assert pacman._saved_bytes(output) == expected


def test_conda_counts_tarballs_and_unused_packages() -> None:
    """Verify the conda size adds tarballs and unused packages, ignoring unsized parts."""
    # Given
    output = (
        '{"success": true, "tarballs": {"total_size": 1000, "pkg_sizes": {}}, '
        '"packages": {"total_size": 2500}, "index_cache": {"files": ["/x"]}, "logfiles": []}'
    )

    # When / Then
    assert conda._reclaimable_bytes(output) == 3500


def test_conda_size_without_package_sections_is_zero() -> None:
    """Verify a report that holds only unsized parts measures as zero."""
    # Given / When / Then
    assert conda._reclaimable_bytes('{"success": true, "logfiles": []}') == 0


_PODMAN_DF = (
    '[{"Type":"Images","RawReclaimable":900,"Reclaimable":"900B (7%)"},'
    '{"Type":"Containers","RawReclaimable":100,"Reclaimable":"100B"},'
    '{"Type":"Local Volumes","RawReclaimable":5000,"Reclaimable":"5kB"}]'
)


@pytest.mark.parametrize(
    ("types", "expected"),
    [(frozenset({"Images", "Containers"}), 1000), (frozenset({"Local Volumes"}), 5000)],
)
def test_podman_sums_raw_reclaimable(types: frozenset[str], expected: int) -> None:
    """Verify podman sizes come from the integer RawReclaimable field of the chosen rows."""
    # Given / When / Then
    assert podman._reclaimable_from(_PODMAN_DF, types=types) == expected


def _patch_podman(
    mocker: MockerFixture, *, info: str | None, connections: list[dict[str, object]] | None = None
) -> None:
    """Answer podman info and podman system connection list; None makes that command fail."""

    def answer(argv: list[str], **_: object) -> CompletedCommand:
        out = (
            info if argv[1] == "info" else None if connections is None else json.dumps(connections)
        )
        return _completed(returncode=125) if out is None else _completed(stdout=out)

    mocker.patch("purger.plugins.podman.run_command", autospec=True, side_effect=answer)


def _podman_info(*, remote: bool) -> str:
    return json.dumps({"host": {"serviceIsRemote": remote}})


_MACHINE: dict[str, object] = {
    "Name": "podman-machine-default",
    "URI": "ssh://core@127.0.0.1:53298/run/user/501/podman/podman.sock",
    "IsMachine": True,
    "Default": True,
}
_REMOTE: dict[str, object] = {
    "Name": "prod",
    "URI": "ssh://root@example.com/run/podman/podman.sock",
    "IsMachine": False,
    "Default": False,
}


def test_podman_unavailable_when_info_fails(mocker: MockerFixture) -> None:
    """Verify podman is skipped when it does not answer, such as a stopped machine on macOS."""
    # Given
    _patch_podman(mocker, info=None)
    assert podman.PLUGIN.is_available is not None

    # When / Then
    assert podman.PLUGIN.is_available() is False


def test_podman_available_for_a_local_service(mocker: MockerFixture) -> None:
    """Verify native podman on Linux, whose service is not remote, is available."""
    # Given
    _patch_podman(mocker, info=_podman_info(remote=False))

    # When / Then
    assert podman._is_available() is True


def test_podman_available_through_a_podman_machine(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the default podman machine connection on macOS counts as local."""
    # Given
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)
    _patch_podman(mocker, info=_podman_info(remote=True), connections=[_MACHINE, _REMOTE])

    # When / Then
    assert podman._is_available() is True


@pytest.mark.parametrize(
    ("env", "connections"),
    [
        ({"CONTAINER_CONNECTION": "prod"}, [_MACHINE, _REMOTE]),
        ({}, [{**_MACHINE, "Default": False}, {**_REMOTE, "Default": True}]),
        ({"CONTAINER_HOST": "ssh://root@example.com/run/podman/podman.sock"}, [_MACHINE]),
        ({"CONTAINER_HOST": "tcp://127.0.0.1:34451"}, [_MACHINE]),
        ({"CONTAINER_CONNECTION": "missing"}, [_MACHINE]),
        (
            {"CONTAINER_HOST": "unix:///run/podman/podman.sock", "CONTAINER_CONNECTION": "prod"},
            [_MACHINE, _REMOTE],
        ),
        ({}, None),
    ],
)
def test_podman_unavailable_for_a_remote_service(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    connections: list[dict[str, object]] | None,
) -> None:
    """Verify a connection to another host, or one podman cannot name, is never available."""
    # Given
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _patch_podman(mocker, info=_podman_info(remote=True), connections=connections)

    # When / Then
    assert podman._is_available() is False


@pytest.mark.parametrize(
    ("xdg_data_home", "expected"),
    [
        (
            None,
            (
                "~/.local/share/Trash/files/*",
                "~/.local/share/Trash/info/*",
                "~/.local/share/Trash/directorysizes",
            ),
        ),
        (
            "/Users/me/data",
            (
                "/Users/me/data/Trash/files/*",
                "/Users/me/data/Trash/info/*",
                "/Users/me/data/Trash/directorysizes",
            ),
        ),
    ],
)
def test_linux_trash_empties_contents_not_folders(
    xdg_data_home: str | None, expected: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the Linux Trash keeps its files and info folders and follows XDG_DATA_HOME."""
    # Given
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if xdg_data_home is not None:
        monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home)

    # When / Then
    assert trash._trash_paths("linux") == expected


def test_macos_trash_empties_home_trash() -> None:
    """Verify the macOS Trash is the home .Trash folder."""
    # Given / When / Then
    assert trash._trash_paths("darwin") == ("~/.Trash/*",)


def test_thumbnails_clean_each_size_folder() -> None:
    """Verify the thumbnail action removes the size folders inside the thumbnail cache."""
    # Given
    action = thumbnails.PLUGIN.actions[0]

    # When / Then
    assert isinstance(action.paths, tuple)
    assert len(action.paths) == 1
    assert action.paths[0].endswith("/thumbnails/*")


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        ({}, "darwin", "~/Library/Caches/ms-playwright"),
        ({}, "linux", "~/.cache/ms-playwright"),
        ({"XDG_CACHE_HOME": "/xdg"}, "linux", "/xdg/ms-playwright"),
        ({"PLAYWRIGHT_BROWSERS_PATH": "/browsers"}, "darwin", "/browsers"),
        ({"PLAYWRIGHT_BROWSERS_PATH": "0"}, "darwin", "~/Library/Caches/ms-playwright"),
    ],
)
def test_playwright_registry_dir(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], platform: str, expected: str
) -> None:
    """Verify the browser cache follows Playwright's platform default and its overrides."""
    # Given
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # When / Then
    assert playwright._registry_dir(platform) == expected


def test_playwright_old_builds_keep_the_newest_of_each_browser(tmp_path: Path) -> None:
    """Verify only builds older than each browser's newest are picked, and other entries stay."""
    # Given
    names = [
        "chromium-1228",
        "chromium-1254",
        "chromium-999",
        "chromium_headless_shell-1228",
        "chromium_headless_shell-1254",
        "ffmpeg-1011",
        "webkit-2370",
        "webkit-2388",
        ".links",
        "notes",
    ]
    builds = [tmp_path / name for name in names]

    # When
    old = playwright._old_builds(builds)

    # Then
    assert [path.name for path in old] == [
        "chromium-1228",
        "chromium-999",
        "chromium_headless_shell-1228",
        "webkit-2370",
    ]


def test_playwright_without_old_builds_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify an empty browser cache leaves the plugin unavailable instead of failing."""
    # Given a browser cache holding only the newest build
    (tmp_path / "chromium-1254").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))

    # When / Then
    assert playwright.PLUGIN.is_available is not None
    assert not playwright.PLUGIN.is_available()


def test_playwright_finds_old_builds_when_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the old builds come from the cache as it is when measured, not at import."""
    # Given a browser cache that gains an old build after import
    for name in ("chromium-1228", "chromium-1254"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "bin").write_bytes(b"x" * 10_000)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert playwright.PLUGIN.is_available is not None

    # When
    report = measure_plugin(playwright.PLUGIN, include_dangerous=False)

    # Then
    assert playwright.PLUGIN.is_available()
    assert report.actions[0].resolved_paths == [tmp_path / "chromium-1228"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "~/Library/Caches/CocoaPods"),
        ({"CP_HOME_DIR": "/pods"}, "/pods/cache"),
        ({"CP_HOME_DIR": "/pods", "CP_CACHE_DIR": "/cache"}, "/cache"),
        ({"CP_CACHE_DIR": "relative"}, "~/Library/Caches/CocoaPods"),
        ({"CP_HOME_DIR": "relative"}, "~/Library/Caches/CocoaPods"),
    ],
)
def test_cocoapods_cache_dir(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """Verify CP_CACHE_DIR wins over CP_HOME_DIR, which wins over the default."""
    # Given
    monkeypatch.delenv("CP_HOME_DIR", raising=False)
    monkeypatch.delenv("CP_CACHE_DIR", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # When / Then
    assert cocoapods._cache_dir() == expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "~/.cache/node/corepack"),
        ({"XDG_CACHE_HOME": "/xdg"}, "/xdg/node/corepack"),
        ({"XDG_CACHE_HOME": "/xdg", "COREPACK_HOME": "/cp"}, "/cp"),
    ],
)
def test_corepack_home(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str) -> None:
    """Verify COREPACK_HOME wins, then XDG_CACHE_HOME, on every platform."""
    # Given
    monkeypatch.delenv("COREPACK_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # When / Then
    assert corepack._home() == expected


def _simctl_listing(tmp_path: Path) -> str:
    return json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                    {"isAvailable": False, "dataPath": f"{tmp_path}/OLD/data"},
                ],
                "com.apple.CoreSimulator.SimRuntime.iOS-27-1": [
                    {"isAvailable": True, "dataPath": f"{tmp_path}/NEW/data"},
                    {"isAvailable": False},
                ],
            }
        }
    )


def test_simulators_size_counts_only_unavailable_devices(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the size covers each unavailable device's folder and skips available ones."""
    # Given
    for device, size in (("OLD", 10_000), ("NEW", 50_000)):
        (tmp_path / device / "data").mkdir(parents=True)
        (tmp_path / device / "data" / "disk").write_bytes(b"x" * size)
    mocker.patch(
        "purger.plugins.simulators.run_command",
        autospec=True,
        return_value=_completed(stdout=_simctl_listing(tmp_path)),
    )

    # When
    size = simulators._unavailable_size()

    # Then
    assert size == disk_usage([tmp_path / "OLD"])


def test_simulators_unavailable_until_first_launch_finishes(mocker: MockerFixture) -> None:
    """Verify simctl is never called while Xcode's first-launch setup is unfinished."""
    # Given
    run = mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=[_completed(), _completed(returncode=1)],
    )

    # When / Then
    assert simulators._is_available() is False
    assert run.call_args.args[0] == ["xcodebuild", "-checkFirstLaunchStatus"]


def test_simulators_never_run_xcodebuild_without_developer_tools(mocker: MockerFixture) -> None:
    """Verify the xcodebuild shim, which opens an install dialog, is skipped without tools."""
    # Given
    run = mocker.patch(
        "purger.runner.run_command", autospec=True, return_value=_completed(returncode=2)
    )

    # When / Then
    assert simulators._is_available() is False
    run.assert_called_once()
    assert run.call_args.args[0] == ["xcode-select", "-p"]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1.4.4\n", True),
        ("1.5.0\n", True),
        ("2.0.0-canary.1+abc\n", True),
        ("1.4.3\n", False),
        ("1.3.10\n", False),
        ("not a version\n", False),
    ],
)
def test_bun_available_only_with_cache_prune(
    mocker: MockerFixture, output: str, expected: bool
) -> None:
    """Verify bun before 1.4.4, whose cache prune silently does nothing, is skipped."""
    # Given
    mocker.patch(
        "purger.plugins.bun.run_command", autospec=True, return_value=_completed(stdout=output)
    )

    # When / Then
    assert bun._is_available() is expected


def test_bun_unavailable_when_version_cannot_run(mocker: MockerFixture) -> None:
    """Verify a bun that cannot run counts as unavailable."""
    # Given
    mocker.patch(
        "purger.plugins.bun.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["bun"]),
    )

    # When / Then
    assert bun._is_available() is False
