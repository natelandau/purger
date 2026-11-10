"""Guard filesystem deletions so purger can only remove caches inside the user's home."""

import shutil
from pathlib import Path

from nclutils.fs import find_user_home_dir


class UnsafePathError(Exception):
    """Raised when a path is not safe to delete."""


def invoking_home() -> Path:
    """Return the home directory of the user who ran purger.

    Measuring and deleting share this lookup so the paths that are measured are
    the ones that get removed. Under sudo, this is the home of `SUDO_USER`.

    Returns:
        Path: The invoking user's home directory.

    Raises:
        RuntimeError: If no home directory can be determined.
    """
    return find_user_home_dir() or Path.home()


def _device(path: Path) -> int | None:
    """Return the device id of a path without following a final symlink, or None if missing.

    Raises:
        UnsafePathError: If the path exists but cannot be inspected, so the
            device check fails closed.
    """
    try:
        return path.lstat().st_dev
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = f"Refusing to delete {path}: cannot check its device: {exc}"
        raise UnsafePathError(msg) from exc


def _symlinked_parent(path: Path, root: Path) -> Path | None:
    """Return the first symlinked directory between path and root, or None."""
    for parent in path.parents:
        if parent == root:
            return None
        if parent.is_symlink():
            return parent
    return None


def _home_root(path: Path, home: Path, home_resolved: Path) -> Path | None:
    """Return the spelling of home that path lies under, literal or resolved, or None."""
    return next((root for root in (home, home_resolved) if path.is_relative_to(root)), None)


def has_symlinked_parent(path: Path, *, home: Path, home_resolved: Path) -> bool:
    """Report whether a path passes through a symlinked directory below home.

    Measuring uses this to leave out matches that deletion would refuse.

    Args:
        path: The path to check.
        home: The invoking user's home directory.
        home_resolved: home with symlinks resolved, passed in so a caller checking
            many paths resolves it once.

    Returns:
        bool: True when a parent between home and the path is a symlink.
    """
    root = _home_root(path=path, home=home, home_resolved=home_resolved)
    return root is not None and _symlinked_parent(path=path, root=root) is not None


def _assert_no_symlinked_parent(path: Path, home: Path, home_resolved: Path) -> None:
    """Refuse a path that passes through a symlinked directory below home.

    Such a link can point anywhere inside home, so where the path resolves is no
    proof that it names a cache.
    """
    if ".." in path.parts:
        msg = f"Refusing to delete {path}: it contains '..'"
        raise UnsafePathError(msg)
    root = _home_root(path=path, home=home, home_resolved=home_resolved)
    if root is None:
        msg = f"Refusing to delete {path}: it is not inside the home directory {home}"
        raise UnsafePathError(msg)
    parent = _symlinked_parent(path=path, root=root)
    if parent is not None:
        msg = f"Refusing to delete {path}: {parent} is a symlink, so it may lead elsewhere"
        raise UnsafePathError(msg)


def _assert_single_device(tree: Path) -> None:
    """Refuse a directory tree with a mount point inside it, so removal stays on one device."""
    device = _device(tree)
    for directory, subdirs, _files in tree.walk():
        for name in subdirs:
            if _device(directory / name) != device:
                msg = f"Refusing to delete {tree}: {directory / name} is on a different device"
                raise UnsafePathError(msg)


def _validated_location(path: Path, home: Path) -> Path:
    """Validate a deletion target and return the real location that was judged.

    This is the last line of defense against removing files outside home, home
    itself, or a direct child of home such as `~/Library`. A symlink is judged by
    where the link lives, not where it points. Relative paths, paths with `..`,
    paths reached through a symlinked directory below home, a relative or
    degenerate home (such as `/`), and targets on another device than home are
    refused.

    Raises:
        UnsafePathError: If home or the path is degenerate, or the path is not a
            deletable location strictly inside home on the same device.
    """
    if not home.is_absolute():
        msg = f"Refusing to delete {path}: the home directory {home} is not absolute"
        raise UnsafePathError(msg)
    home_resolved = home.resolve()
    # Anchor plus at least two components, so "/" and "/Users" are never treated as a home.
    if len(home_resolved.parts) < 3:  # noqa: PLR2004
        msg = f"Refusing to delete {path}: {home_resolved} is not a plausible home directory"
        raise UnsafePathError(msg)
    if not path.is_absolute():
        msg = f"Refusing to delete {path}: the path is not absolute"
        raise UnsafePathError(msg)
    _assert_no_symlinked_parent(path=path, home=home, home_resolved=home_resolved)

    # A symlink is judged by where the link lives, since deletion unlinks it without traversing.
    location = path.parent.resolve() / path.name if path.is_symlink() else path.resolve()

    if not location.is_relative_to(home_resolved):
        msg = f"Refusing to delete {path}: resolves to {location}, outside the home directory"
        raise UnsafePathError(msg)
    if location == home_resolved:
        msg = f"Refusing to delete {path}: it is the home directory itself"
        raise UnsafePathError(msg)
    if location.parent == home_resolved:
        msg = f"Refusing to delete {path}: it is a direct child of the home directory"
        raise UnsafePathError(msg)

    location_device = _device(location)
    if location_device is not None and location_device != _device(home_resolved):
        msg = f"Refusing to delete {path}: it is on a different device than the home directory"
        raise UnsafePathError(msg)
    return location


def delete_path(path: Path, home: Path) -> None:
    """Delete a file, symlink, or directory tree after confirming it is safe.

    Symlinks are unlinked and never followed, so a link to data elsewhere cannot cause
    that data to be removed. A directory tree holding a mount point is refused. A path
    that does not exist is a no-op.

    Args:
        path: The path to delete.
        home: The invoking user's home directory.

    Raises:
        UnsafePathError: If the path or home is unsafe.
    """
    location = _validated_location(path=path, home=home)

    # Delete the location that was validated so a swapped ancestor cannot redirect the removal.
    if location.is_symlink():
        location.unlink()
    elif location.is_dir():
        _assert_single_device(location)
        shutil.rmtree(location)
    elif location.exists():
        location.unlink()
