"""Unit tests for the WebSocket fan-out.

These exist because the loop used to be a closure inside a 300-line factory:
the only way to reach it was to boot a whole FastAPI app, so its failure
handling went untested and a bug that silently froze every dashboard shipped.
"""

import asyncio

import pytest

from sentinella.core.broadcast import BroadcastHub


class FakeWS:
    """A socket that records what it was sent and how it was closed."""

    def __init__(self, fail=False, hang=False):
        self.sent = []
        self.closed_with = None
        self._fail = fail
        self._hang = hang

    async def send_text(self, payload):
        if self._hang:
            await asyncio.sleep(3600)
        if self._fail:
            raise ConnectionResetError("client went away")
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


def make_hub(snapshots, **kwargs):
    """A hub whose source yields *snapshots* in turn, raising any exception."""
    seq = iter(snapshots)

    async def source():
        value = next(seq)
        if isinstance(value, Exception):
            raise value
        return value

    kwargs.setdefault("interval", 0.01)
    return BroadcastHub(snapshot_source=source, serializer=lambda s: s, **kwargs)


@pytest.mark.asyncio
async def test_broadcast_sends_to_every_client():
    hub = make_hub([{"a": 1}])
    a, b = FakeWS(), FakeWS()
    hub.add(a)
    hub.add(b)

    await hub.broadcast_once()

    assert a.sent == ['{"a": 1}']
    assert b.sent == ['{"a": 1}']


@pytest.mark.asyncio
async def test_a_failing_client_is_dropped_without_affecting_the_others():
    hub = make_hub([{"a": 1}])
    good, bad = FakeWS(), FakeWS(fail=True)
    hub.add(good)
    hub.add(bad)

    await hub.broadcast_once()

    assert good.sent == ['{"a": 1}']
    assert bad not in hub.clients
    assert good in hub.clients


@pytest.mark.asyncio
async def test_a_stalled_client_cannot_block_the_others():
    """One socket that never drains must not hold up the whole fan-out."""
    hub = make_hub([{"a": 1}], send_timeout=0.01)
    stalled, good = FakeWS(hang=True), FakeWS()
    hub.add(stalled)
    hub.add(good)

    await asyncio.wait_for(hub.broadcast_once(), timeout=2)

    assert good.sent == ['{"a": 1}']
    assert stalled not in hub.clients
    assert stalled.closed_with == (1011, "Send timeout")


@pytest.mark.asyncio
async def test_the_loop_survives_a_failing_cycle():
    hub = make_hub([RuntimeError("boom"), {"ok": 1}, {"ok": 2}])
    ws = FakeWS()
    hub.add(ws)

    hub.start()
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    await hub.stop()

    assert ws.sent, "the loop stopped after the first failed cycle"


@pytest.mark.asyncio
async def test_repeated_failures_close_the_clients_so_they_reconnect():
    """A permanently broken source must not leave browsers on a live-looking
    socket — closing them hands control to their own reconnect backoff."""
    hub = make_hub([RuntimeError("boom")] * 20, max_failures=3)
    ws = FakeWS()
    hub.add(ws)

    hub.start()
    for _ in range(300):
        if ws.closed_with:
            break
        await asyncio.sleep(0.01)
    await hub.stop()

    assert ws.closed_with == (1011, "Broadcast failure")
    assert ws not in hub.clients


@pytest.mark.asyncio
async def test_the_loop_is_idle_with_no_clients():
    """No clients means no collection — a dashboard nobody is watching should
    not keep the machine busy."""
    collected = {"n": 0}

    async def source():
        collected["n"] += 1
        return {}

    hub = BroadcastHub(snapshot_source=source, serializer=lambda s: s, interval=0.01)
    hub.start()
    await asyncio.sleep(0.1)
    await hub.stop()

    assert collected["n"] == 0


def test_capacity_limit():
    hub = BroadcastHub(snapshot_source=None, serializer=lambda s: s, interval=1, max_clients=2)
    assert not hub.is_full()
    hub.add(FakeWS())
    hub.add(FakeWS())
    assert hub.is_full()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_safe_before_start():
    hub = make_hub([{}])
    await hub.stop()  # never started
    hub.start()
    await hub.stop()
    await hub.stop()
