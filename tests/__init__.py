
"""Ordinary tests cannot open network connections."""
from unittest.mock import patch

def _deny_network(*args, **kwargs):
    raise AssertionError("Offline tests must use injected public responses, not network connections.")

_network_guard = patch("socket.socket.connect", side_effect=_deny_network)
_network_guard.start()
