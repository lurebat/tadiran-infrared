"""Tests for the Tadiran YB1FA protocol."""

import base64
import importlib.util
import sys
from itertools import product
from pathlib import Path

import pytest

PROTOCOL_PATH = (
    Path(__file__).parents[1] / "custom_components" / "tadiran_infrared" / "protocol.py"
)
SPEC = importlib.util.spec_from_file_location("tadiran_protocol", PROTOCOL_PATH)
assert SPEC is not None and SPEC.loader is not None
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)

MAX_TEMP = protocol.MAX_TEMP
MIN_TEMP = protocol.MIN_TEMP
TadiranAcCommand = protocol.TadiranAcCommand
TadiranFanSpeed = protocol.TadiranFanSpeed
TadiranMode = protocol.TadiranMode
TadiranSwingMode = protocol.TadiranSwingMode


def _decode_broadlink_capture(value: str) -> list[int]:
    """Convert a Broadlink Base64 packet into signed microsecond timings."""
    packet = base64.b64decode(value)
    payload_length = int.from_bytes(packet[2:4], "little")
    payload = packet[4 : 4 + payload_length]
    timings: list[int] = []
    index = 0
    while index < len(payload):
        ticks = payload[index]
        index += 1
        if ticks == 0:
            ticks = int.from_bytes(payload[index : index + 2], "big")
            index += 2
        duration = round(ticks * 8192 / 269)
        timings.append(duration if len(timings) % 2 == 0 else -duration)
    return timings


def test_real_smartir_broadlink_capture_decodes() -> None:
    """Decode an independently captured cool/low/top/20 °C frame."""
    capture = (
        "JgCSAAABJJAXNBcRFxAXNBc1FhEXEBcRFhEXERY1FhEXERYRFxAXERYRFxAX"
        "ERYRFxEWNRY1FxAXERYRFxAXERc0FxAXNRYRFxAXNRYRFwACjBcRFzQXEB"
        "cRFhEXERYRFxAXERYRFxAXERYRFzQXERYRFxEWERcQFxEXEBcQFxEWERcQF"
        "xEXEBcRFjUWERcRFjUWAA0F"
    )

    command = TadiranAcCommand.from_raw_timings(_decode_broadlink_capture(capture))

    assert command is not None
    assert command.mode is TadiranMode.COOL
    assert command.temperature == 20
    assert command.fan is TadiranFanSpeed.LOW
    assert command.swing is TadiranSwingMode.TOP
    assert command.turbo is False


@pytest.mark.parametrize(
    ("command", "expected_state"),
    [
        (
            TadiranAcCommand(mode=TadiranMode.OFF),
            "0109e05000200060",
        ),
        (
            TadiranAcCommand(
                mode=TadiranMode.AUTO,
                temperature=16,
                fan=TadiranFanSpeed.AUTO,
                swing=TadiranSwingMode.STATIC,
            ),
            "08096050002000d0",
        ),
        (
            TadiranAcCommand(
                mode=TadiranMode.COOL,
                temperature=20,
                fan=TadiranFanSpeed.LOW,
                swing=TadiranSwingMode.TOP,
            ),
            "1904605002200090",
        ),
        (
            TadiranAcCommand(
                mode=TadiranMode.HEAT,
                temperature=24,
                fan=TadiranFanSpeed.MEDIUM,
                swing=TadiranSwingMode.SWING,
            ),
            "6c08605001200000",
        ),
        (
            TadiranAcCommand(
                mode=TadiranMode.COOL,
                temperature=30,
                fan=TadiranFanSpeed.HIGH,
                swing=TadiranSwingMode.BOTTOM,
                turbo=True,
            ),
            "390e705006200030",
        ),
    ],
)
def test_known_smartir_vectors(command: TadiranAcCommand, expected_state: str) -> None:
    """Generated timings contain the exact state captured by SmartIR 1344."""
    decoded = TadiranAcCommand.from_raw_timings(command.get_raw_timings())

    assert decoded is not None
    assert command.get_state().hex() == expected_state
    assert decoded.mode is command.mode
    assert decoded.temperature == command.temperature
    assert decoded.fan is command.fan
    assert decoded.swing is command.swing
    assert decoded.turbo is command.turbo


def test_every_smartir_combination_round_trips() -> None:
    """Every non-empty SmartIR 1344 state combination round-trips."""
    modes = [
        TadiranMode.AUTO,
        TadiranMode.COOL,
        TadiranMode.DRY,
        TadiranMode.FAN_ONLY,
        TadiranMode.HEAT,
    ]
    count = 0
    for mode, fan, swing, temperature in product(
        modes,
        TadiranFanSpeed,
        TadiranSwingMode,
        range(MIN_TEMP, MAX_TEMP + 1),
    ):
        command = TadiranAcCommand(
            mode=mode,
            temperature=temperature,
            fan=fan,
            swing=swing,
        )
        decoded = TadiranAcCommand.from_raw_timings(command.get_raw_timings())
        assert decoded is not None
        assert decoded.get_state() == command.get_state()
        count += 1

    for mode, swing, temperature in product(
        (TadiranMode.COOL, TadiranMode.HEAT),
        TadiranSwingMode,
        range(MIN_TEMP, MAX_TEMP + 1),
    ):
        command = TadiranAcCommand(
            mode=mode,
            temperature=temperature,
            fan=TadiranFanSpeed.HIGH,
            swing=swing,
            turbo=True,
        )
        decoded = TadiranAcCommand.from_raw_timings(command.get_raw_timings())
        assert decoded is not None
        assert decoded.get_state() == command.get_state()
        count += 1

    # SmartIR has 1 off + 1,320 on-state entries; 60 unsupported turbo
    # entries are empty. Auto temperatures collapse to the fixed 25 °C state.
    assert count == 1320


@pytest.mark.parametrize("temperature", [MIN_TEMP - 1, MAX_TEMP + 1])
def test_temperature_range_is_validated(temperature: int) -> None:
    """Reject temperatures outside the remote's range."""
    with pytest.raises(ValueError, match="out of range"):
        TadiranAcCommand(mode=TadiranMode.COOL, temperature=temperature)


def test_turbo_is_limited_to_cool_and_heat() -> None:
    """Reject turbo in modes for which SmartIR has no command."""
    with pytest.raises(ValueError, match="only available"):
        TadiranAcCommand(
            mode=TadiranMode.DRY,
            temperature=24,
            turbo=True,
        )


def test_invalid_checksum_is_rejected() -> None:
    """Reject a frame whose checksum no longer matches its state."""
    command = TadiranAcCommand(mode=TadiranMode.COOL, temperature=24)
    timings = command.get_raw_timings()
    timings[123] = -1600

    assert TadiranAcCommand.from_raw_timings(timings) is None
