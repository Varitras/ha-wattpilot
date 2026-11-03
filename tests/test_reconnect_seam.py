"""
The seam between the real client and the config entry.

What a stand-in for the client cannot show: a whole sequence of connections.
Each case disturbs one stage, and the charger is healthy afterwards.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

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
}


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


async def test_an_unusable_handshake_during_an_install_does_not_fail_it(
    hass: HomeAssistant,
) -> None:
    """The firmware is on the charger by then: only the connection hiccuped."""
    charger = ScriptedCharger()
    async with ScriptedEntry(hass, charger) as scripted:
        hub = scripted.hub
        charger.next_handshakes.append(UNUSABLE_HANDSHAKES["hello without a serial"])
        await hub.async_install_firmware("43.9")
        assert await until(lambda: scripted.hub not in (None, hub))
        await hass.async_block_till_done()
        assert scripted.entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("interval", [0, 5])
@pytest.mark.parametrize("pause", [0.0, 0.2])
@pytest.mark.parametrize("last_chunk", [False, True], ids=["first", "last"])
async def test_a_firmware_install_reloads_once_it_has_reconnected(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    interval: int,
    pause: float,
    last_chunk: bool,  # noqa: FBT001 -- a parameter of the case
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
    async with ScriptedEntry(hass, charger, interval) as scripted:
        hub = scripted.hub
        await hub.async_install_firmware("43.9")
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
