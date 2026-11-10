"""Tests for plugin size-parsing and availability hooks."""

from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from nclutils.sh import CompletedCommand, ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.output import is_partial
from purger.plugins import (
    ansible,
    apt,
    brew,
    conda,
    docker,
    maestro,
    mise,
    npm,
    pacman,
    pip,
    pnpm,
    podman,
    pre_commit,
    rip2,
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

    # Then the footprint is cleanup's total plus only the version cleanup leaves
    assert report.footprint_bytes == 1024 + disk_usage([current])
    sizes = {r.action.name: r.size_bytes for r in report.actions}
    assert sizes == {"cleanup": 1024, "autoremove": disk_usage([old, current])}


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


@pytest.mark.parametrize(
    ("leftover", "cmdline", "expected"),
    [
        (True, ["/usr/bin/python3", "/Users/me/.local/bin/purger", "clean", "ansible"], True),
        (False, ["/bin/zsh"], False),
        (True, ["/usr/bin/python3", "/Users/me/.local/bin/ansible-playbook", "site.yml"], False),
    ],
    ids=["idle-with-leftovers", "nothing-to-clean", "ansible-running"],
)
def test_ansible_available_only_when_idle_with_leftovers(
    leftover: bool,
    cmdline: list[str],
    expected: bool,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Verify the plugin applies only with leftovers to clean and no ansible process using them."""
    # Given
    if leftover:
        stale = tmp_path / ".ansible" / "tmp" / "ansible-tmp-1747399837.59-86917-2270"
        stale.mkdir(parents=True)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    mocker.patch.object(ansible, "_TMP_PATHS", ("~/.ansible/tmp/*",))
    mocker.patch.object(ansible, "_GALAXY_PATHS", ("~/.ansible/galaxy_cache/*",))
    process = mocker.Mock(info={"cmdline": cmdline})
    mocker.patch("purger.plugins._shared.psutil.process_iter", return_value=[process])
    assert ansible.PLUGIN.is_available is not None

    # When / Then
    assert ansible.PLUGIN.is_available() is expected


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


@pytest.mark.parametrize(
    ("has_output", "cmdline", "expected"),
    [
        (True, ["/bin/zsh"], True),
        (False, ["/bin/zsh"], False),
        (True, ["/usr/bin/java", "-classpath", "/x/lib/*", "maestro.cli.AppKt", "test"], False),
        (True, None, True),
    ],
    ids=["idle-with-output", "nothing-to-clean", "maestro-running", "unreadable-argv"],
)
def test_maestro_available_only_when_idle_with_output(
    has_output: bool,
    cmdline: list[str] | None,
    expected: bool,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Verify the plugin applies only with output to clean and no Maestro run writing to it."""
    # Given
    if has_output:
        (tmp_path / ".maestro" / "tests" / "2026-11-10_041643").mkdir(parents=True)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    mocker.patch.object(maestro, "_TEST_OUTPUT_PATHS", ("~/.maestro/tests/*",))
    process = mocker.Mock(info={"cmdline": cmdline})
    mocker.patch("purger.plugins._shared.psutil.process_iter", return_value=[process])
    assert maestro.PLUGIN.is_available is not None

    # When / Then
    assert maestro.PLUGIN.is_available() is expected


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


def test_podman_unavailable_when_info_fails(mocker: MockerFixture) -> None:
    """Verify podman is skipped when it does not answer, such as a stopped machine on macOS."""
    # Given
    mocker.patch(
        "purger.runner.run_command", autospec=True, return_value=_completed(returncode=125)
    )
    assert podman.PLUGIN.is_available is not None

    # When / Then
    assert podman.PLUGIN.is_available() is False


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
    assert len(action.paths) == 1
    assert action.paths[0].endswith("/thumbnails/*")
