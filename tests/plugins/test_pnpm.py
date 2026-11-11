"""Tests for the pnpm plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugin import SizeComputedBy
from purger.plugins import pnpm
from purger.sizing import disk_usage
from tests.conftest import make_completed


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
            make_completed(stdout=f"{tmp_path}\n"),
            make_completed(stdout="Dry run: 1 directories would be deleted.\nv11/metadata\n"),
        ],
    )
    action = next(a for a in pnpm.PLUGIN.actions if a.name == "metadata-prune")
    assert isinstance(action.size, SizeComputedBy)

    # When
    size = action.size.function()

    # Then
    assert size == disk_usage([stale])


_NODE = "/opt/homebrew/bin/node"


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/Users/me/Library/pnpm/pnpm", "install"], True),
        ([_NODE, "/Users/me/.cache/node/corepack/pnpm/9.1.0/bin/pnpm.cjs", "i"], True),
        ([_NODE, "/opt/homebrew/bin/npm", "install"], False),
        (["/Users/me/Library/pnpm/pnpm", "run", "dev"], False),
    ],
)
def test_pnpm_skips_while_pnpm_runs(cmdline: list[str], expected: bool) -> None:
    """Verify pnpm counts as running however it was launched, and nothing else does."""
    # Given
    assert pnpm.PLUGIN.skip_while_running is not None

    # When / Then
    assert pnpm.PLUGIN.skip_while_running(cmdline) is expected
