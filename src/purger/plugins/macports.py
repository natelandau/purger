"""Plugin for MacPorts build files, distfiles, and inactive ports."""

from pathlib import Path

from nclutils.sh import which

from purger.plugin import Action, Plugin, Tier

# sudo's secure_path omits the MacPorts bin folder, so the command uses an absolute
# path. The prefix follows the `port` on PATH, since MacPorts can install elsewhere.
_PORT = which("port") or Path("/opt/local/bin/port")
_PREFIX = _PORT.parent.parent

PLUGIN = Plugin(
    name="macports",
    description="MacPorts build files, distfiles, and inactive ports",
    platforms=frozenset({"darwin"}),
    requires="port",
    paths=(
        f"{_PREFIX}/var/macports/build",
        f"{_PREFIX}/var/macports/distfiles",
        f"{_PREFIX}/var/macports/logs",
    ),
    actions=(
        Action(
            name="clean",
            tier=Tier.SAFE,
            help="Remove build directories, source archives, and logs of installed ports.",
            command=(str(_PORT), "-N", "clean", "--all", "installed"),
            needs_sudo=True,
        ),
        Action(
            name="reclaim",
            tier=Tier.CAUTION,
            help=(
                "Uninstall inactive ports and unrequested ports nothing depends on, "
                "and remove unused distfiles. Removed ports must be installed again to return."
            ),
            command=(str(_PORT), "-N", "reclaim"),
            needs_sudo=True,
        ),
    ),
)
