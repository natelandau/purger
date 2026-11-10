"""Plugin for Xcode build products and device support files."""

from purger.plugin import Action, Plugin, Tier

PLUGIN = Plugin(
    name="xcode",
    description="Xcode derived data, device support, and archives",
    platforms=frozenset({"darwin"}),
    actions=(
        Action(
            name="derived-data",
            tier=Tier.SAFE,
            help="Remove build products and indexes. Xcode rebuilds them on next build.",
            paths=("~/Library/Developer/Xcode/DerivedData/*",),
        ),
        Action(
            name="device-support",
            tier=Tier.CAUTION,
            help=(
                "Remove iOS debug symbols copied from devices. "
                "Reconnecting each device re-copies them, which takes minutes."
            ),
            paths=("~/Library/Developer/Xcode/iOS DeviceSupport/*",),
        ),
        Action(
            name="archives",
            tier=Tier.DANGEROUS,
            help=(
                "Remove app archives, which hold the dSYMs needed to symbolicate shipped apps. "
                "Deleted archives cannot be rebuilt."
            ),
            paths=("~/Library/Developer/Xcode/Archives/*",),
        ),
    ),
)
