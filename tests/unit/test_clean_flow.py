"""Tests for the clean decision flow."""

from pathlib import Path
from typing import Any, TypedDict

import pytest
from nclutils.strings import human_size
from pytest_mock import MockerFixture

from purger.clean_flow import CleanOptions, plan_clean, risky_prompt, safe_prompt
from purger.plugin import Tier
from purger.reports import ActionReport, PluginReport, Status
from tests.conftest import make_action, make_plugin


class FakeConfirm:
    """Record prompts and return scripted answers."""

    def __init__(self, answers: list[bool] | None = None) -> None:
        self.answers = list(answers or [])
        self.prompts: list[str] = []

    def __call__(self, text: str) -> bool:
        self.prompts.append(text)
        return self.answers.pop(0) if self.answers else False


class _Extra(TypedDict, total=False):
    """Per-action test overrides: Action fields and the measured size."""

    action: dict[str, Any]
    size: int | None


def _report(
    plugin_name: str = "p",
    footprint: int | None = 100,
    **actions: tuple[Tier, _Extra],
) -> PluginReport:
    reports = [
        ActionReport(
            action=make_action(name=name, tier=tier, **extra.get("action", {})),
            size_bytes=extra.get("size", 10),
            resolved_paths=[Path("/x")],
        )
        for name, (tier, extra) in actions.items()
    ]
    plugin = make_plugin(name=plugin_name, actions=tuple(r.action for r in reports))
    return PluginReport(plugin=plugin, footprint_bytes=footprint, actions=reports)


def _run(
    reports: list[PluginReport],
    confirm: FakeConfirm | None = None,
    *,
    sudo: bool = False,
    **opts: bool,
) -> tuple[list[ActionReport], FakeConfirm]:
    confirm = confirm or FakeConfirm()
    approved = plan_clean(reports, CleanOptions(**opts), confirm=confirm, sudo_ready=lambda: sudo)
    return approved, confirm


def test_plan_clean_yes_never_approves_risky() -> None:
    """Verify --yes in a non-interactive run declines CAUTION and DANGEROUS actions."""
    # Given caution and dangerous actions
    rep = _report(c=(Tier.CAUTION, {}), d=(Tier.DANGEROUS, {}))

    # When run with yes, non-interactive, dangerous allowed
    approved, confirm = _run([rep], yes=True, interactive=False, dangerous=True)

    # Then nothing runs and nothing is asked
    assert approved == []
    assert [r.status for r in rep.actions] == [Status.DECLINED, Status.DECLINED]
    assert confirm.prompts == []


def test_plan_clean_yes_skips_only_safe_prompt() -> None:
    """Verify --yes skips the SAFE prompt but still asks about CAUTION."""
    # Given one safe and one caution action
    rep = _report(s=(Tier.SAFE, {}), c=(Tier.CAUTION, {}))

    # When run with yes and interactive
    approved, confirm = _run([rep], FakeConfirm([True]), yes=True, interactive=True)

    # Then only the caution prompt appears, and both are approved
    assert len(confirm.prompts) == 1
    assert confirm.prompts[0] == risky_prompt("p", rep.actions[1])
    assert approved == rep.actions


def test_plan_clean_safe_single_prompt() -> None:
    """Verify three SAFE actions yield one prompt and a no declines all."""
    # Given three safe actions
    rep = _report(a=(Tier.SAFE, {}), b=(Tier.SAFE, {}), c=(Tier.SAFE, {}))

    # When the user answers no
    approved, confirm = _run([rep], FakeConfirm([False]), interactive=True)

    # Then one prompt, all declined
    assert len(confirm.prompts) == 1
    assert approved == []
    assert all(r.status is Status.DECLINED for r in rep.actions)


def test_plan_clean_false_answer_declines_caution() -> None:
    """Verify a False answer (including a cancelled prompt) declines CAUTION."""
    # Given a caution action
    rep = _report(c=(Tier.CAUTION, {}))

    # When answered False
    approved, _ = _run([rep], FakeConfirm([False]), interactive=True)

    # Then declined
    assert approved == []
    assert rep.actions[0].status is Status.DECLINED


@pytest.mark.parametrize(("sudo", "expected"), [(True, None), (False, Status.SKIPPED)])
def test_plan_clean_non_interactive_sudo(sudo: bool, expected: Status | None) -> None:
    """Verify a SAFE sudo action runs non-interactively only when sudo is ready."""
    # Given a safe sudo action
    rep = _report(s=(Tier.SAFE, {"action": {"needs_sudo": True}}))

    # When run with yes, non-interactive
    approved, _ = _run([rep], sudo=sudo, yes=True, interactive=False)

    # Then approved or skipped
    assert rep.actions[0].status is expected
    assert approved == (rep.actions if sudo else [])


