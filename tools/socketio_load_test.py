"""Staging-only Socket.IO connection/fanout/reconnect probe.

Requires python-socketio[asyncio_client]. Never points at a public host and
never sends chat messages or notification events. Cookie file: one test-user
Cookie header value per line; do not commit it.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import time
from pathlib import Path
from urllib.parse import urlsplit


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * p)))
    return round(ordered[index], 2)


async def run(args) -> None:
    import socketio

    cookies = [line.strip() for line in Path(args.cookie_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(cookies) < args.clients:
        raise SystemExit("Need one distinct authorized test-session cookie per client")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("Staging load-test token is empty")
    clients = []
    connected = 0
    upgraded = 0
    reconnect_ok = 0
    fanout_received = 0
    latencies_ms: list[float] = []
    sent_at: dict[str, float] = {}
    start = time.monotonic()

    async def connect_one(index: int, client):
        nonlocal connected, upgraded
        try:
            await client.connect(
                args.url,
                headers={"Cookie": cookies[index]},
                socketio_path="socket.io",
                transports=[args.transport],
                wait_timeout=15,
            )
            joined = await client.call("load_test_join", {"token": token}, timeout=10)
            if not joined or not joined.get("ok"):
                await client.disconnect()
                return
            connected += 1
            if client.transport() == "websocket":
                upgraded += 1
        except Exception:
            return

    for index in range(args.clients):
        client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

        async def on_probe(data, _client=client):
            nonlocal fanout_received
            sequence = str((data or {}).get("sequence") or "")
            timestamp = sent_at.get(sequence)
            if timestamp is not None:
                fanout_received += 1
                latencies_ms.append((time.monotonic() - timestamp) * 1000)

        client.on("load_test_probe", on_probe)
        clients.append(client)
    try:
        await asyncio.gather(*(
            staggered_connect(index, client, args.ramp_ms, connect_one)
            for index, client in enumerate(clients)
        ))
        active = [client for client in clients if client.connected]
        initial_connected = len(active)
        initial_upgraded = upgraded
        if not active:
            raise SystemExit("No authorized test clients connected")
        midpoint = time.monotonic() + args.duration / 2
        end = time.monotonic() + args.duration
        sequence_number = 0
        while time.monotonic() < end:
            sequence = str(sequence_number)
            sent_at[sequence] = time.monotonic()
            try:
                await active[0].call("load_test_probe", {"sequence": sequence}, timeout=10)
            except Exception:
                pass
            sequence_number += 1
            if midpoint and time.monotonic() >= midpoint:
                midpoint = 0
                sample = active[1:max(2, len(active) // 10)]
                for client in sample:
                    index = clients.index(client)
                    try:
                        await client.disconnect()
                        await connect_one(index, client)
                        if client.connected:
                            reconnect_ok += 1
                    except Exception:
                        pass
            await asyncio.sleep(args.interval)
        await asyncio.sleep(1)
    finally:
        await asyncio.gather(*(client.disconnect() for client in clients if client.connected), return_exceptions=True)
    result = {
        "requested": args.clients,
        "connected_initial": initial_connected,
        "connection_success_percent": round(100 * initial_connected / args.clients, 2),
        "websocket_upgraded_initial": initial_upgraded,
        "websocket_upgrade_percent": round(100 * initial_upgraded / initial_connected, 2) if initial_connected else 0,
        "reconnect_success": reconnect_ok,
        "fanout_received": fanout_received,
        "probes_sent": len(sent_at),
        "latency_ms_p50": percentile(latencies_ms, 0.50),
        "latency_ms_p95": percentile(latencies_ms, 0.95),
        "latency_ms_p99": percentile(latencies_ms, 0.99),
        "elapsed_seconds": round(time.monotonic() - start, 2),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def staggered_connect(index, client, ramp_ms, connect):
    await asyncio.sleep(index * ramp_ms / 1000)
    await connect(index, client)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Loopback/private staging URL only")
    parser.add_argument("--clients", type=int, choices=(50, 100, 150, 200, 250, 300), required=True)
    parser.add_argument("--cookie-file", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--ramp-ms", type=float, default=100.0)
    parser.add_argument("--transport", choices=("websocket", "polling"), default="websocket")
    parser.add_argument("--staging-only", action="store_true", help="Explicit safety acknowledgment")
    args = parser.parse_args()
    host = urlsplit(args.url).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise SystemExit("Use a literal loopback/private IP; public DNS names are forbidden")
    if not args.staging_only or not (address.is_loopback or address.is_private):
        raise SystemExit("Only explicitly acknowledged private staging targets are allowed")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
