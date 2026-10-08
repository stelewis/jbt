"""Deny observed Python network access during local financial processing."""

from __future__ import annotations

import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class NetworkAccessError(PermissionError):
    """Processing attempted network access, even if the denial was caught."""


@dataclass(slots=True)
class NetworkGuard:
    """Record attempts independently of whether the caller catches denial."""

    attempted: bool = False

    def check(self) -> None:
        """Refuse successful processing after an attempted connection."""
        if self.attempted:
            message = "financial processing network access denied"
            raise NetworkAccessError(message)


_active: NetworkGuard | None = None
_scopes = 0
_scope_lock = RLock()
_installed = False
_DNS_EVENTS = frozenset(
    {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }
)


def _audit(event: str, arguments: tuple[object, ...]) -> None:
    guard = _active
    if guard is None:
        return
    internet = (socket.AF_INET, socket.AF_INET6)
    denied = event in _DNS_EVENTS
    if event == "socket.__new__":
        denied = arguments[1] in internet
    elif event in {"socket.connect", "socket.bind", "socket.sendto"}:
        denied = (
            isinstance(arguments[0], socket.socket) and arguments[0].family in internet
        )
    if denied:
        guard.attempted = True
        guard.check()


@contextmanager
def processing() -> Iterator[NetworkGuard]:
    """Scope the application policy without guarding external provisioning."""
    global _active, _scopes, _installed  # noqa: PLW0603 - process-wide audit scope
    with _scope_lock:
        if not _installed:
            sys.addaudithook(_audit)
            _installed = True
        if _active is None:
            _active = NetworkGuard()
        guard = _active
        _scopes += 1
    try:
        yield guard
        guard.check()
    finally:
        with _scope_lock:
            _scopes -= 1
            if _scopes == 0:
                _active = None
