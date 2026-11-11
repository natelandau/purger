"""Tests for the ruby plugin."""

from pathlib import Path

import pytest
from nclutils.sh import ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugins import ruby
from purger.sizing import disk_usage, measure_plugin
from tests.conftest import make_completed

_DRY_RUN = (
    "Cleaning up installed gems...\n"
    "Dry Run Mode: Would uninstall rake-13.0.6\n"
    "Dry Run Mode: Would uninstall nokogiri-1.15.0-arm64-darwin\n"
    "Clean up complete\n"
)


def _install_gem(gem_dir: Path, full_name: str, size: int) -> None:
    """Lay out one gem version the way RubyGems stores it."""
    (gem_dir / "gems" / full_name).mkdir(parents=True)
    (gem_dir / "gems" / full_name / "lib.rb").write_bytes(b"x" * size)
    (gem_dir / "specifications").mkdir(exist_ok=True)
    (gem_dir / "specifications" / f"{full_name}.gemspec").write_bytes(b"x" * 100)
    (gem_dir / "cache").mkdir(exist_ok=True)
    (gem_dir / "cache" / f"{full_name}.gem").write_bytes(b"x" * size)
    ext = gem_dir / "extensions" / "arm64-darwin-23" / "3.3.0" / full_name
    ext.mkdir(parents=True)
    (ext / "ext.bundle").write_bytes(b"x" * size)


def _mock_gem(mocker: MockerFixture, gem_dir: Path, dry_run: str = _DRY_RUN) -> None:
    """Answer the gem queries the plugin and the sizing code run."""
    outputs = {
        ruby.GEM_DIRECTORY_COMMAND: f"{gem_dir}\n",
        (*ruby.GEM_CLEANUP_COMMAND, "-n"): dry_run,
    }
    for target in ("purger.plugins.ruby.run_command", "purger.sizing.run_command"):
        mocker.patch(
            target,
            autospec=True,
            side_effect=lambda argv, **_: make_completed(stdout=outputs[tuple(argv)]),
        )


def test_ruby_old_version_names_read_the_dry_run() -> None:
    """Verify only the gems the dry run would uninstall are read, platform suffix included."""
    # When / Then
    assert ruby._old_version_names(_DRY_RUN) == ["rake-13.0.6", "nokogiri-1.15.0-arm64-darwin"]


def test_ruby_old_version_names_drop_repeats() -> None:
    """Verify a version the dry run lists twice is read once."""
    # Given
    output = (
        "Dry Run Mode: Would uninstall rake-13.0.6\nDry Run Mode: Would uninstall rake-13.0.6\n"
    )

    # When / Then
    assert ruby._old_version_names(output) == ["rake-13.0.6"]


