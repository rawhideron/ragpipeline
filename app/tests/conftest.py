import socket

import pytest

from src import main


@pytest.fixture(autouse=True)
def resolve_hostnames_to_a_public_address(monkeypatch):
    """Every fetch runs the private-address check, so stop tests doing real DNS.

    Tests that care about resolution override main.socket.getaddrinfo themselves.
    """

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]

    monkeypatch.setattr(main.socket, "getaddrinfo", fake_getaddrinfo)
