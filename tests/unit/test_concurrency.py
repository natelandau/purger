"""Tests for running work concurrently on daemon threads."""

import threading

import pytest

from purger.concurrency import map_as_completed


def test_map_as_completed_yields_every_result_with_its_index() -> None:
    """Verify each item's result is yielded once, tagged with the item's index."""
    # Given
    items = [1, 2, 3, 4, 5]

    # When
    results = dict(map_as_completed(lambda n: n * 10, items=items, max_workers=2))

    # Then
    assert results == {0: 10, 1: 20, 2: 30, 3: 40, 4: 50}


def test_map_as_completed_empty_yields_nothing() -> None:
    """Verify no items means no results and no threads."""
    assert list(map_as_completed(lambda n: n, items=[])) == []


def test_map_as_completed_reraises_worker_interrupt_in_caller() -> None:
    """Verify a KeyboardInterrupt raised by the function reaches the caller."""

    # Given
    def interrupt(_: int) -> int:
        raise KeyboardInterrupt

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        list(map_as_completed(interrupt, items=[1]))


def test_map_as_completed_uses_daemon_threads() -> None:
    """Verify workers are daemon threads, so an interrupted caller can exit at once."""
    # Given
    daemons: list[bool] = []

    def record(_: int) -> None:
        daemons.append(threading.current_thread().daemon)

    # When
    list(map_as_completed(record, items=[1, 2]))

    # Then
    assert daemons == [True, True]
