"""UDP socket creation, and the Windows ICMP quirk both endpoints must handle.

Every protocol socket is created here, so there is one place to change when
``ImpairedSocket`` (T4.1) starts wrapping them.

THE WINDOWS ICMP QUIRK
----------------------
On Windows, sending a datagram to a port with nothing bound to it provokes an
ICMP port-unreachable, and the OS reports that back on the *next* ``recvfrom``
of the **sending** socket as ``WSAECONNRESET`` (WinError 10054, surfacing in
Python as ``ConnectionResetError``). The message reads "an existing connection
was forcibly closed", which is doubly misleading for a connectionless protocol:
there is no connection, and the socket remains perfectly usable.

Left unhandled it breaks two things:

* the sender's START retry budget is skipped entirely, so "no receiver is
  running" surfaces as a confusing socket error instead of the clear message
  specs.md §13 asks for;
* a receiver that ACKs a sender which has already exited dies on its next read,
  turning a completed transfer into a crash.

**There is no socket-option fix available from Python.** The Winsock ioctl that
disables this behavior, ``SIO_UDP_CONNRESET`` (0x9800000C), is not exposed as a
``socket`` module constant, and passing the raw control code to ``sock.ioctl``
fails with ``ValueError: invalid ioctl command`` — CPython whitelists only
``SIO_RCVALL``, ``SIO_KEEPALIVE_VALS`` and ``SIO_LOOPBACK_FAST_PATH``. Verified
on this machine, not assumed.

So the handling is the caller's, and it is not optional:

    except ConnectionResetError:
        continue        # a stale ICMP, not a failure; keep waiting

Treat it as "nothing arrived yet" and keep waiting out the current timeout,
rather than as a failed attempt. Returning immediately would collapse a paced
retry budget into microseconds and give up on a receiver that is merely slow to
start. The enclosing deadline still bounds the wait, and at most one such error
arrives per datagram sent, so the loop cannot spin.
"""

from __future__ import annotations

import socket


def open_udp_socket(bind_to: tuple[str, int] | None = None) -> socket.socket:
    """Create a UDP socket for protocol traffic.

    ``bind_to`` binds the socket when given; pass port 0 for an ephemeral port
    and read the real one back with ``getsockname()``.

    Callers must handle ``ConnectionResetError`` on receive — see the module
    docstring for why it cannot be suppressed here.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if bind_to is not None:
        sock.bind(bind_to)
    return sock
