"""Run independent measurements side by side without delaying exit."""

import queue
import threading
from collections.abc import Callable, Iterator, Sequence

MAX_WORKERS = 8


def map_as_completed[T, R](
    fn: Callable[[T], R], items: Sequence[T], *, max_workers: int = MAX_WORKERS
) -> Iterator[tuple[int, R]]:
    """Call fn on every item concurrently and yield each result as soon as it is ready.

    Workers are daemon threads, so Ctrl-C in the caller ends the process at once
    instead of waiting for in-flight work, such as a long directory walk, to finish.
    Once the caller stops iterating, workers start no new items.

    Args:
        fn: The function to call on each item.
        items: The items to process.
        max_workers: The most items processed at once.

    Yields:
        tuple[int, R]: The item's index in items and fn's result for it.
    """
    if not items:
        return
    todo: queue.SimpleQueue[int] = queue.SimpleQueue()
    for index in range(len(items)):
        todo.put(index)
    done: queue.SimpleQueue[tuple[int, R] | BaseException] = queue.SimpleQueue()
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            try:
                index = todo.get_nowait()
            except queue.Empty:
                return
            try:
                done.put((index, fn(items[index])))
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
                done.put(exc)

    for _ in range(min(max_workers, len(items))):
        threading.Thread(target=work, daemon=True).start()
    try:
        for _ in items:
            outcome = done.get()
            if isinstance(outcome, BaseException):
                raise outcome
            yield outcome
    finally:
        stop.set()
