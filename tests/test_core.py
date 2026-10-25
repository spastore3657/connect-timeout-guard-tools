"""
Tests for connect_timeout_guard_tools.core.

All tests are deterministic.  We never touch the network.  Instead we
exercise the deadline math and the exception-translation logic directly.

We DO NOT test the real ``socket.create_connection`` path against a live
listener because that would require binding a socket in the test process,
which introduces timing nondeterminism on slow CI runners.  The real
connector is a one-line wrapper around the standard library; its behaviour
is the standard library's behaviour and is not worth re-asserting here.
"""

import socket
import time
import unittest
from unittest import mock

from connect_timeout_guard_tools.core import (
    ConnectDeadline,
    ConnectTimeoutError,
)


class _FakeClock:
    """A controllable monotonic clock for deterministic tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class ConnectDeadlineConstructionTests(unittest.TestCase):
    def test_records_absolute_deadline(self):
        clock = _FakeClock(start=100.0)
        cd = ConnectDeadline(5.0, clock=clock)
        self.assertEqual(cd.deadline, 105.0)

    def test_default_clock_is_monotonic(self):
        cd = ConnectDeadline(1.0)
        # We do not assert on the value, only that it lies in the future
        # relative to the real monotonic clock.
        self.assertGreater(cd.deadline, time.monotonic())

    def test_rejects_nan_timeout(self):
        with self.assertRaises(ValueError):
            ConnectDeadline(float("nan"), clock=_FakeClock())

    def test_rejects_infinite_timeout(self):
        with self.assertRaises(ValueError):
            ConnectDeadline(float("inf"), clock=_FakeClock())

    def test_rejects_negative_timeout(self):
        with self.assertRaises(ValueError):
            ConnectDeadline(-0.1, clock=_FakeClock())

    def test_rejects_non_numeric_timeout(self):
        with self.assertRaises(TypeError):
            ConnectDeadline("soon", clock=_FakeClock())


class ConnectDeadlineRemainingTests(unittest.TestCase):
    def test_remaining_shrinks_as_clock_advances(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(10.0, clock=clock)
        self.assertEqual(cd.remaining(), 10.0)
        clock.advance(3.0)
        self.assertEqual(cd.remaining(), 7.0)
        clock.advance(10.0)
        self.assertEqual(cd.remaining(), 0.0)

    def test_remaining_never_negative(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(1.0, clock=clock)
        clock.advance(100.0)
        self.assertEqual(cd.remaining(), 0.0)

    def test_expired_true_after_deadline(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(2.0, clock=clock)
        clock.advance(2.0)
        self.assertTrue(cd.expired())

    def test_expired_false_before_deadline(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(2.0, clock=clock)
        clock.advance(1.9)
        self.assertFalse(cd.expired())


class ConnectDeadlineZeroTimeoutTests(unittest.TestCase):
    def test_zero_timeout_fails_immediately_without_network(self):
        # With timeout=0, remaining() is 0 at construction time, so connect()
        # must raise before any socket call is made.  We patch
        # create_connection to assert it is never invoked.
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(0.0, clock=clock)
        with mock.patch("connect_timeout_guard_tools.core.socket.create_connection") as m:
            with self.assertRaises(ConnectTimeoutError):
                cd.connect("127.0.0.1", 8080)
            m.assert_not_called()


class ConnectDeadlineConnectTests(unittest.TestCase):
    def test_connect_passes_remaining_timeout_to_create_connection(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(8.0, clock=clock)
        clock.advance(2.0)  # 6 seconds left
        fake_sock = mock.Mock(spec=socket.socket)
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection",
            return_value=fake_sock,
        ) as m:
            result = cd.connect("127.0.0.1", 9999)
        self.assertIs(result, fake_sock)
        m.assert_called_once()
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get("timeout"), 6.0)
        self.assertEqual(kwargs.get("address"), ("127.0.0.1", 9999))

    def test_connect_translates_socket_timeout(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(1.0, clock=clock)
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection",
            side_effect=socket.timeout("handshake stalled"),
        ):
            with self.assertRaises(ConnectTimeoutError) as ctx:
                cd.connect("10.0.0.1", 1)
        self.assertEqual(ctx.exception.host, "10.0.0.1")
        self.assertEqual(ctx.exception.port, 1)
        self.assertEqual(ctx.exception.deadline, 1.0)

    def test_connect_translates_timeout_error_subclass(self):
        # On CPython 3.10+, socket.timeout IS TimeoutError, but we catch the
        # base class explicitly so the wrapper is robust across versions.
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(1.0, clock=clock)
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaises(ConnectTimeoutError):
                cd.connect("10.0.0.2", 2)

    def test_connect_does_not_swallow_connection_refused(self):
        # A refused connection is an OSError subclass but NOT a timeout.
        # We must let it through unchanged so callers can distinguish
        # "nobody is listening" from "too slow".
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(5.0, clock=clock)
        refused = ConnectionRefusedError(111, "Connection refused")
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection",
            side_effect=refused,
        ):
            with self.assertRaises(ConnectionRefusedError):
                cd.connect("127.0.0.1", 1)

    def test_connect_does_not_swallow_dns_failure(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(5.0, clock=clock)
        # socket.gaierror is an OSError subclass raised by getaddrinfo.
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection",
            side_effect=socket.gaierror(-2, "Name or service not known"),
        ):
            with self.assertRaises(socket.gaierror):
                cd.connect("nonexistent.invalid", 80)

    def test_connect_after_expiry_does_not_touch_the_network(self):
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(1.0, clock=clock)
        clock.advance(1.0)  # now expired
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection"
        ) as m:
            with self.assertRaises(ConnectTimeoutError):
                cd.connect("127.0.0.1", 3)
            m.assert_not_called()

    def test_connect_timeout_error_is_a_timeout_error_subclass(self):
        # Callers using ``except TimeoutError`` must catch our type.
        clock = _FakeClock(start=0.0)
        cd = ConnectDeadline(1.0, clock=clock)
        with mock.patch(
            "connect_timeout_guard_tools.core.socket.create_connection",
            side_effect=socket.timeout("stalled"),
        ):
            try:
                cd.connect("10.0.0.3", 4)
            except TimeoutError:
                pass
            else:
                self.fail("ConnectTimeoutError was not a TimeoutError")


if __name__ == "__main__":
    unittest.main()