def test_ruby_runs_when_user_owns_gem_dir(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a GEM_HOME the user owns, as a version manager creates, is cleaned."""
    # Given
    _mock_gem(mocker, tmp_path)
    mocker.patch("purger.plugins.ruby.invoking_home", return_value=tmp_path)

    # When / Then
    assert ruby.gem_directory_is_owned_by_user() is True


def test_ruby_skipped_when_gem_dir_owned_by_another_user(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify a system Ruby's root-owned GEM_HOME is skipped, since cleaning it needs sudo."""
    # Given a home directory whose owner differs from the GEM_HOME owner
    _mock_gem(mocker, Path("/"))
    mocker.patch("purger.plugins.ruby.invoking_home", return_value=tmp_path)

    # When / Then
    assert ruby.gem_directory_is_owned_by_user() is False


def test_ruby_skipped_when_gem_dir_missing(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a GEM_HOME that does not exist yet skips the plugin."""
    # Given
    _mock_gem(mocker, tmp_path / "missing")

    # When / Then
    assert ruby.gem_directory_is_owned_by_user() is False


def test_ruby_skipped_when_gem_query_fails(mocker: MockerFixture) -> None:
    """Verify a failed `gem env gemdir` skips the plugin instead of raising."""
    # Given
    mocker.patch(
        "purger.plugins.ruby.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["gem"]),
    )

    # When / Then
    assert ruby.gem_directory_is_owned_by_user() is False


def test_ruby_measures_only_old_versions(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the action sizes every file of the old versions and the total size is all of GEM_HOME."""
    # Given two old versions and one current version
    _install_gem(tmp_path, "rake-13.0.6", 10_000)
    _install_gem(tmp_path, "nokogiri-1.15.0-arm64-darwin", 20_000)
    _install_gem(tmp_path, "rake-13.2.1", 50_000)
    _mock_gem(mocker, tmp_path)

    # When
    report = measure_plugin(
        ruby.PLUGIN, include_dangerous=False, settings=Config().settings_for(ruby.PLUGIN)
    )

    # Then
    (action,) = report.actions
    current = {
        tmp_path / "gems" / "rake-13.2.1",
        tmp_path / "specifications" / "rake-13.2.1.gemspec",
        tmp_path / "cache" / "rake-13.2.1.gem",
        tmp_path / "extensions" / "arm64-darwin-23" / "3.3.0" / "rake-13.2.1",
    }
    assert len(action.resolved_paths) == 8
    assert current.isdisjoint(action.resolved_paths)
    assert action.size_bytes == disk_usage(action.resolved_paths)
    assert report.total_size_bytes == disk_usage([tmp_path])


def test_ruby_measures_zero_when_nothing_is_old(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a dry run that lists no gems sizes the action at zero."""
    # Given
    _install_gem(tmp_path, "rake-13.2.1", 50_000)
    _mock_gem(mocker, tmp_path, dry_run="Cleaning up installed gems...\nClean up complete\n")

    # When
    report = measure_plugin(
        ruby.PLUGIN, include_dangerous=False, settings=Config().settings_for(ruby.PLUGIN)
    )

    # Then
    (action,) = report.actions
    assert action.resolved_paths == []
    assert action.size_bytes == 0


@pytest.mark.parametrize(
    ("argv", "running"),
    [
        (["gem", "install", "rails"], True),
        (["/usr/bin/ruby", "/usr/local/bin/bundle", "install"], True),
        (["bundler", "update"], True),
        (["ruby", "app.rb"], False),
        (["gemini"], False),
    ],
)
def test_ruby_skipped_while_gem_or_bundler_runs(argv: list[str], *, running: bool) -> None:
    """Verify a running gem or Bundler pauses cleanup, and other Ruby programs do not."""
    # When / Then
    assert ruby.PLUGIN.skip_while_running is not None
    assert ruby.PLUGIN.skip_while_running(argv) is running


def test_ruby_gem_dir_resolves_symlinks(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a symlinked GEM_HOME is measured at its real path, which deletion accepts."""
    # Given a GEM_HOME reached through a symlink
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _mock_gem(mocker, link)

    # When / Then
    assert ruby._gem_directory() == real


def test_ruby_asks_for_the_gem_directory_once_per_command(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the availability check and the total size share one `gem env gemdir` run."""
    # Given
    _install_gem(tmp_path, "rake-13.2.1", 50_000)
    gem = mocker.patch(
        "purger.plugins.ruby.run_command",
        autospec=True,
        side_effect=lambda argv, **_: make_completed(
            stdout=f"{tmp_path}\n" if tuple(argv) == ruby.GEM_DIRECTORY_COMMAND else ""
        ),
    )

    # When
    assert ruby.PLUGIN.skip_unless is not None
    ruby.PLUGIN.skip_unless()
    report = measure_plugin(
        ruby.PLUGIN, include_dangerous=False, settings=Config().settings_for(ruby.PLUGIN)
    )

    # Then
    asked = [
        call for call in gem.call_args_list if tuple(call.args[0]) == ruby.GEM_DIRECTORY_COMMAND
    ]
    assert len(asked) == 1
    assert report.total_size_bytes == disk_usage([tmp_path])
