# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from provident import AsyncClient
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.provident.const import CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS, DOMAIN

from .fixtures.portal import PASSWORD as PORTAL_PASSWORD
from .fixtures.portal import USERNAME as PORTAL_USERNAME
from .fixtures.portal import make_portal

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use Home Assistant's stable snapshot serializer/dirname."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


def _warm_provident_date_types() -> None:
    """Cache the real ``date`` in the pyo3 extension before freezegun's FakeDate can poison it."""
    from datetime import date

    from provident import Client

    with contextlib.suppress(Exception):  # only the argument conversion matters
        Client(base_url="http://127.0.0.1:9/", connect_timeout=1).usage.series(
            "MP:1", date(2026, 1, 1), date(2026, 1, 2)
        )


_warm_provident_date_types()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request) -> None:
    """Skipped for ``recorder_mock`` tests, where ``patch_client`` clears the cache instead."""
    if "recorder_mock" not in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")


@pytest.fixture
def portal() -> Generator[Any]:
    import pytest_socket

    # The portal server needs real sockets; teardown restores PHACC's loopback-only policy.
    pytest_socket.enable_socket()
    fake, server, thread = make_portal()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        pytest_socket.socket_allow_hosts(["127.0.0.1"])
        pytest_socket.disable_socket(allow_unix_socket=True)


@pytest.fixture
def portal_factory() -> Generator[Any]:
    servers: list[Any] = []

    def _make():
        fake, server, _thread = make_portal()
        servers.append((server,))
        return fake

    yield _make
    for (server,) in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def patch_client(portal: Any, hass: HomeAssistant) -> AsyncGenerator[None]:
    from homeassistant import loader

    await hass.config.async_set_time_zone("UTC")
    await hass.async_block_till_done()
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    with (
        patch(
            "custom_components.provident.new_client",
            lambda: AsyncClient(base_url=portal.url, rates_base_url=portal.url),
        ),
        patch(
            "custom_components.provident.coordinator.new_client",
            lambda: AsyncClient(base_url=portal.url, rates_base_url=portal.url),
        ),
    ):
        yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Provident ({PORTAL_USERNAME})",
        data={
            CONF_USERNAME: PORTAL_USERNAME,
            CONF_PASSWORD: PORTAL_PASSWORD,
            CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
        },
        unique_id=PORTAL_USERNAME,
    )


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant,
    patch_client: None,
    mock_config_entry: MockConfigEntry,
) -> AsyncGenerator[MockConfigEntry]:
    await hass.config.async_set_time_zone("UTC")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    # Wait for background tasks too: the first statistics import runs as one.
    await hass.async_block_till_done(wait_background_tasks=True)
    # MockConfigEntry resolves to Any (PHACC ships no py.typed).
    return mock_config_entry  # type: ignore[no-any-return]
