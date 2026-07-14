"""Thread-safe coordination for operations that must not overlap.

The gate is intentionally independent from the application controller so external
workers, such as a skin-library migration, can share the same exclusion boundary.
Controller operations use :meth:`OperationGate.try_acquire`; long-lived external
workers can use the fair, cancellable :meth:`OperationGate.acquire` wait.
"""

from __future__ import annotations

import math
from collections import deque
from threading import Condition, Event, Lock
from types import TracebackType


class OperationLease:
    """An ownership token returned by :class:`OperationGate`.

    A lease can be released more than once safely.  The gate still validates the
    owner and opaque token on the first release, preventing a stale or foreign
    lease from unlocking a newer operation.
    """

    __slots__ = ("_gate", "_owner", "_release_lock", "_released", "_token")

    def __init__(self, gate: OperationGate, owner: str, token: object) -> None:
        self._gate = gate
        self._owner = owner
        self._token = token
        self._release_lock = Lock()
        self._released = False

    @property
    def owner(self) -> str:
        """The human-readable owner supplied during acquisition."""

        return self._owner

    @property
    def released(self) -> bool:
        """Whether this lease has already been released."""

        with self._release_lock:
            return self._released

    def release(self) -> None:
        """Release the gate once; repeated calls are harmless."""

        with self._release_lock:
            if self._released:
                return
            self._gate._release(self._owner, self._token)
            self._released = True

    def __enter__(self) -> OperationLease:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


class OperationGate:
    """A fair, cancellable, single-owner operation gate."""

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._current_owner: str | None = None
        self._current_token: object | None = None
        self._waiters: deque[object] = deque()

    @property
    def current_owner(self) -> str | None:
        """Return the active owner, or ``None`` when the gate is available."""

        with self._condition:
            return self._current_owner

    def try_acquire(self, owner: str) -> OperationLease | None:
        """Acquire immediately, returning ``None`` when another operation wins.

        Queued waiters take priority over new non-blocking callers.  This prevents
        a migration waiting for an active sync from being starved by repeated tray
        actions.
        """

        owner = self._validate_owner(owner)
        with self._condition:
            if self._current_token is not None or self._waiters:
                return None
            return self._grant_locked(owner, object())

    def acquire(
        self,
        owner: str,
        cancel_event: Event,
        *,
        poll_interval_seconds: float = 0.05,
    ) -> OperationLease | None:
        """Wait fairly for a lease, or return ``None`` after cancellation."""

        owner = self._validate_owner(owner)
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive and finite")

        waiter = object()
        queued = True
        with self._condition:
            self._waiters.append(waiter)
            try:
                while True:
                    if cancel_event.is_set():
                        return None
                    if self._current_token is None and self._waiters[0] is waiter:
                        self._waiters.popleft()
                        queued = False
                        return self._grant_locked(owner, object())
                    self._condition.wait(poll_interval_seconds)
            finally:
                if queued:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()

    @staticmethod
    def _validate_owner(owner: str) -> str:
        normalized = owner.strip()
        if not normalized:
            raise ValueError("owner must not be empty")
        return normalized

    def _grant_locked(self, owner: str, token: object) -> OperationLease:
        if self._current_token is not None:
            raise RuntimeError("operation gate is already owned")
        self._current_owner = owner
        self._current_token = token
        return OperationLease(self, owner, token)

    def _release(self, owner: str, token: object) -> None:
        with self._condition:
            if self._current_owner != owner or self._current_token is not token:
                raise RuntimeError("operation lease does not own this gate")
            self._current_owner = None
            self._current_token = None
            self._condition.notify_all()


__all__ = ["OperationGate", "OperationLease"]
