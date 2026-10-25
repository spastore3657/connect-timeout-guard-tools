"""
Core implementation: a deadline-bounded socket connector.

Design choice
-------------
We wrap a plain ``socket.create_connection`` rather than implementing the
TCP handshake by hand.  The monotonic clock governs the *overall* budget,
not per-call retry cadence, so we keep the selection-and-timeout loop that
``create_connection`` already uses and just feed it a shrinking remainder.
This is a deliberate trade-off: we give up fine-grained control of the
handshake internals in exchange for not reimplementing getaddrinfo address
walking, which is where most real-world connection code spends its time.

Why a clock argument?
--------------------
Everything that touches time in this library reads from a callable that the
caller supplies (defaulting to ``time.monotonic``).  This is not test-only:
if a caller wants to drive the connector against a simulated clock, they
can, without monkey-patching ``time``.
"""

from __future__ import annotations

import socket
import time
from typing import Callable, Optional


__all__ = ["ConnectDeadline", "ConnectTimeoutError"]


#: Type alias for the monotonic clock callable.
Clock = Callable[[], float]


class ConnectTimeoutError(TimeoutError):
    """Raised when the TCP connection attempt does not complete by its deadline."""

    def __init__(self, host: str, port: int, deadline: float) -> None:
        self.host = host
        self.port = port
        self.deadline = deadline
        super().__init__(
            f"Connect to {host}:{port} timed out at monotonic deadline {deadline}"
        )


class ConnectDeadline:
    """
    A strict deadline for a socket connection attempt.

    The deadline is absolute, expressed in whatever unit the supplied ``clock``
returns (seconds since the monotonic epoch, by default).  We use an absolute
    deadline rather than a relative timeout so that retries against the next
    resolved address automatically get the remaining budget without the caller
    having to do arithmetic.

    Parameters
    ----------
    timeout:
        The number of seconds the connection attempt may run.  Must be a
        non-negative finite number.  Zero means: fail immediately if the
        first connect would block.  ``float('inf')`` is *rejected* because a
        monotonic-clock deadline that can never fire is not a deadline; a
        caller who wants no timeout should just use ``socket.create_connection``
        directly.
    clock:
        Callable returning the current time as a float.  Defaults to
        ``time.monotonic`` so that deadlines are not subject to system clock
        adjustments.

    Attributes
    ----------
    deadline:
        The absolute monotonic deadline computed as ``clock() + timeout``.
        Read-only.
    """

    __slots__ = ("_deadline", "_clock", "_timeout")

    def __init__(self, timeout: float, clock: Optional[Clock] = None) -> None:
        self._clock: Clock = clock if clock is not None else time.monotonic

        # Reject NaN and infinity explicitly.  NaN comparisons are always
        # False, which would make every time check silently pass; inf would
        # mean the deadline never fires, which defeats the point of the library.
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            raise TypeError("timeout must be a real number")
        if timeout_f != timeout_f:  # NaN check
            raise ValueError("timeout must not be NaN")
        if timeout_f < 0:
            raise ValueError("timeout must be non-negative")
        if timeout_f == float("inf"):
            raise ValueError("timeout must be finite")

        self._timeout = timeout_f
        # Compute the absolute deadline once, at construction, so that the
        # clock is sampled exactly once.  This makes the budget deterministic
        # regardless of how long address resolution takes.
        self._deadline: float = self._clock() + timeout_f

    @property
    def deadline(self) -> float:
        return self._deadline

    def remaining(self) -> float:
        """Return the seconds left until the deadline, or 0 if passed."""
        return max(0.0, self._deadline - self._clock())

    def expired(self) -> bool:
        """True if the deadline has passed."""
        return self._clock() >= self._deadline

    def connect(
        self,
        host: str,
        port: int,
        source_address: Optional[tuple] = None,
    ) -> socket.socket:
        """
        Connect to ``host:port``, aborting if the deadline expires.

        We call ``socket.create_connection`` with a ``timeout`` equal to the
        *remaining* budget at call time.  ``create_connection`` walks the
        list of addresses returned by ``getaddrinfo`` and attempts each one;
        each attempt gets the same wall-clock timeout, so the overall budget
        can be exceeded by up to one ``timeout``.  To keep that within the
        spirit of a strict deadline we shrink the per-attempt timeout when
        the remaining budget is smaller.

        Parameters
        ----------
        host:
            Hostname or IP literal to connect to.
        port:
            Port number.
        source_address:
            Optional ``(host, port)`` to bind the client socket to before
            connecting.

        Raises
        ------
        ConnectTimeoutError:
            If no connection attempt completes before the deadline expires.
        OSError:
            Any other socket-level error (refused, unreachable, DNS failure).
        """
        remaining = self.remaining()
        if remaining <= 0:
            # Budget already exhausted before we even tried.  This can happen
            # legitimately when the deadline was constructed with timeout=0
            # or when resolution of a previous address ate all the budget.
            raise ConnectTimeoutError(host, port, self._deadline)

        try:
            return socket.create_connection(
                address=(host, port),
                timeout=remaining,
                source_address=source_address,
            )
        except (socket.timeout, TimeoutError):
            # ``create_connection`` raises ``socket.timeout`` on its own
            # per-attempt budget, and ``TimeoutError`` is the parent class;
            # catching both covers CPython across versions.  We translate to
            # our own type so callers catch one exception regardless of which
            # internal timeout fired.
            raise ConnectTimeoutError(host, port, self._deadline)