def test_plan_clean_sudo_ready_called_at_most_once(mocker: MockerFixture) -> None:
    """Verify sudo_ready is called once however many sudo actions exist."""
    # Given two sudo actions
    rep = _report(
        a=(Tier.SAFE, {"action": {"needs_sudo": True}}),
        b=(Tier.SAFE, {"action": {"needs_sudo": True}}),
    )
    sudo_ready = mocker.Mock(return_value=True)

    # When planned
    plan_clean(
        [rep],
        CleanOptions(yes=True, interactive=False),
        confirm=FakeConfirm(),
        sudo_ready=sudo_ready,
    )

    # Then called once
    assert sudo_ready.call_count == 1


def test_plan_clean_sudo_not_checked_without_need(mocker: MockerFixture) -> None:
    """Verify sudo_ready is not called when no SAFE sudo action needs it."""
    # Given a plain action
    rep = _report(a=(Tier.SAFE, {}))
    sudo_ready = mocker.Mock(return_value=True)

    # When planned non-interactively
    plan_clean(
        [rep],
        CleanOptions(yes=True, interactive=False),
        confirm=FakeConfirm(),
        sudo_ready=sudo_ready,
    )

    # Then never called
    sudo_ready.assert_not_called()


def test_plan_clean_non_interactive_without_yes_declines_safe() -> None:
    """Verify a non-interactive run without --yes declines SAFE actions."""
    # Given a safe action
    rep = _report(a=(Tier.SAFE, {}))

    # When run non-interactively without yes
    approved, confirm = _run([rep], interactive=False)

    # Then declined without prompting
    assert approved == []
    assert rep.actions[0].status is Status.DECLINED
    assert confirm.prompts == []


def test_plan_clean_nothing_to_clean() -> None:
    """Verify zero-size actions and unsized actions of zero-footprint plugins are not prompted."""
    # Given a zero-size action and an unsized action in a zero-footprint plugin
    rep = _report(a=(Tier.SAFE, {"size": 0}))
    rep2 = _report("q", footprint=0, b=(Tier.CAUTION, {"size": None}))

    # When planned
    approved, confirm = _run([rep, rep2], interactive=True)

    # Then both are nothing-to-clean and no prompt happens
    assert approved == []
    assert rep.actions[0].status is Status.NOTHING_TO_CLEAN
    assert rep2.actions[0].status is Status.NOTHING_TO_CLEAN
    assert confirm.prompts == []


def test_plan_clean_known_size_beats_zero_footprint() -> None:
    """Verify an action with a known non-zero size is offered even when the footprint is zero."""
    # Given a zero-footprint plugin whose action measures its own data
    rep = _report(footprint=0, v=(Tier.CAUTION, {"size": 2_000}))

    # When planned interactively and approved
    approved, confirm = _run([rep], FakeConfirm([True]), interactive=True)

    # Then the action is offered and approved
    assert len(confirm.prompts) == 1
    assert approved == rep.actions


def test_plan_clean_dangerous_without_flag_declined() -> None:
    """Verify DANGEROUS actions are declined when --dangerous is absent."""
    # Given a dangerous action
    rep = _report(d=(Tier.DANGEROUS, {}))

    # When run interactively without dangerous
    approved, confirm = _run([rep], FakeConfirm([True]), interactive=True)

    # Then declined and not asked
    assert approved == []
    assert rep.actions[0].status is Status.DECLINED
    assert confirm.prompts == []


def test_plan_clean_dry_run_plans_everything() -> None:
    """Verify dry run marks non-empty actions PLANNED and never prompts."""
    # Given mixed actions
    rep = _report(s=(Tier.SAFE, {}), c=(Tier.CAUTION, {}), z=(Tier.SAFE, {"size": 0}))

    # When dry run
    approved, confirm = _run([rep], dry_run=True, yes=True)

    # Then planned except the empty one
    assert approved == []
    assert confirm.prompts == []
    assert [r.status for r in rep.actions] == [
        Status.PLANNED,
        Status.PLANNED,
        Status.NOTHING_TO_CLEAN,
    ]


def test_plan_clean_returns_plugin_order() -> None:
    """Verify approved actions come back in plugin order, not prompt order."""
    # Given a caution action before a safe action, across two plugins
    rep1 = _report("one", c=(Tier.CAUTION, {}), s=(Tier.SAFE, {}))
    rep2 = _report("two", s=(Tier.SAFE, {}))

    # When everything is approved
    approved, confirm = _run(
        [rep1, rep2], FakeConfirm([True, True]), dangerous=True, interactive=True
    )

    # Then SAFE prompt is first, caution second, result in plugin order
    assert "one/s" in confirm.prompts[0]
    assert confirm.prompts[1] == risky_prompt("one", rep1.actions[0])
    assert approved == [rep1.actions[0], rep1.actions[1], rep2.actions[0]]


def test_clean_options_default_is_non_interactive() -> None:
    """Verify CleanOptions fails closed by assuming no one can answer prompts."""
    # Given / When
    opts = CleanOptions()

    # Then
    assert opts.interactive is False


