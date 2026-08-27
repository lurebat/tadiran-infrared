"""Tests for the generic SmartIR climate entity."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.tadiran_infrared.smartir import SmartIrClimateProfile
from custom_components.tadiran_infrared.smartir_climate import SmartIrClimateEntity


def _packet(*ticks: int) -> str:
    payload = bytearray(ticks)
    packet = bytes((0x26, 0, *len(payload).to_bytes(2, "little"))) + payload
    return base64.b64encode(packet).decode()


def _profile(
    *, discrete_on: bool = False, include_heat: bool = False
) -> SmartIrClimateProfile:
    off = _packet(10, 20, 30)
    on = _packet(11, 21, 31)
    state_16 = _packet(12, 22, 32)
    state_17 = _packet(13, 23, 33)
    commands: dict[str, object] = {
        "off": off,
        "cool": {
            "auto": {
                "static": {
                    "16": state_16,
                    "17": state_17,
                },
            }
        },
    }
    operation_modes = ["cool"]
    if include_heat:
        operation_modes.append("heat")
        commands["heat"] = {
            "auto": {
                "static": {
                    "16": _packet(14, 24, 34),
                    "17": _packet(15, 25, 35),
                }
            }
        }
    if discrete_on:
        commands["on"] = on
    return SmartIrClimateProfile.from_dict(
        {
            "manufacturer": "Example",
            "supportedModels": ["Example AC"],
            "supportedController": "Broadlink",
            "commandsEncoding": "Base64",
            "minTemperature": 16,
            "maxTemperature": 17,
            "precision": 1,
            "operationModes": operation_modes,
            "fanModes": ["auto"],
            "swingModes": ["static"],
            "commands": commands,
        }
    )


def _entity(
    *, discrete_on: bool = False, include_heat: bool = False
) -> SmartIrClimateEntity:
    entry = SimpleNamespace(entry_id="test")
    entity = SmartIrClimateEntity(
        entry,  # type: ignore[arg-type]
        "infrared.test_emitter",
        _profile(discrete_on=discrete_on, include_heat=include_heat),
    )
    entity.async_write_ha_state = Mock()
    entity._send_command = AsyncMock()
    return entity


async def test_turn_on_sends_state_and_commits_after_success() -> None:
    """Turning on sends the selected table command and then updates state."""
    entity = _entity()

    await entity.async_set_hvac_mode(HVACMode.COOL)

    entity._send_command.assert_awaited_once()
    sent = entity._send_command.await_args.args[0]
    assert sent.value == _packet(12, 22, 32)
    assert entity.hvac_mode is HVACMode.COOL
    entity.async_write_ha_state.assert_called_once()


async def test_discrete_on_precedes_state_command() -> None:
    """Profiles with a separate on command send it before their state."""
    entity = _entity(discrete_on=True)

    with patch(
        "custom_components.tadiran_infrared.smartir_climate.asyncio.sleep",
        AsyncMock(),
    ) as sleep:
        await entity.async_set_hvac_mode(HVACMode.COOL)

    assert [call.args[0].value for call in entity._send_command.await_args_list] == [
        _packet(11, 21, 31),
        _packet(12, 22, 32),
    ]
    sleep.assert_awaited_once_with(0.5)


async def test_temperature_change_while_off_does_not_transmit() -> None:
    """Changing the stored target while off does not accidentally power on."""
    entity = _entity()

    await entity.async_set_temperature(temperature=17)

    entity._send_command.assert_not_awaited()
    assert entity.target_temperature == 17
    assert entity.hvac_mode is HVACMode.OFF


async def test_transmission_failure_does_not_commit_state() -> None:
    """A failed emitter call leaves the entity's previous state intact."""
    entity = _entity()
    entity._send_command.side_effect = HomeAssistantError("send failed")

    with pytest.raises(HomeAssistantError, match="send failed"):
        await entity.async_set_hvac_mode(HVACMode.COOL)

    assert entity.hvac_mode is HVACMode.OFF
    entity.async_write_ha_state.assert_not_called()


