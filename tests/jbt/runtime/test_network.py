import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from jbt.runtime.network import NetworkAccessError, processing

# The scope must exit after a caught denial to test the persistent attempt flag.
# ruff: noqa: PT012


@pytest.mark.parametrize("attempt", ["socket", "dns"])
def test_caught_network_attempt_is_still_failure(attempt: str) -> None:
    with pytest.raises(NetworkAccessError), processing():
        try:
            if attempt == "socket":
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            else:
                socket.getaddrinfo("example.invalid", 443)
        except NetworkAccessError:
            pass


def test_nested_processing_scopes_share_attempts() -> None:
    with pytest.raises(NetworkAccessError), processing() as outer:
        try:
            with processing() as inner:
                assert inner is outer
                socket.getaddrinfo("example.invalid", 443)
        except NetworkAccessError:
            pass
        outer.check()


def test_inactive_scope_does_not_block_local_provisioning() -> None:
    with processing() as guard:
        guard.check()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
        pass


def test_processing_threads_cannot_escape_the_scope() -> None:
    with pytest.raises(NetworkAccessError), processing(), ThreadPoolExecutor() as pool:
        future = pool.submit(socket.getaddrinfo, "example.invalid", 443)
        with pytest.raises(NetworkAccessError):
            future.result()
