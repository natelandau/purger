"""Plugin for unused Flatpak runtimes and extensions."""

from purger.plugin import Action, Plugin, Tier

_UNINSTALL_UNUSED = ("flatpak", "uninstall", "--unused", "-y", "--noninteractive")


def _unknown_size() -> None:
    """Report the footprint as unknown, since flatpak cannot preview what `--unused` removes.

    Summing the two actions would also be unknown, but naming it here keeps the
    footprint from looking like an oversight.
    """


PLUGIN = Plugin(
    name="flatpak",
    description="Flatpak runtimes and extensions no app uses",
    platforms=frozenset({"linux"}),
    requires="flatpak",
    size=_unknown_size,
    actions=(
        Action(
            name="unused-user",
            tier=Tier.SAFE,
            help="Remove runtimes and extensions no app in your user installation uses.",
            command=(*_UNINSTALL_UNUSED, "--user"),
        ),
        Action(
            name="unused-system",
            tier=Tier.SAFE,
            help="Remove runtimes and extensions no app in the system installation uses.",
            command=(*_UNINSTALL_UNUSED, "--system"),
            needs_sudo=True,
        ),
    ),
)
