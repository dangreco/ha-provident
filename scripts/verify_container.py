#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

"""Boot Home Assistant in a container against the fake portal and check the integration end to end.

Run on the host: .venv/bin/python scripts/verify_container.py
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HA_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.10.2"
CONTAINER_PORT = 8123
PROVIDENT_USERNAME = "demo"
PROVIDENT_PASSWORD = "hunter2"

PORTAL_HOSTNAME = "provident.meterconnex.com"


def detect_runtime() -> str:
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    raise SystemExit("neither podman nor docker is installed")


def make_certs(directory: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ha-provident test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, PORTAL_HOSTNAME)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(PORTAL_HOSTNAME)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    (directory / "ca.pem").write_bytes(ca.public_bytes(pem))
    (directory / "portal.pem").write_bytes(leaf.public_bytes(pem))
    (directory / "portal.key").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


def log(message: str) -> None:
    print(f"[verify] {message}", flush=True)


def http_json(method: str, url: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def http_form(url: str, form: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


async def ws_rpc(ws_url: str, token: str, messages: list[dict]) -> list[dict]:
    import aiohttp

    results = []
    timeout = aiohttp.ClientTimeout(total=120)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.ws_connect(ws_url, max_msg_size=0) as ws,
    ):
        auth = await ws.receive_json()
        assert auth["type"] == "auth_required", auth
        await ws.send_json({"type": "auth", "access_token": token})
        ok = await ws.receive_json()
        assert ok["type"] == "auth_ok", ok
        for message in messages:
            await ws.send_json(message)
            response = await ws.receive_json()
            assert response.get("success"), response
            results.append(response["result"])
    return results


def wait_for_ha(base_url: str, timeout: float = 240.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/onboarding", timeout=5) as r:
                json.loads(r.read())
            return
        except Exception:
            time.sleep(2)
    raise TimeoutError("Home Assistant did not come up")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=HA_IMAGE)
    parser.add_argument("--keep", action="store_true", help="keep the container")
    args = parser.parse_args()

    runtime = detect_runtime()
    workdir = Path(tempfile.mkdtemp(prefix="ha-provident-verify-"))
    config = workdir / "config"
    certs = workdir / "certs"
    (config / "custom_components").mkdir(parents=True)
    certs.mkdir()
    make_certs(certs)
    # SSL_CERT_FILE replaces the trust store and HA still needs PyPI, so bundle system roots + test CA.
    system_roots = subprocess.run(
        [runtime, "run", "--rm", "--entrypoint", "cat", args.image, "/etc/ssl/cert.pem"],
        check=True,
        capture_output=True,
    ).stdout
    (certs / "bundle.pem").write_bytes(system_roots + b"\n" + (certs / "ca.pem").read_bytes())
    (config / "configuration.yaml").write_text(
        "default_config:\nlogger:\n  logs:\n    custom_components.provident: debug\n"
    )
    suffix = str(os.getpid())
    ha_name = f"ha-provident-verify-{suffix}"
    portal_name = f"ha-provident-portal-{suffix}"
    network = f"ha-provident-net-{suffix}"
    base_url = f"http://127.0.0.1:{CONTAINER_PORT}"

    def cleanup() -> None:
        for name in (ha_name, portal_name):
            subprocess.run([runtime, "rm", "-f", name], check=False, capture_output=True)
        subprocess.run([runtime, "network", "rm", network], check=False, capture_output=True)
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    portal_container = [
        runtime,
        "run",
        "-d",
        "--name",
        portal_name,
        "--network",
        network,
        "--network-alias",
        PORTAL_HOSTNAME,
        "--entrypoint",
        "python3",
        "-v",
        f"{REPO / 'tests' / 'fixtures'}:/srv/fixtures:ro,Z",
        "-v",
        f"{REPO / 'scripts' / 'portal_server.py'}:/srv/portal_server.py:ro,Z",
        "-v",
        f"{certs}:/certs:ro,Z",
        args.image,
        "/srv/portal_server.py",
    ]
    container = [
        runtime,
        "run",
        "-d",
        "--name",
        ha_name,
        "--network",
        network,
        "-v",
        f"{config}:/config:Z",
        # Bind mount: a symlink to a host path would dangle in the container.
        "-v",
        f"{REPO / 'custom_components' / 'provident'}:/config/custom_components/provident:ro,Z",
        "-v",
        f"{certs}:/certs:ro,Z",
        "-e",
        "SSL_CERT_FILE=/certs/bundle.pem",
        "-p",
        f"127.0.0.1:{CONTAINER_PORT}:{CONTAINER_PORT}",
        args.image,
    ]
    try:
        subprocess.run([runtime, "network", "create", network], check=True, capture_output=True)
        subprocess.run(portal_container, check=True, capture_output=True)
        subprocess.run(container, check=True, capture_output=True)
        log(f"containers started; portal answers as https://{PORTAL_HOSTNAME}/ on {network}")
        wait_for_ha(base_url)
        log("HA is up; onboarding")

        auth_code = http_json(
            "POST",
            f"{base_url}/api/onboarding/users",
            body={
                "client_id": base_url + "/",
                "name": "Verifier",
                "username": "verify@example.com",
                "password": "verify-password-123",
                "language": "en",
            },
        )["auth_code"]
        token_resp = http_form(
            f"{base_url}/auth/token",
            {
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": base_url + "/",
            },
        )
        token = token_resp["access_token"]
        log("owner user created, token acquired")

        ws_url = base_url.replace("http", "ws", 1) + "/api/websocket"
        log("running config flow over REST")
        flow = http_json(
            "POST",
            f"{base_url}/api/config/config_entries/flow",
            token=token,
            body={"handler": "provident", "show_advanced_options": False},
        )
        assert flow["step_id"] == "user", flow
        created = http_json(
            "POST",
            f"{base_url}/api/config/config_entries/flow/{flow['flow_id']}",
            token=token,
            body={
                "username": PROVIDENT_USERNAME,
                "password": PROVIDENT_PASSWORD,
            },
        )
        assert created["type"] == "create_entry", created
        entry_id = created["result"]["entry_id"]
        log(f"config entry created: {entry_id}")

        deadline = time.monotonic() + 120
        states = None
        while time.monotonic() < deadline:
            states = http_json("GET", f"{base_url}/api/states", token=token)
            if any(s["entity_id"] == "sensor.3105_electricity_total_usage" for s in states):
                break
            time.sleep(2)
        by_id = {s["entity_id"]: s for s in states or []}
        electricity = by_id.get("sensor.3105_electricity_total_usage")
        assert electricity is not None, f"sensors missing: {list(by_id)[:5]}"
        assert electricity["attributes"]["device_class"] == "energy"
        assert electricity["attributes"]["state_class"] == "total_increasing"
        assert electricity["state"] not in ("unknown", "unavailable")
        water = by_id["sensor.3105_cold_water_total_usage"]
        assert water["attributes"]["device_class"] == "water"
        log(f"sensors live: electricity={electricity['state']} water={water['state']}")

        log("waiting for statistics import")
        deadline = time.monotonic() + 180
        stats = []
        while time.monotonic() < deadline:
            results = asyncio.run(
                ws_rpc(
                    ws_url,
                    token,
                    [
                        {
                            "id": 3,
                            "type": "recorder/statistics_during_period",
                            "start_time": (datetime.now(UTC) - timedelta(days=40)).isoformat(),
                            "end_time": datetime.now(UTC).isoformat(),
                            "statistic_ids": ["provident:85006408_9_hourly_consumption"],
                            "period": "hour",
                        },
                    ],
                )
            )
            stats = results[0].get("provident:85006408_9_hourly_consumption", [])
            if len(stats) > 24:
                break
            time.sleep(3)
        assert len(stats) > 24, f"only {len(stats)} statistics imported"
        sums = [row["sum"] for row in stats]
        assert all(b >= a for a, b in itertools.pairwise(sums)), "sums not monotonic"
        log(f"statistics: {len(stats)} hourly rows, monotonic sums")

        log("register statistic validates as an energy grid source")
        results = asyncio.run(
            ws_rpc(
                ws_url,
                token,
                [
                    {
                        "id": 4,
                        "type": "energy/save_prefs",
                        "energy_sources": [
                            {
                                "type": "grid",
                                "stat_energy_from": "provident:85006408_9_hourly_consumption",
                                "stat_cost": None,
                                "entity_energy_price": None,
                                "number_energy_price": None,
                                "cost_adjustment_day": 0.0,
                            },
                            {
                                "type": "water",
                                "stat_energy_from": "provident:23166694_2_hourly_consumption",
                                "stat_cost": None,
                                "entity_energy_price": None,
                                "number_energy_price": None,
                            },
                        ],
                        "device_consumption": [],
                    },
                ],
            )
        )
        log("energy prefs saved; running the dashboard's own validator")
        validation = asyncio.run(ws_rpc(ws_url, token, [{"id": 5, "type": "energy/validate"}]))[0]
        issues = [i for source in validation["energy_sources"] for i in source]
        assert not issues, f"energy validation issues: {issues}"
        log("energy dashboard validation: no issues for grid and water sources")

        log("reloading entry")
        http_json(
            "POST",
            f"{base_url}/api/config/config_entries/entry/{entry_id}/reload",
            token=token,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            states = http_json("GET", f"{base_url}/api/states", token=token)
            by_id = {s["entity_id"]: s for s in states}
            reloaded = by_id.get("sensor.3105_electricity_total_usage")
            if reloaded and reloaded["state"] not in ("unknown", "unavailable"):
                break
            time.sleep(2)
        else:
            raise AssertionError("sensor did not return after reload")
        log("entry reloaded; sensors recovered")

        log("removing entry")
        http_json(
            "DELETE",
            f"{base_url}/api/config/config_entries/entry/{entry_id}",
            token=token,
        )
        states = http_json("GET", f"{base_url}/api/states", token=token)
        left = [s["entity_id"] for s in states if "3105" in s["entity_id"]]
        assert not left, f"entities left after removal: {left}"
        log("entry removed; entities cleaned up")

        log("ALL CHECKS PASSED")
        return 0
    finally:
        if args.keep:
            log(f"keeping containers {ha_name}, {portal_name} and config {config}")
        cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log(f"FAILED: {error}")
        raise
