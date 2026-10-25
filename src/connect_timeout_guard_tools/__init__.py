"""
connect-timeout-guard-tools
=========================

Wrap a socket connection attempt with a monotonic-clock deadline that aborts
if the TCP handshake does not complete.
"""

from connect_timeout_guard_tools.core import (
    ConnectDeadline,
    ConnectTimeoutError,
)

__all__ = ["ConnectDeadline", "ConnectTimeoutError"]