async def test_turn_off_sends_discrete_off_command() -> None:
    """Turning off sends the profile's off capture."""
    entity = _entity()
    entity._attr_hvac_mode = HVACMode.COOL

    await entity.async_turn_off()

    sent = entity._send_command.await_args.args[0]
    assert sent.value == _packet(10, 20, 30)
    assert entity.hvac_mode is HVACMode.OFF


async def test_concurrent_transactions_do_not_interleave() -> None:
    """A second service call waits for a discrete-on transaction to finish."""
    entity = _entity(discrete_on=True)
    delay_started = asyncio.Event()
    release_delay = asyncio.Event()
    real_sleep = asyncio.sleep

    async def controlled_delay(_seconds: float) -> None:
        delay_started.set()
        await release_delay.wait()

    with patch(
        "custom_components.tadiran_infrared.smartir_climate.asyncio.sleep",
        controlled_delay,
    ):
        turn_on = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.COOL))
        await delay_started.wait()
        turn_off = asyncio.create_task(entity.async_turn_off())
        await real_sleep(0)

        assert len(entity._send_command.await_args_list) == 1
        release_delay.set()
        await asyncio.gather(turn_on, turn_off)

    assert [call.args[0].value for call in entity._send_command.await_args_list] == [
        _packet(11, 21, 31),
        _packet(12, 22, 32),
        _packet(10, 20, 30),
    ]
    assert entity.hvac_mode is HVACMode.OFF


async def test_state_lookup_finishes_before_discrete_on() -> None:
    """A corrupted lookup cannot power on the appliance before failing."""
    entity = _entity(discrete_on=True)
    commands = entity._profile.commands
    assert isinstance(commands, dict)
    commands["cool"]["auto"]["static"]["16"] = ""  # type: ignore[index]

    with pytest.raises(ValueError, match="invalid command"):
        await entity.async_set_hvac_mode(HVACMode.COOL)

    entity._send_command.assert_not_awaited()
    assert entity.hvac_mode is HVACMode.OFF


async def test_concurrent_turn_on_reads_last_mode_inside_lock() -> None:
    """Turn-on observes a mode change that commits while it waits for the lock."""
    entity = _entity(include_heat=True)
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    calls = 0

    async def controlled_send(_command: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_send_started.set()
            await release_first_send.wait()

    entity._send_command.side_effect = controlled_send
    set_heat = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
    await first_send_started.wait()
    turn_on = asyncio.create_task(entity.async_turn_on())
    await asyncio.sleep(0)
    release_first_send.set()
    await asyncio.gather(set_heat, turn_on)

    assert entity.hvac_mode is HVACMode.HEAT
    assert entity._last_active_hvac_mode is HVACMode.HEAT


async def test_failed_state_after_discrete_on_is_compensated_with_off() -> None:
    """A failed state packet after power-on sends a compensating off packet."""
    entity = _entity(discrete_on=True)
    entity._send_command.side_effect = [
        None,
        HomeAssistantError("state failed"),
        None,
    ]

    with (
        patch(
            "custom_components.tadiran_infrared.smartir_climate.asyncio.sleep",
            AsyncMock(),
        ),
        pytest.raises(HomeAssistantError, match="state failed"),
    ):
        await entity.async_set_hvac_mode(HVACMode.COOL)

    assert [call.args[0].value for call in entity._send_command.await_args_list] == [
        _packet(11, 21, 31),
        _packet(12, 22, 32),
        _packet(10, 20, 30),
    ]
    assert entity.hvac_mode is HVACMode.OFF


async def test_failed_on_compensation_marks_state_unknown() -> None:
    """If both state and compensating off fail, publish unknown state."""
    entity = _entity(discrete_on=True)
    entity._send_command.side_effect = [
        None,
        HomeAssistantError("state failed"),
        HomeAssistantError("off failed"),
    ]

    with (
        patch(
            "custom_components.tadiran_infrared.smartir_climate.asyncio.sleep",
            AsyncMock(),
        ),
        pytest.raises(HomeAssistantError, match="state failed"),
    ):
        await entity.async_set_hvac_mode(HVACMode.COOL)

    assert entity.hvac_mode is None
    entity.async_write_ha_state.assert_called_once()
