"""
The seam between the real client and the config entry.

What a stand-in for the client cannot show: a whole sequence of connections.
Each case disturbs one stage, and the charger is healthy afterwards.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from custom_components.wattpilot.api.exceptions import WattpilotConnectionError
from custom_components.wattpilot.const import DOMAIN

from .scripted_charger import (
    AUTH_REQUIRED,
    HELLO,
    ScriptedCharger,
    ScriptedEntry,
    until,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NO_SERIAL = {key: value for key, value in HELLO.items() if key != "serial"}
UNUSABLE_HANDSHAKES = {
    "hello without a serial": (NO_SERIAL, AUTH_REQUIRED),
    "hello with an empty serial": ({**HELLO, "serial": ""}, AUTH_REQUIRED),
    "auth request with an empty token": (HELLO, {**AUTH_REQUIRED, "token1": ""}),
    "auth request with a null hash": (HELLO, {**AUTH_REQUIRED, "hash": None}),
    "auth request with a blank hash": (HELLO, {**AUTH_REQUIRED, "hash": " "}),
    "auth request with an empty hash": (HELLO, {**AUTH_REQUIRED, "hash": ""}),
    "auth request with a number as hash": (HELLO, {**AUTH_REQUIRED, "hash": 5}),
    "auth request with a list as hash": (HELLO, {**AUTH_REQUIRED, "hash": []}),
}
# Who mostly takes the connection after a reboot: as shipped the install's own
# attempt, with a backoff below the pause the reader. Both race, so a single
# install proves nothing about the path it took.
TIMINGS = {"install": {}, "reader": {"backoff": 0.01, "pause": 0.3}}


@pytest.mark.parametrize("case", list(UNUSABLE_HANDSHAKES))
async def test_one_unusable_handshake_does_not_end_the_reconnecting(
    hass: HomeAssistant, case: str
) -> None:
    """
    The frame may be a passing state of a charger that is starting up.

    Treated as a refusal it stopped the reconnecting for good: every entity
    stayed unavailable until a manual reload, behind a repair notice saying
    that retrying would only repeat the refusal.
    """
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger) as scripted:
        charger.next_handshakes.append(UNUSABLE_HANDSHAKES[case])
        connections = len(charger.sockets)
        charger.sockets[-1].end()
        assert await until(lambda: len(charger.sockets) >= connections + 2)
        assert await until(lambda: scripted.hub.charger.properties_initialized)
        assert scripted.hub.charger.refusal is None
        assert not [issue for issue in ir.async_get(hass).issues if issue[0] == DOMAIN]


async def test_an_unknown_sign_in_scheme_stays_a_refusal(hass: HomeAssistant) -> None:
    """A name is a statement of the charger: retrying would only repeat it."""
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger) as scripted:
        client = scripted.hub.charger
        charger.next_handshakes.append((HELLO, {**AUTH_REQUIRED, "hash": "scrypt"}))
        charger.sockets[-1].end()
        assert await until(lambda: client.refusal is not None)
        assert "scrypt" in str(client.refusal)


async def test_a_lasting_unusable_handshake_says_why_in_the_log(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """No notice and no refusal: the log is the only place left to find it."""
    caplog.set_level(logging.DEBUG)
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger):
        charger.next_handshakes.extend(
            [UNUSABLE_HANDSHAKES["hello without a serial"]] * 2
        )
        connections = len(charger.sockets)
        charger.sockets[-1].end()
        assert await until(lambda: len(charger.sockets) >= connections + 3)
    assert "Charger sent hello without a usable serial" in caplog.text


async def test_a_frame_behind_an_unusable_handshake_is_not_answered(
    hass: HomeAssistant,
) -> None:
    """The connection is over: an answer would go out on a dead attempt."""
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger):
        charger.next_handshakes.append(UNUSABLE_HANDSHAKES["hello without a serial"])
        connections = len(charger.sockets)
        charger.sockets[-1].end()
        assert await until(lambda: len(charger.sockets) >= connections + 2)
        assert charger.sockets[connections].sent == []


async def test_a_setup_against_an_unusable_handshake_is_retried_and_named(
    hass: HomeAssistant,
) -> None:
    """Loaded without data, nobody would ever learn what the charger sent."""
    charger = ScriptedCharger()
    charger.next_handshakes.extend([UNUSABLE_HANDSHAKES["hello without a serial"]] * 5)
    async with ScriptedEntry(hass, charger) as scripted:
        assert scripted.entry.state is ConfigEntryState.SETUP_RETRY
        assert "hello without a usable serial" in scripted.entry.reason


async def test_a_failed_install_leaves_the_reconnecting_running(
    hass: HomeAssistant,
) -> None:
    """Nothing else would bring the connection back to a charger that recovers."""
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger) as scripted:
        charger.next_handshakes.extend(
            [UNUSABLE_HANDSHAKES["hello without a serial"]] * 500
        )
        with pytest.raises(WattpilotConnectionError, match="Timeout reconnecting"):
            await scripted.hub.charger.install_firmware_update("43.9", timeout=0.4)
        charger.next_handshakes.clear()
        assert await until(lambda: scripted.hub and scripted.hub.charger.connected)


@pytest.mark.parametrize("opener", TIMINGS)
async def test_each_timing_reaches_the_path_it_stands_for(
    hass: HomeAssistant, opener: str
) -> None:
    """With both cases on one path, the other would go untested unnoticed."""
    charger = ScriptedCharger()
    first_after_reboot = []
    async with ScriptedEntry(hass, charger, **TIMINGS[opener]) as scripted:
        for version in ("43.7", "43.8", "43.9"):
            hub = scripted.hub
            connections = len(charger.opened_by)
            await hub.async_install_firmware(version)
            first_after_reboot.append(charger.opened_by[connections])
            assert await until(lambda: scripted.hub not in (None, hub))  # noqa: B023
            await hass.async_block_till_done()
    assert opener in first_after_reboot


@pytest.mark.parametrize("opener", TIMINGS)
async def test_an_unusable_handshake_during_an_install_does_not_fail_it(
    hass: HomeAssistant, opener: str
) -> None:
    """The firmware is on the charger by then: only the connection hiccuped."""
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger, **TIMINGS[opener]) as scripted:
        hub = scripted.hub
        charger.next_handshakes.append(UNUSABLE_HANDSHAKES["hello without a serial"])
        await hub.async_install_firmware("43.9")
        assert await until(lambda: scripted.hub not in (None, hub))
        await hass.async_block_till_done()
        assert scripted.entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("interval", [0, 5])
@pytest.mark.parametrize("pause", [0.0, 0.2])
@pytest.mark.parametrize("last_chunk", [False, True], ids=["first", "last"])
@pytest.mark.parametrize("opener", TIMINGS)
async def test_a_firmware_install_reloads_once_it_has_reconnected(  # noqa: PLR0913, PLR0917
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    interval: int,
    pause: float,
    last_chunk: bool,  # noqa: FBT001 -- a parameter of the case
    opener: str,
) -> None:
    """
    The new version arrives in the replay after the reboot, with a pause.

    The reload started from that push closed the connection the install was
    waiting on, and the install was reported as failed though it had worked.
    """
    caplog.set_level(logging.INFO)
    charger = ScriptedCharger()
    charger.status_pause = pause
    charger.firmware_in_last_chunk = last_chunk
    async with ScriptedEntry(hass, charger, interval, **TIMINGS[opener]) as scripted:
        hub = scripted.hub
        await hub.async_install_firmware("43.9")
        assert hub.charger.connected, "closed before the install had ended"
        if interval:
            hub._flush_pending()  # the timer this test does not wait for
        assert await until(lambda: scripted.hub not in (None, hub))
        await hass.async_block_till_done()
        assert scripted.entry.state is ConfigEntryState.LOADED
        assert scripted.hub.firmware == "43.9"
    reloads = [
        message
        for message in caplog.messages
        if message == "Charger firmware changed to 43.9, reloading"
    ]
    assert len(reloads) == 1, reloads


async def test_a_version_reported_before_the_reboot_does_not_fail_the_install(
    hass: HomeAssistant,
) -> None:
    """
    The status is complete then, so only the install's end can hold the reload.

    Started at once, it closed the connection and the install was reported
    as failed.
    """
    charger = ScriptedCharger()
    charger.firmware_before_reboot = True
    async with ScriptedEntry(hass, charger) as scripted:
        hub = scripted.hub
        await hub.async_install_firmware("43.9")
        assert await until(lambda: scripted.hub not in (None, hub))
        await hass.async_block_till_done()
        assert scripted.entry.state is ConfigEntryState.LOADED


async def test_a_status_ended_early_by_a_delta_does_not_reload_in_a_loop(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A setup that found no version has none to compare the first report with.

    Taken for a change, it reloaded into the same early end, again and again.
    """
    caplog.set_level(logging.INFO)
    charger = ScriptedCharger()
    charger.leading_delta = True
    charger.firmware_in_last_chunk = True
    charger.status_pause = 0.05
    async with ScriptedEntry(hass, charger) as scripted:
        assert await until(
            lambda: scripted.hub and scripted.hub.firmware == charger.firmware
        )
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
    assert not [message for message in caplog.messages if "reloading" in message]


