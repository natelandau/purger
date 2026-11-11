"""Tests for the thumbnails plugin."""

from purger.plugins import thumbnails


def test_thumbnails_clean_each_size_folder() -> None:
    """Verify the thumbnail action removes the size folders inside the thumbnail cache."""
    # Given
    action = thumbnails.PLUGIN.actions[0]

    # When / Then
    assert isinstance(action.paths, tuple)
    assert len(action.paths) == 1
    assert action.paths[0].endswith("/thumbnails/*")
