from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event, Thread

import pytest

from league_skin_manager.operation_gate import OperationGate, OperationLease


def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def test_try_acquire_exposes_owner_and_release_is_idempotent() -> None:
    gate = OperationGate()

    lease = gate.try_acquire("  skin synchronization  ")

    assert lease is not None
    assert lease.owner == "skin synchronization"
    assert lease.released is False
    assert gate.current_owner == "skin synchronization"
    assert gate.try_acquire("migration") is None

    lease.release()
    lease.release()
    assert lease.released is True
    assert gate.current_owner is None

    next_lease = gate.try_acquire("migration")
    assert next_lease is not None
    with next_lease:
        assert next_lease.owner == "migration"
    assert gate.current_owner is None


def test_foreign_token_cannot_release_current_owner() -> None:
    gate = OperationGate()
    lease = gate.try_acquire("migration")
    assert lease is not None
    forged = OperationLease(gate, "migration", object())

    with pytest.raises(RuntimeError, match="does not own"):
        forged.release()

    assert forged.released is False
    assert gate.current_owner == "migration"
    lease.release()


def test_waiting_acquisition_is_cancellable_without_releasing_owner() -> None:
    gate = OperationGate()
    active = gate.try_acquire("sync")
    assert active is not None
    cancel = Event()
    completed = Event()
    result: list[OperationLease | None] = []

    def wait_for_gate() -> None:
        result.append(gate.acquire("migration", cancel, poll_interval_seconds=0.005))
        completed.set()

    worker = Thread(target=wait_for_gate)
    worker.start()
    wait_until(lambda: len(gate._waiters) == 1)
    cancel.set()

    assert completed.wait(1)
    worker.join(1)
    assert result == [None]
    assert gate.current_owner == "sync"
    active.release()


def test_waiter_has_priority_over_new_nonblocking_callers() -> None:
    gate = OperationGate()
    active = gate.try_acquire("sync")
    assert active is not None
    cancel = Event()
    acquired = Event()
    release_waiter = Event()

    def wait_for_gate() -> None:
        lease = gate.acquire("migration", cancel, poll_interval_seconds=0.005)
        assert lease is not None
        acquired.set()
        release_waiter.wait(1)
        lease.release()

    worker = Thread(target=wait_for_gate)
    worker.start()
    wait_until(lambda: len(gate._waiters) == 1)
    active.release()

    # Whether the waiter has already acquired or is still first in line, a new
    # tray operation cannot jump ahead of it.
    assert gate.try_acquire("barging launch") is None
    assert acquired.wait(1)
    assert gate.current_owner == "migration"
    release_waiter.set()
    worker.join(1)
    assert not worker.is_alive()
    assert gate.current_owner is None


@pytest.mark.parametrize("owner", ["", "   "])
def test_owner_must_not_be_empty(owner: str) -> None:
    gate = OperationGate()
    with pytest.raises(ValueError, match="owner"):
        gate.try_acquire(owner)


def test_wait_settings_are_validated_and_precancelled_wait_does_not_acquire() -> None:
    gate = OperationGate()
    cancel = Event()
    with pytest.raises(ValueError, match="positive and finite"):
        gate.acquire("migration", cancel, poll_interval_seconds=0)

    cancel.set()
    assert gate.acquire("migration", cancel) is None
    assert gate.current_owner is None
