"""Plugin for the apt package cache."""

from purger.plugin import Action, Plugin, Tier

PLUGIN = Plugin(
    name="apt",
    description="apt downloaded package files",
    platforms=frozenset({"linux"}),
    requires="apt-get",
    # Only the package files, so the clean action's size accounts for the whole footprint.
    paths=("/var/cache/apt/archives/*.deb",),
    actions=(
        Action(
            name="autoclean",
            tier=Tier.SAFE,
            help="Remove downloaded packages that can no longer be downloaded again.",
            command=("apt-get", "autoclean"),
            needs_sudo=True,
        ),
        Action(
            name="clean",
            tier=Tier.SAFE,
            help="Remove all downloaded package files. Reinstalls download them again.",
            command=("apt-get", "clean"),
            needs_sudo=True,
            paths=("/var/cache/apt/archives/*.deb",),
        ),
    ),
)
