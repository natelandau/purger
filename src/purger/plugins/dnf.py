"""Plugin for the dnf downloaded package cache."""

from purger.plugin import Action, Plugin, Tier

# dnf4 caches under /var/cache/dnf, dnf5 under /var/cache/libdnf5.
_PACKAGE_PATHS = ("/var/cache/dnf/*/packages/*.rpm", "/var/cache/libdnf5/*/packages/*.rpm")

PLUGIN = Plugin(
    name="dnf",
    description="dnf downloaded package files",
    platforms=frozenset({"linux"}),
    requires="dnf",
    paths=_PACKAGE_PATHS,
    actions=(
        Action(
            name="packages",
            tier=Tier.SAFE,
            help="Remove downloaded package files. Reinstalls download them again.",
            command=("dnf", "clean", "packages"),
            needs_sudo=True,
            paths=_PACKAGE_PATHS,
        ),
    ),
)
