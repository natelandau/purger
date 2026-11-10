"""Plugin for MacPorts build files, distfiles, and inactive ports."""

from purger.plugin import Action, Plugin, Tier

# sudo's secure_path omits /opt/local/bin, so the default MacPorts prefix is spelled out.
_PORT = "/opt/local/bin/port"

PLUGIN = Plugin(
    name="macports",
    description="MacPorts build files, distfiles, and inactive ports",
    platforms=frozenset({"darwin"}),
    requires="port",
    paths=(
        "/opt/local/var/macports/build",
        "/opt/local/var/macports/distfiles",
        "/opt/local/var/macports/logs",
    ),
    actions=(
        Action(
            name="clean",
            tier=Tier.SAFE,
            help="Remove build directories, source archives, and logs of installed ports.",
            command=(_PORT, "-N", "clean", "--all", "installed"),
            needs_sudo=True,
        ),
        Action(
            name="reclaim",
            tier=Tier.CAUTION,
            help=(
                "Uninstall inactive ports and unrequested ports nothing depends on, "
                "and remove unused distfiles. Removed ports must be installed again to return."
            ),
            command=(_PORT, "-N", "reclaim"),
            needs_sudo=True,
        ),
    ),
)
