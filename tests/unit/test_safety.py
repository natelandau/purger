"""Tests for the path deletion guards."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.safety import (
    UnsafePathError,
    _device,
    _validated_location,
    delete_path,
    invoking_home,
    resolve_home_folder,
)


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path) -> Path:
    """Create a fake home directory."""
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture(name="precious")
def precious_fixture(tmp_path: Path) -> Path:
    """Create a directory outside home that must never be touched."""
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "file").write_text("keep")
    return precious


def test_validated_location_accepts_nested_path(home: Path) -> None:
    """Verify a path two levels below home passes."""
    # Given / When / Then
    _validated_location(home / "Library" / "Caches" / "x", home)


@pytest.mark.parametrize("relative", [".", "Library", ".cache"])
def test_validated_location_refuses_home_and_direct_children(home: Path, relative: str) -> None:
    """Verify home itself and its direct children are refused."""
    # Given
    target = home / relative

    # When / Then
    with pytest.raises(UnsafePathError, match="home"):
        _validated_location(target, home)


def test_validated_location_refuses_outside_home(home: Path, tmp_path: Path) -> None:
    """Verify a path outside home is refused and named in the message."""
    # Given
    target = tmp_path / "elsewhere" / "x"

    # When / Then
    with pytest.raises(UnsafePathError, match="elsewhere"):
        _validated_location(target, home)


def test_validated_location_refuses_dotdot_escape(home: Path, tmp_path: Path) -> None:
    """Verify `..` segments that leave home are refused."""
    # Given
    target = home / "Library" / "Caches" / ".." / ".." / ".." / "elsewhere" / "x"

    # When / Then
    with pytest.raises(UnsafePathError):
        _validated_location(target, home)


def test_validated_location_refuses_dotdot_to_direct_child(home: Path) -> None:
    """Verify `..` segments that collapse to a direct child of home are refused."""
    # Given
    target = home / "Library" / "Caches" / ".." / ".."

    # When / Then
    with pytest.raises(UnsafePathError):
        _validated_location(target, home)


@pytest.mark.parametrize("relative", ["", "Library/Caches/x", "~/Library/Caches/x"])
def test_validated_location_refuses_relative_path(
    home: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    """Verify a relative or unexpanded path is refused even when cwd is under home."""
    # Given
    (home / "Library" / "Caches").mkdir(parents=True)
    monkeypatch.chdir(home)

    # When / Then
    with pytest.raises(UnsafePathError, match="not absolute"):
        _validated_location(Path(relative), home)


@pytest.mark.parametrize("bad_home", [Path("/"), Path("/Users"), Path("relative/home"), Path("~")])
def test_validated_location_refuses_degenerate_home(bad_home: Path) -> None:
    """Verify a root, one-level, or relative home is refused."""
    # Given
    target = Path("/opt/homebrew/x")

    # When / Then
    with pytest.raises(UnsafePathError, match="home directory"):
        _validated_location(target, bad_home)


def test_validated_location_refuses_different_device(home: Path, mocker: MockerFixture) -> None:
    """Verify a target on another device than home is refused."""
    # Given
    target = home / "Library" / "Developer" / "x"
    target.parent.mkdir(parents=True)
    home_resolved = home.resolve()
    mocker.patch(
        "purger.safety._device",
        autospec=True,
        side_effect=lambda path: 1 if path == home_resolved else 2,
    )

    # When / Then
    with pytest.raises(UnsafePathError, match="different device"):
        _validated_location(target, home)


def test_validated_location_refuses_path_through_symlink_to_other_volume(
    home: Path, tmp_path: Path
) -> None:
    """Verify a path reached through a symlink leaving home is refused."""
    # Given
    external = tmp_path / "external"
    external.mkdir()
    (home / "Library").mkdir()
    (home / "Library" / "Developer").symlink_to(external)
    target = home / "Library" / "Developer" / "DerivedData" / "x"

    # When / Then
    with pytest.raises(UnsafePathError):
        _validated_location(target, home)


def test_validated_location_refuses_symlink_that_is_direct_child_of_home(
    home: Path, precious: Path
) -> None:
    """Verify a symlink directly under home is refused as a direct child."""
    # Given
    link = home / "link"
    link.symlink_to(precious)

    # When / Then
    with pytest.raises(UnsafePathError):
        _validated_location(link, home)


def test_validated_location_accepts_symlink_inside_home_pointing_outside(
    home: Path, precious: Path
) -> None:
    """Verify the link itself is judged, not its target."""
    # Given
    (home / "Library" / "Caches").mkdir(parents=True)
    link = home / "Library" / "Caches" / "link"
    link.symlink_to(precious)

    # When / Then
    _validated_location(link, home)


def test_validated_location_works_when_home_is_a_symlink(tmp_path: Path) -> None:
    """Verify a symlinked home is resolved before comparison."""
    # Given
    real = tmp_path / "real"
    (real / "Library").mkdir(parents=True)
    home = tmp_path / "homelink"
    home.symlink_to(real)

    # When / Then
    _validated_location(home / "Library" / "Caches", home)
    with pytest.raises(UnsafePathError):
        _validated_location(home / "Library", home)


def test_delete_path_unlinks_symlink_without_traversing(home: Path, precious: Path) -> None:
    """Verify a symlink match is removed and its target is left intact."""
    # Given
    (home / "Library" / "Caches").mkdir(parents=True)
    link = home / "Library" / "Caches" / "link"
    link.symlink_to(precious)

    # When
    delete_path(link, home)

    # Then
    assert not link.is_symlink()
    assert (precious / "file").read_text() == "keep"


def test_delete_path_unlinks_dangling_symlink(home: Path, tmp_path: Path) -> None:
    """Verify a broken symlink is still removed."""
    # Given
    (home / "Library" / "Caches").mkdir(parents=True)
    link = home / "Library" / "Caches" / "dangling"
    link.symlink_to(tmp_path / "nowhere")

    # When
    delete_path(link, home)

    # Then
    assert not link.is_symlink()


def test_delete_path_removes_file(home: Path) -> None:
    """Verify a regular file is removed."""
    # Given
    (home / "Library" / "Caches").mkdir(parents=True)
    target = home / "Library" / "Caches" / "f.txt"
    target.write_text("x")

    # When
    delete_path(target, home)

    # Then
    assert not target.exists()


def test_delete_path_removes_directory_tree(home: Path, precious: Path) -> None:
    """Verify a directory is removed without following inner symlinks."""
    # Given
    tree = home / "Library" / "Caches" / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "a.txt").write_text("a")
    (tree / "sub" / "link").symlink_to(precious)

    # When
    delete_path(tree, home)

    # Then
    assert not tree.exists()
    assert (precious / "file").read_text() == "keep"


def test_delete_path_missing_is_noop(home: Path) -> None:
    """Verify a path that does not exist is a no-op."""
    # Given
    target = home / "Library" / "Caches" / "missing"

    # When / Then
    delete_path(target, home)


def test_delete_path_refuses_unsafe_path_without_deleting(home: Path, tmp_path: Path) -> None:
    """Verify an unsafe path raises and the file survives."""
    # Given
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    # When / Then
    with pytest.raises(UnsafePathError):
        delete_path(outside, home)
    assert outside.exists()


def test_delete_path_refuses_symlinked_parent_inside_home(home: Path) -> None:
    """Verify a path whose parent is a symlink to elsewhere in home is refused."""
    # Given DerivedData is a symlink to ~/Documents, which holds user files
    documents = home / "Documents"
    (documents / "Project").mkdir(parents=True)
    (home / "Library" / "Developer" / "Xcode").mkdir(parents=True)
    (home / "Library" / "Developer" / "Xcode" / "DerivedData").symlink_to(documents)
    target = home / "Library" / "Developer" / "Xcode" / "DerivedData" / "Project"

    # When / Then
    with pytest.raises(UnsafePathError, match="symlink"):
        delete_path(target, home)
    assert (documents / "Project").exists()


def test_validated_location_refuses_path_lexically_outside_home(home: Path, tmp_path: Path) -> None:
    """Verify a path not written under home is refused even before it is resolved."""
    # Given a path that only reaches home through a symlink outside it
    (home / "Library" / "Caches").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(home / "Library")

    # When / Then
    with pytest.raises(UnsafePathError, match="not inside the home directory"):
        _validated_location(alias / "Caches" / "x", home)


def test_delete_path_refuses_tree_spanning_devices(home: Path, mocker: MockerFixture) -> None:
    """Verify a directory tree holding a mount point is refused and left intact."""
    # Given a cache tree whose `mnt` subdirectory sits on another device
    tree = home / "Library" / "Caches" / "tree"
    (tree / "mnt").mkdir(parents=True)
    (tree / "mnt" / "data").write_text("keep")
    mocker.patch(
        "purger.safety._device",
        autospec=True,
        side_effect=lambda path: 2 if path.name == "mnt" else 1,
    )

    # When / Then
    with pytest.raises(UnsafePathError, match="different device"):
        delete_path(tree, home)
    assert (tree / "mnt" / "data").read_text() == "keep"


def test_device_wraps_os_errors(mocker: MockerFixture) -> None:
    """Verify an unreadable path makes the device check fail closed with UnsafePathError."""
    # Given a path whose lstat is denied
    path = mocker.create_autospec(Path, instance=True)
    path.lstat.side_effect = PermissionError("denied")

    # When / Then
    with pytest.raises(UnsafePathError, match="denied"):
        _device(path)


def test_device_missing_path_is_none(tmp_path: Path) -> None:
    """Verify a missing path has no device rather than raising."""
    # Given / When / Then
    assert _device(tmp_path / "missing") is None


def test_invoking_home_prefers_the_invoking_user(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify invoking_home uses the invoking user's home when it is known."""
    # Given a known invoking user's home
    mocker.patch("purger.safety.find_user_home_dir", autospec=True, return_value=tmp_path)

    # When / Then
    assert invoking_home() == tmp_path


