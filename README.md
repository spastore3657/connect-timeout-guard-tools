# connect-timeout-guard-tools

Wrap a socket connection attempt with a monotonic-clock deadline that aborts if the TCP handshake does not complete.

## Usage

```python
from connect_timeout_guard_tools import ConnectDeadline, ConnectTimeoutError

deadline = ConnectDeadline(timeout=3.0)
try:
    sock = deadline.connect("127.0.0.1", 6379)
except ConnectTimeoutError as exc:
    print(f"timed out: {exc}")
except OSError as exc:
    print(f"connection failed: {exc}")
else:
    sock.close()
```

## Why this exists

`socket.create_connection` accepts a `timeout`, but that timeout is applied per destination address. When `getaddrinfo` returns multiple records, each attempt gets the full budget, so the wall-clock cost of a failed connect can be a multiple of the value you passed. This library enforces a single absolute deadline across the whole attempt so the caller controls the worst case.

The trade-off: we still call `create_connection` internally rather than driving the non-blocking handshake ourselves. That means we cannot interrupt a TCP SYN that is in flight below the Python level. We rely on the socket-layer timeout to fire and then translate that into our own `ConnectTimeoutError`. In practice this is close enough to a true deadline, and it avoids re-implementing address-family selection.

## Construction rules

- `timeout` must be a non-negative finite number. `float('inf')` is rejected — a deadline that never fires is not a deadline; use `create_connection` directly if you want no bound.
- `NaN` is rejected because every comparison against it is `False`, which would silently disable the guard.
- A `clock` callable can be supplied as the second argument. It defaults to `time.monotonic`.

## Awkward edge

With `timeout=0.0`, the connector raises `ConnectTimeoutError` without attempting the connection, because there is no budget to spend. This is intentional: zero means "already expired". If you need a very small but non-zero budget, pass a small positive float.

## Running the tests

```
PYTHONPATH=src python -m unittest discover -s tests
