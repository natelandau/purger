"""Plugin for Podman images, containers, build cache, and volumes."""

import json
import logging
import os
from functools import partial

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import Action, Plugin, Tier

_DF_CMD = ("podman", "system", "df", "--format", "json")
_REMOVABLE = frozenset({"Images", "Containers"})

logger = logging.getLogger(__name__)


def _json_output(argv: list[str]) -> object | None:
    """Run a podman query and parse its JSON output, or return None when either step fails."""
    try:
        result = run_command(argv, check=False, timeout=10)
    except ShellCommandError as exc:
        logger.debug("%s failed: %s", " ".join(argv), exc)
        return None
    if not result.ok:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.debug("%s printed invalid JSON", " ".join(argv))
        return None


def _remote_is_local() -> bool:
    """Report whether the remote connection podman uses reaches a service on this machine.

    Podman picks the connection from `CONTAINER_HOST` or `CONTAINER_CONNECTION`,
    then the default connection. A `podman machine` VM counts as local, and so does
    a unix socket. Any other TCP or SSH URL counts as remote, even on loopback,
    since it can tunnel to another host.

    Returns:
        bool: True when the connection is a podman machine or a unix socket.
    """
    url = os.environ.get("CONTAINER_HOST", "")
    wanted = os.environ.get("CONTAINER_CONNECTION", "")
    # Which variable wins when both are set is undocumented, so both must be local.
    if url and not url.startswith("unix://"):
        return False
    if url and not wanted:
        return True
    connections = _json_output(["podman", "system", "connection", "list", "--format", "json"])
    if not isinstance(connections, list):
        return False
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        if (conn.get("Name") == wanted) if wanted else conn.get("Default") is True:
            return conn.get("IsMachine") is True or str(conn.get("URI", "")).startswith("unix://")
    return False


def _is_available() -> bool:
    """Report whether a Podman service on this machine answers.

    On macOS that needs a running podman machine. A service on another host is
    never available, so a prune cannot reach it.

    Returns:
        bool: True when `podman info` succeeds and its service is local.
    """
    info = _json_output(["podman", "info", "--format", "json"])
    if not isinstance(info, dict):
        return False
    host = info.get("host")
    if isinstance(host, dict) and host.get("serviceIsRemote") is False:
        return True
    if _remote_is_local():
        return True
    logger.debug("skipping podman: its connection does not reach this machine")
    return False


def _reclaimable_from(output: str, types: frozenset[str]) -> int:
    """Sum the reclaimable bytes of the given `podman system df` types.

    Args:
        output: The JSON array from `podman system df --format json`.
        types: Row types to include, such as `Images` or `Local Volumes`.

    Returns:
        int: Total reclaimable bytes.
    """
    return sum(row["RawReclaimable"] for row in json.loads(output) if row.get("Type") in types)


PLUGIN = Plugin(
    name="podman",
    description="Podman images, containers, build cache, and volumes",
    requires="podman",
    is_available=_is_available,
    size_cmd=_DF_CMD,
    parse_size=partial(_reclaimable_from, types=_REMOVABLE),
    actions=(
        Action(
            name="system",
            tier=Tier.CAUTION,
            help=(
                "Remove all unused images, stopped containers, pods, networks, and build cache. "
                "Images must be pulled again before next use."
            ),
            command=("podman", "system", "prune", "--all", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=_REMOVABLE),
        ),
        Action(
            name="volumes",
            tier=Tier.DANGEROUS,
            help="Remove all unused volumes. Volume data such as databases is lost permanently.",
            command=("podman", "volume", "prune", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=frozenset({"Local Volumes"})),
            outside_footprint=True,
        ),
    ),
)