async def test_a_new_version_just_before_an_outage_reloads_after_it(
    hass: HomeAssistant,
) -> None:
    """Reloading into the outage would swap the reconnect for a failing setup."""
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger) as scripted:
        hub = scripted.hub
        charger.offline = True
        charger.firmware = "43.9"
        charger.sockets[-1].put({"type": "deltaStatus", "status": {"fwv": "43.9"}})
        charger.sockets[-1].end()
        await asyncio.sleep(0.1)
        assert scripted.hub is hub, "the charger is away"

        charger.offline = False
        assert await until(lambda: scripted.hub not in (None, hub))
        await hass.async_block_till_done()
        assert scripted.entry.state is ConfigEntryState.LOADED
        assert scripted.hub.firmware == "43.9"


@pytest.mark.parametrize("unusable", ["", " ", 43, None])
async def test_a_version_that_is_no_text_is_no_version(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, unusable: object
) -> None:
    """Taken for one, each connection compared it with the real one and reloaded."""
    caplog.set_level(logging.INFO)
    charger = ScriptedCharger()
    version = charger.firmware
    charger.firmware = unusable
    async with ScriptedEntry(hass, charger) as scripted:
        assert scripted.hub.firmware is None
        charger.sockets[-1].put({"type": "deltaStatus", "status": {"fwv": version}})
        assert await until(lambda: scripted.hub.firmware == version)
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
    assert not [message for message in caplog.messages if "reloading" in message]


@pytest.mark.parametrize("unusable", ["", " ", 43])
async def test_an_unusable_version_on_a_running_connection_changes_nothing(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, unusable: object
) -> None:
    """The log said "changed to , reloading" and took every entity away for it."""
    caplog.set_level(logging.INFO)
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger) as scripted:
        charger.sockets[-1].put({"type": "deltaStatus", "status": {"fwv": unusable}})
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert scripted.hub.firmware == charger.firmware
        update = hass.states.get("update.wattpilot_firmware")
        assert update.attributes["installed_version"] == charger.firmware
    assert not [message for message in caplog.messages if "reloading" in message]