def test_plan_clean_every_action_resolved() -> None:
    """Verify every action ends with a status or is in the approved list."""
    # Given a mixed scenario
    rep1 = _report(
        "one",
        s=(Tier.SAFE, {}),
        c=(Tier.CAUTION, {}),
        d=(Tier.DANGEROUS, {}),
        z=(Tier.SAFE, {"size": 0}),
    )
    rep2 = _report("two", footprint=0, s=(Tier.SAFE, {}))

    # When planned with the caution refused
    approved, _ = _run([rep1, rep2], FakeConfirm([True, False]), interactive=True)

    # Then the invariant holds
    for rep in (rep1, rep2):
        for ar in rep.actions:
            assert (ar.status is not None) != (ar in approved)


def test_safe_prompt_total_only_when_all_sized() -> None:
    """Verify the SAFE prompt shows a total only when every size is known."""
    # Given sized and unsized reports
    a = ActionReport(action=make_action(name="a"), size_bytes=1_500_000, resolved_paths=[])
    b = ActionReport(action=make_action(name="b"), size_bytes=500_000, resolved_paths=[])
    c = ActionReport(action=make_action(name="c"), size_bytes=None, resolved_paths=[])

    # When prompts are built
    sized = safe_prompt([("p", a), ("p", b)])
    unsized = safe_prompt([("p", a), ("p", c)])

    # Then only the fully sized one has the total
    assert human_size(2_000_000) in sized
    assert "p/a" in sized
    assert "about" not in unsized


def test_safe_prompt_names_sudo_actions() -> None:
    """Verify the SAFE prompt marks exactly the actions that run with sudo."""
    # Given one sudo action and one plain action
    a = ActionReport(action=make_action(name="a", needs_sudo=True), size_bytes=1, resolved_paths=[])
    b = ActionReport(action=make_action(name="b"), size_bytes=1, resolved_paths=[])

    # When
    text = safe_prompt([("p", a), ("p", b)])

    # Then
    assert "p/a (sudo)" in text
    assert "p/b (sudo)" not in text
    assert "sudo" not in safe_prompt([("p", b)])


def test_risky_prompt_text() -> None:
    """Verify risky prompts show help and warn for DANGEROUS actions."""
    # Given caution and dangerous actions
    c = ActionReport(
        action=make_action(name="c", tier=Tier.CAUTION, help="Drops stuff"),
        size_bytes=5,
        resolved_paths=[],
    )
    d = ActionReport(
        action=make_action(name="d", tier=Tier.DANGEROUS, help="Nukes stuff"),
        size_bytes=None,
        resolved_paths=[],
    )

    # When built / Then help and warning appear
    assert "Drops stuff" in risky_prompt("p", c)
    assert "may permanently delete data" not in risky_prompt("p", c)
    assert "Nukes stuff" in risky_prompt("p", d)
    assert "may permanently delete data" in risky_prompt("p", d)


def test_plan_clean_yes_interactive_still_asks_dangerous() -> None:
    """Verify --yes in an interactive run still asks about DANGEROUS and declines on No."""
    # Given a safe and a dangerous action with --dangerous
    rep = _report(s=(Tier.SAFE, {}), d=(Tier.DANGEROUS, {}))

    # When run with yes and interactive, answering No to the dangerous prompt
    approved, confirm = _run(
        [rep], FakeConfirm([False]), yes=True, interactive=True, dangerous=True
    )

    # Then only the dangerous prompt was shown, and only the safe action runs
    assert confirm.prompts == [risky_prompt("p", rep.actions[1])]
    assert "may permanently delete data" in confirm.prompts[0]
    assert approved == [rep.actions[0]]
    assert rep.actions[1].status is Status.DECLINED


def test_plan_clean_interactive_safe_sudo_never_probes_sudo(mocker: MockerFixture) -> None:
    """Verify an interactive SAFE sudo action is approved without probing sudo."""
    # Given a safe sudo action
    rep = _report(s=(Tier.SAFE, {"action": {"needs_sudo": True}}))
    sudo_ready = mocker.Mock(return_value=False)

    # When approved interactively
    approved = plan_clean(
        [rep],
        CleanOptions(interactive=True),
        confirm=FakeConfirm([True]),
        sudo_ready=sudo_ready,
    )

    # Then it is approved and sudo is never probed
    assert approved == rep.actions
    sudo_ready.assert_not_called()


def test_risky_prompt_mentions_sudo() -> None:
    """Verify the risky prompt says when an action requires sudo."""
    # Given a caution sudo action and a caution plain action
    rooty = ActionReport(
        action=make_action(name="r", tier=Tier.CAUTION, command=("tool",), needs_sudo=True),
        size_bytes=1,
        resolved_paths=[],
    )
    plain = ActionReport(
        action=make_action(name="q", tier=Tier.CAUTION), size_bytes=1, resolved_paths=[]
    )

    # When
    rooty_text = risky_prompt("p", rooty)
    plain_text = risky_prompt("p", plain)

    # Then
    assert "Requires sudo" in rooty_text
    assert "sudo" not in plain_text
