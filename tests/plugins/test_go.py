"""Tests for the go plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugin import RiskLevel, RunCommand
from purger.plugins import go
from purger.sizing import disk_usage, measure_action
from tests.conftest import make_completed


def _action(name: str):  # noqa: ANN202
    return next(a for a in go.PLUGIN.actions if a.name == name)


def test_go_build_cache_size_counts_only_hashed_folders(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the build-cache size leaves out the fuzz corpus, which `go clean -cache` keeps."""
    # Given a cache with a hashed folder, a fuzz corpus, and a stray file
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / "obj").write_bytes(b"x" * 10_000)
    (tmp_path / "fuzz").mkdir()
    (tmp_path / "fuzz" / "corpus").write_bytes(b"x" * 50_000)
    run = mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        return_value=make_completed(stdout=f"{tmp_path}\n"),
    )

    # When
    report = measure_action(_action("build-cache"), settings=Config().settings_for(go.PLUGIN))

    # Then
    assert run.call_args.args[0] == ["go", "env", "GOCACHE"]
    assert report.size_bytes == disk_usage([tmp_path / "ab"])


def test_go_modcache_size_counts_the_whole_folder(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the module cache size covers the whole folder `go env GOMODCACHE` prints."""
    # Given
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "src").write_bytes(b"x" * 10_000)
    run = mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        return_value=make_completed(stdout=f"{tmp_path}\n"),
    )

    # When
    report = measure_action(_action("modcache"), settings=Config().settings_for(go.PLUGIN))

    # Then
    assert run.call_args.args[0] == ["go", "env", "GOMODCACHE"]
    assert report.size_bytes == disk_usage([tmp_path])


@pytest.mark.parametrize(
    ("name", "argv", "risk"),
    [
        ("build-cache", ("go", "clean", "-cache"), RiskLevel.SAFE),
        ("modcache", ("go", "clean", "-modcache"), RiskLevel.CAUTION),
    ],
)
def test_go_cleanups_run_go_clean_without_sudo(
    name: str, argv: tuple[str, ...], risk: RiskLevel
) -> None:
    """Verify each action runs its `go clean` command as the user, at the expected risk."""
    # Given
    action = _action(name)

    # When / Then
    assert isinstance(action.cleanup, RunCommand)
    assert action.cleanup.argv == argv
    assert action.cleanup.with_sudo is False
    assert action.risk_level is risk
