"""Hub behavior: error mapping, per-property dispatch, availability."""

from __future__ import annotations

import logging
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from websockets.exceptions import WebSocketException

from custom_components.wattpilot.api import AuthenticationError, WattpilotError
from custom_components.wattpilot.api.exceptions import CommandError
from custom_components.wattpilot.const import signal_availability, signal_property
from custom_components.wattpilot.hub import WattpilotHub

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


def make_hub(
    hass: HomeAssistant,
    charger: FakeWattpilot,
    update_interval: timedelta = timedelta(0),
) -> WattpilotHub:
    return WattpilotHub(hass, ENTRY_ID, charger, update_interval)  # type: ignore[arg-type]


async def test_first_connect_is_not_announced_as_a_recovery(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A charger that has only just been set up was never away.

    Availability now runs through one method for connect, disconnect and the
    periodic check, so the first connect passes through the same code that
    reports a recovery. It has to tell the two apart by `_last_available`
    still being None -- which is exactly what makes that initial value
    load-bearing rather than a formality.
    """
    hub = make_hub(hass, fake_charger)
    with caplog.at_level(logging.DEBUG):
        await hub.async_connect()
    messages = [record.getMessage() for record in caplog.records]
    assert "Charger 123456 is back online" not in messages
    assert "Charger 123456 connected" in messages


async def test_create_local_builds_client_with_host_and_password(
    hass: HomeAssistant,
) -> None:
    """The credentials the config entry holds must reach the client verbatim."""
    with patch("custom_components.wattpilot.hub.Wattpilot") as client:
        hub = WattpilotHub.create_local(hass, ENTRY_ID, "192.168.1.50", "secret")
    assert client.call_args.args == ()
    assert client.call_args.kwargs == {"host": "192.168.1.50", "password": "secret"}
    # Not cosmetic: every dispatcher signal is namespaced by this id, so a
    # wrong one silently detaches every entity from its pushes.
    assert hub._entry_id == ENTRY_ID


# The message matters as much as the exception type: it is what Home Assistant
# shows the user on the config entry, and a bare "None" there is useless.
async def test_connect_maps_auth_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger.connect_error = AuthenticationError("wrong password")
    hub = make_hub(hass, fake_charger)
    with pytest.raises(ConfigEntryAuthFailed, match="wrong password"):
        await hub.async_connect()


async def test_connect_maps_connection_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger.connect_error = ConnectionError("timeout")
    hub = make_hub(hass, fake_charger)
    with pytest.raises(ConfigEntryNotReady, match="Cannot connect to charger: timeout"):
        await hub.async_connect()


async def test_connect_maps_wattpilot_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger.connect_error = WattpilotError("boom")
    hub = make_hub(hass, fake_charger)
    with pytest.raises(ConfigEntryNotReady, match="Cannot connect to charger: boom"):
        await hub.async_connect()


async def test_connect_marks_available_so_first_push_is_no_transition(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A successful connect already establishes availability.

    Without that, the very first property push would look like an
    unavailable -> available transition and fire a spurious signal."""
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    events: list[bool] = []

    @callback
    def _collect(available: bool) -> None:  # noqa: FBT001 -- dispatcher callback arg
        events.append(available)

    async_dispatcher_connect(hass, signal_availability(ENTRY_ID), _collect)
    fake_charger.push("amp", 16)
    await hass.async_block_till_done()
    assert events == []


async def test_get_property_returns_default_for_unknown_key(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    hub = make_hub(hass, fake_charger)
    assert hub.get_property("no_such_key", "fallback") == "fallback"
    assert hub.get_property("no_such_key") is None


async def test_property_push_dispatches_to_exact_signal(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    received: dict[str, list[Any]] = {"amp": [], "eto": []}
    for key in received:  # noqa: PLC0206 -- key drives two things, not just values

        @callback
        def _collect(value: Any, key: str = key) -> None:
            received[key].append(value)

        async_dispatcher_connect(hass, signal_property(ENTRY_ID, key), _collect)
    fake_charger.push("amp", 16)
    await hass.async_block_till_done()
    assert received == {"amp": [16], "eto": []}


async def test_availability_transitions_fire_once(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    events: list[bool] = []

    @callback
    def _collect(available: bool) -> None:  # noqa: FBT001 -- dispatcher callback arg
        events.append(available)

    async_dispatcher_connect(hass, signal_availability(ENTRY_ID), _collect)

    # Connection drops silently -> the 30s interval check must notice, once.
    fake_charger.connected = False
    for _ in range(3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
    assert events == [False]

    # A property push while reconnected must flip it back immediately.
    fake_charger.connected = True
    fake_charger.push("amp", 10)
    await hass.async_block_till_done()
    assert events == [False, True]


async def test_set_property_maps_errors(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    fake_charger.set_error = ConnectionError("socket closed")
    with pytest.raises(HomeAssistantError, match="Failed to set amp: socket closed"):
        await hub.async_set_property("amp", 16)


async def test_set_property_maps_websocket_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    fake_charger.set_error = WebSocketException("closed")
    with pytest.raises(HomeAssistantError, match="Failed to set amp: closed"):
        await hub.async_set_property("amp", 16)


@pytest.mark.parametrize(
    ("write", "message"),
    [
        (lambda hub: hub.async_set_next_trip(time(7, 30)), "Failed to set next trip"),
        (
            lambda hub: hub.async_set_next_trip_energy(25000),
            "Failed to set next trip energy",
        ),
        (lambda hub: hub.async_enable_cloud_api(), "Failed to enable cloud API"),
        (lambda hub: hub.async_disable_cloud_api(), "Failed to disable cloud API"),
        (lambda hub: hub.async_install_firmware("42.6"), "Firmware update failed"),
    ],
    ids=["next_trip", "next_trip_energy", "cloud_on", "cloud_off", "firmware"],
)
async def test_write_errors_surface_with_their_cause(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    write: Callable[[WattpilotHub], Awaitable[object]],
    message: str,
) -> None:
    """Every write path must name what failed and why, not just raise."""
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    fake_charger.write_error = ConnectionError("socket closed")
    with pytest.raises(HomeAssistantError, match=f"{message}: socket closed"):
        await write(hub)


async def test_shutdown_disconnects_and_stops_dispatch(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    await hub.async_shutdown()
    assert fake_charger.disconnect_count == 1
    assert fake_charger._callbacks == []


async def test_shutdown_maps_disconnect_error_and_still_clears_callbacks(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A failing disconnect must surface as HomeAssistantError, but the
    hub's handle cleanup (unsubscribe, cancel timer) must already have
    happened -- a failed shutdown must not leave the hub half-alive."""
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    fake_charger.disconnect_error = ConnectionError("socket closed")
    with pytest.raises(HomeAssistantError, match="Failed to disconnect: socket closed"):
        await hub.async_shutdown()
    assert fake_charger._callbacks == []
    assert hub._unsubscribe_properties is None
    assert hub._cancel_timer is None


async def test_start_dispatch_is_idempotent(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """reconnect_charger calls start_dispatch again; no duplicate callbacks."""
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    hub.start_dispatch()
    assert len(fake_charger._callbacks) == 1


async def test_update_interval_coalesces_pushes_into_one_latest_value(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    With an interval set, a burst of pushes is held and delivered as a
    single latest value when the interval elapses -- at most one update per
    property per interval, which is the whole point of the setting.
    """
    hub = make_hub(hass, fake_charger, timedelta(seconds=5))
    await hub.async_connect()
    hub.start_dispatch()
    received: list[Any] = []

    @callback
    def _collect(value: Any) -> None:
        received.append(value)

    async_dispatcher_connect(hass, signal_property(ENTRY_ID, "amp"), _collect)

    fake_charger.push("amp", 6)
    fake_charger.push("amp", 10)
    fake_charger.push("amp", 16)
    await hass.async_block_till_done()
    assert received == []  # held, not dispatched yet

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert received == [16]  # only the latest, exactly once


async def test_zero_update_interval_dispatches_immediately(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Interval 0 is the pass-through default: no buffering, no timer."""
    hub = make_hub(hass, fake_charger, timedelta(0))
    await hub.async_connect()
    hub.start_dispatch()
    received: list[Any] = []

    @callback
    def _collect(value: Any) -> None:
        received.append(value)

    async_dispatcher_connect(hass, signal_property(ENTRY_ID, "amp"), _collect)
    fake_charger.push("amp", 16)
    await hass.async_block_till_done()
    assert received == [16]
    assert hub._cancel_flush is None  # no flush timer when not throttling


async def test_availability_is_not_delayed_by_the_update_interval(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    A dropped connection must show at once, not up to an interval later.

    The value buffer holds property pushes; availability must skip it, or a
    dead charger would keep displaying live-looking readings until the next
    flush.
    """
    hub = make_hub(hass, fake_charger, timedelta(seconds=5))
    await hub.async_connect()
    hub.start_dispatch()
    events: list[bool] = []

    @callback
    def _collect(available: bool) -> None:  # noqa: FBT001 -- dispatcher callback arg
        events.append(available)

    async_dispatcher_connect(hass, signal_availability(ENTRY_ID), _collect)
    fake_charger.connected = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert events == [False]


async def test_shutdown_drops_buffered_values(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Buffered pushes must not be dispatched to entities after shutdown."""
    hub = make_hub(hass, fake_charger, timedelta(seconds=5))
    await hub.async_connect()
    hub.start_dispatch()
    received: list[Any] = []

    @callback
    def _collect(value: Any) -> None:
        received.append(value)

    async_dispatcher_connect(hass, signal_property(ENTRY_ID, "amp"), _collect)
    fake_charger.push("amp", 16)
    await hub.async_shutdown()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert received == []
    assert hub._cancel_flush is None


async def test_deliberate_shutdown_is_not_logged_as_a_warning(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A normal shutdown (unload, HA stop, config-flow probe) marks the charger
    unavailable -- but that is expected, not a fault. Logging it at WARNING
    turned every clean teardown into a scary line in the log.
    """
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    with caplog.at_level(logging.DEBUG):
        await hub.async_shutdown()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Charger 123456 is unavailable" not in warnings
    messages = [r.getMessage() for r in caplog.records]
    assert "Charger 123456 disconnected" in messages


async def test_unexpected_disconnect_is_still_warned(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A drop the device did not announce is a real fault and must warn."""
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    hub.start_dispatch()
    fake_charger.connected = False
    with caplog.at_level(logging.WARNING):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Charger 123456 is unavailable" in warnings


async def test_a_failed_disconnect_still_reports_unavailable(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Audit VA-04: `available` used to read the vendor client's `connected`
    flag, which that client clears only after a successful ws.close(). A
    close that raises left the flag True while its message loop was already
    gone -- entities kept claiming a live charger that could never update
    them again.

    The hub owns the answer for its own teardown, whatever the client's flag
    says afterwards.
    """
    hub = WattpilotHub(hass, ENTRY_ID, fake_charger)  # type: ignore[arg-type]
    await hub.async_connect()
    assert hub.available

    fake_charger.disconnect_error = ConnectionError("close failed")
    with pytest.raises(HomeAssistantError):
        await hub.async_shutdown()

    assert fake_charger.connected, "precondition: the client's own flag stays set"
    assert not hub.available


async def test_a_refused_write_reaches_the_user_as_an_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Audit VA-03, from the outside: the charger refusing a value must fail
    the action, not end quietly. The vendored client raises CommandError for
    that now; the hub has to keep translating it rather than let it escape as
    a library exception."""
    hub = make_hub(hass, fake_charger)
    await hub.async_connect()
    fake_charger.set_error = CommandError("Charger rejected command 1: nope")

    with pytest.raises(HomeAssistantError, match="Failed to set amp"):
        await hub.async_set_property("amp", 10)


async def test_a_fresh_hub_does_not_claim_to_be_torn_down(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """`available` answers from two things: the charger's own flag and
    whether this hub has torn itself down. The second must start false --
    a hub that never tore anything down claiming otherwise would report an
    established charger as unavailable, and only until the next connect."""
    fake_charger.connected = True
    hub = WattpilotHub(hass, ENTRY_ID, fake_charger)  # type: ignore[arg-type]

    assert hub.available