def test_invoking_home_falls_back_to_current_user(mocker: MockerFixture) -> None:
    """Verify invoking_home falls back to the current user's home when lookup fails."""
    # Given an invoking user whose home cannot be looked up
    mocker.patch("purger.safety.find_user_home_dir", autospec=True, return_value=None)

    # When / Then
    assert invoking_home() == Path.home()


def test_resolve_home_folder_returns_the_resolved_folder(home: Path, tmp_path: Path) -> None:
    """Verify a folder inside home resolves, including one reached through a symlinked home."""
    # Given
    (home / "repos").mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(home)

    # When
    resolved = resolve_home_folder(linked_home / "repos", home_resolved=home.resolve())

    # Then
    assert resolved == (home / "repos").resolve()


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("relative", "not an absolute path"),
        ("home", "not inside your home folder"),
        ("outside", "not inside your home folder"),
        ("dotdot", "not inside your home folder"),
        ("missing", "not an existing folder"),
        ("file", "not an existing folder"),
    ],
)
def test_resolve_home_folder_explains_a_rejection(
    home: Path, precious: Path, kind: str, reason: str
) -> None:
    """Verify each unusable folder is rejected with a reason naming the problem."""
    # Given
    (home / "repos").mkdir()
    (home / "notes.txt").write_text("x")
    path = {
        "relative": Path("repos"),
        "home": home,
        "outside": precious,
        "dotdot": home / "repos" / "..",
        "missing": home / "nowhere",
        "file": home / "notes.txt",
    }[kind]

    # When
    result = resolve_home_folder(path, home_resolved=home.resolve())

    # Then
    assert isinstance(result, str)
    assert reason in result


def test_resolve_home_folder_rejects_another_device(home: Path, mocker: MockerFixture) -> None:
    """Verify a folder on another device than home is rejected."""
    # Given
    (home / "repos").mkdir()
    home_resolved = home.resolve()
    mocker.patch(
        "purger.safety._device",
        autospec=True,
        side_effect=lambda path: 1 if path == home_resolved else 2,
    )

    # When
    result = resolve_home_folder(home / "repos", home_resolved=home_resolved)

    # Then
    assert result == "it is on a different device than your home folder"


def test_resolve_home_folder_reports_an_unreadable_device(
    home: Path, mocker: MockerFixture
) -> None:
    """Verify a folder whose device cannot be checked is rejected with the OS error."""
    # Given
    (home / "repos").mkdir()
    real_lstat = Path.lstat

    def denied(self: Path) -> object:
        if self.name == "repos":
            raise PermissionError(13, "Permission denied")
        return real_lstat(self)

    mocker.patch.object(Path, "lstat", denied)

    # When
    result = resolve_home_folder(home / "repos", home_resolved=home.resolve())

    # Then
    assert result == "it cannot be read (Permission denied)"
