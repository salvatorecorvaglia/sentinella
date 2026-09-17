import dataclasses

import pytest

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
    from fastapi.testclient import TestClient

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
    from unittest.mock import MagicMock, patch

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
    from unittest.mock import MagicMock, patch

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
