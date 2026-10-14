import asyncio
import dataclasses
from unittest.mock import patch

import pytest
from loopback_client import TestClient

from sentinella.core.models import SystemSnapshot
from sentinella.remote.client import RemoteCollector
from sentinella.remote.server import create_remote_app


def test_client_parse_roundtrip(dummy_snapshot):
    # Serialize snapshot to dict (as if coming from the remote API)
    serialized = dataclasses.asdict(dummy_snapshot)

    # Introduce an unexpected extra key to test BUG-5 resolution/resilience
    serialized["sensors"]["temperatures"].append(
        {
            "label": "ExtraTemp",
            "current": 42.0,
            "high": 80.0,
            "critical": 90.0,
            "future_field": "some-value",  # Unexpected key
        }
    )

    # Add a mock remote collector instance
    client = RemoteCollector(address="http://localhost:9090", api_key="")

    # Parse it back
    parsed = client._parse(serialized)

    # Verify parsed types
    assert isinstance(parsed, SystemSnapshot)
    assert parsed.system_info.hostname == "test-host"
    assert len(parsed.sensors.temperatures) == 1
    assert parsed.sensors.temperatures[0].label == "ExtraTemp"
    assert parsed.sensors.temperatures[0].current == 42.0


def test_client_parse_robustness():
    # Test with partial keys and null values
    partial_data = {
        "timestamp": 12345.0,
        "cpu": None,
        "memory": None,
        "disk": None,
        "network": None,
        "system_info": None,
        "sensors": None,
        "containers": None,
        "users": None,
        "processes": None,
    }
    client = RemoteCollector(address="http://localhost:9090", api_key="")
    parsed = client._parse(partial_data)

    assert isinstance(parsed, SystemSnapshot)
    assert parsed.timestamp == 12345.0
    assert parsed.cpu.percent_overall == 0.0
    assert parsed.memory.total == 0
    assert parsed.disk.partitions == []
    assert parsed.network.interfaces == []
    assert parsed.processes == []
    assert parsed.users == []
    assert parsed.sensors.temperatures == []
    assert parsed.containers.containers == []


def test_remote_collector_integration(mock_config):

    app = create_remote_app(mock_config)
    client = RemoteCollector(address="http://localhost:9090")

    mock_client = TestClient(app, base_url="http://localhost:9090")
    client._client = mock_client

    snapshot = client.collect()
    assert snapshot is not None
    assert snapshot.cpu is not None
    assert snapshot.timestamp > 0


@pytest.mark.asyncio
async def test_remote_collector_integration_async(mock_config):
    import httpx

    app = create_remote_app(mock_config)
    client = RemoteCollector(address="http://localhost:9090")

    transport = httpx.ASGITransport(app=app)
    mock_client = httpx.AsyncClient(transport=transport, base_url="http://localhost:9090")
    client._async_client = mock_client

    snapshot = await client.collect_async()
    assert snapshot is not None
    assert snapshot.cpu is not None
    assert snapshot.timestamp > 0


def test_remote_collector_auth_failure_propagation():
    from unittest.mock import MagicMock

    import httpx

    client = RemoteCollector(address="http://localhost:9090", api_key="wrong-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="Unauthorized",
        request=MagicMock(),
        response=mock_resp,
    )

    with patch("httpx.Client.get", return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            client.collect()


@pytest.mark.asyncio
async def test_remote_collector_auth_failure_propagation_async():
    from unittest.mock import MagicMock

    import httpx

    client = RemoteCollector(address="http://localhost:9090", api_key="wrong-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="Unauthorized",
        request=MagicMock(),
        response=mock_resp,
    )

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await client.collect_async()


# ── C5: RemoteCollector must not leak its async client ───────────────────────


@pytest.mark.asyncio
async def test_remote_collector_close_async_releases_async_client():
    client = RemoteCollector("http://localhost:9090")
    await client._get_async_client()
    assert client._async_client is not None
    await client.close_async()
    assert client._async_client is None


def test_remote_collector_sync_close_also_releases_async_client():
    """The TUI drives collect_async but tears down via close()."""
    import asyncio

    client = RemoteCollector("http://localhost:9090")
    asyncio.run(client._get_async_client())
    assert client._async_client is not None
    client.close()
    assert client._async_client is None


def test_remote_collector_close_is_idempotent():
    client = RemoteCollector("http://localhost:9090")
    client.close()
    client.close()


@pytest.mark.asyncio
async def test_remote_collector_close_from_running_loop_still_releases_client():
    """close() called synchronously from inside a running event loop used to
    drop the AsyncClient reference without ever calling aclose() on it — a
    real connection-pool leak. It must now schedule the cleanup instead."""
    client = RemoteCollector("http://localhost:9090")
    await client._get_async_client()
    async_client = client._async_client
    assert async_client is not None

    client.close()  # called from within this running loop
    assert client._async_client is None
    assert client._pending_close_tasks  # cleanup was scheduled, not dropped

    await asyncio.gather(*client._pending_close_tasks)
    assert async_client.is_closed


def test_remote_collector_asks_for_the_full_snapshot():
    """Without ?full=true a remote `sentinella print` would silently export a
    truncated, cmdline-less list while a local run exported everything."""
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"timestamp": 1.0, "processes": []})

    collector = RemoteCollector("127.0.0.1:9090")
    collector._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        collector.collect()
    finally:
        collector.close()
    assert "full=true" in seen["url"]


def test_remote_collector_reports_unknown_modules_as_none():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not enabled"})

    remote = RemoteCollector("127.0.0.1:9090")
    remote._async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(remote.collect_module_async("sensors"))
    finally:
        remote.close()
    assert result is None
