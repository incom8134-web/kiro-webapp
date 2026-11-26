"""Browsers always send Origin. The server now rejects a WebSocket that does not.
The test client does not, so give it the page's own origin unless a test set one
(including an empty one, which is how the missing-Origin case is checked)."""
import pytest


@pytest.fixture(autouse=True)
def browser_sends_origin(monkeypatch):
    import starlette.testclient as tc

    orig = tc.TestClient.websocket_connect

    def wrapped(self, url, subprotocols=None, **kwargs):
        headers = kwargs.get("headers") or {}
        if not isinstance(headers, dict):
            headers = dict(headers)
        else:
            headers = dict(headers)
        if not any(str(k).lower() == "origin" for k in headers):
            headers["origin"] = "http://testserver"
        kwargs["headers"] = headers
        return orig(self, url, subprotocols, **kwargs)

    monkeypatch.setattr(tc.TestClient, "websocket_connect", wrapped)


@pytest.fixture(autouse=True)
def battery_state_is_temp(tmp_path, monkeypatch):
    """Cycle count is persisted. Keep that file out of the home directory during tests."""
    monkeypatch.setenv("KIRO_BATT_STATE", str(tmp_path / "battery_state.json"))
