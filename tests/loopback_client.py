"""A TestClient that reaches the app the way a local browser does.

The servers bind to loopback by default and then only answer to loopback Host
headers (a DNS-rebinding defence), so Starlette's default ``testserver`` host
would be rejected with a 400. Tests that aren't about Host handling use this.
"""

from fastapi.testclient import TestClient as _TestClient


class TestClient(_TestClient):
    def __init__(self, app, base_url: str = "http://localhost", **kwargs) -> None:
        super().__init__(app, base_url=base_url, **kwargs)

    def websocket_connect(self, url, subprotocols=None, **kwargs):
        # Starlette's websocket_connect always sends Host: testserver,
        # whatever base_url says; send the Host a browser would.
        headers = {"host": self.base_url.netloc.decode(), **(kwargs.pop("headers", None) or {})}
        return super().websocket_connect(url, subprotocols, headers=headers, **kwargs)
