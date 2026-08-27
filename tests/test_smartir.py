"""Tests for generic SmartIR profile support."""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "tadiran_infrared" / "smartir.py"
)
SPEC = importlib.util.spec_from_file_location("smartir_profile", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
smartir = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smartir
SPEC.loader.exec_module(smartir)

BroadlinkBase64Command = smartir.BroadlinkBase64Command
SmartIrClimateProfile = smartir.SmartIrClimateProfile
SmartIrProfileError = smartir.SmartIrProfileError


def _packet(*ticks: int) -> str:
    payload = bytearray()
    for value in ticks:
        if value <= 0xFF:
            payload.append(value)
        else:
            payload.extend((0, *value.to_bytes(2, "big")))
    packet = bytes((0x26, 0, *len(payload).to_bytes(2, "little"))) + payload
    return base64.b64encode(packet).decode()


def _profile(command: str) -> dict[str, object]:
    return {
        "manufacturer": "Example",
        "supportedModels": ["Example AC"],
        "supportedController": "Broadlink",
        "commandsEncoding": "Base64",
        "minTemperature": 16,
        "maxTemperature": 17,
        "precision": 1,
        "operationModes": ["cool"],
        "fanModes": ["auto"],
        "swingModes": ["static"],
        "commands": {
            "off": command,
            "cool": {
                "auto": {
                    "static": {
                        "16": command,
                        "17": command,
                    }
                }
            },
        },
    }


def test_broadlink_command_decodes_raw_timings() -> None:
    """Broadlink ticks become alternating signed microsecond timings."""
    command = BroadlinkBase64Command(_packet(10, 20, 300))

    assert command.modulation == 38000
    assert command.get_raw_timings() == [305, -609, 9136]


def test_real_smartir_capture_decodes() -> None:
    """A real SmartIR 1344 capture decodes without Broadlink hardware."""
    value = (
        "JgCSAAABJJAXNBcRFxAXNBc1FhEXEBcRFhEXERY1FhEXERYRFxAXERYRFxAX"
        "ERYRFxEWNRY1FxAXERYRFxAXERc0FxAXNRYRFxAXNRYRFwACjBcRFzQXEB"
        "cRFhEXERYRFxAXERYRFxAXERYRFzQXERYRFxEWERcQFxEXEBcQFxEWERcQF"
        "xEXEBcRFjUWERcRFjUWAA0F"
    )

    timings = BroadlinkBase64Command(value).get_raw_timings()

    assert len(timings) == 140
    assert 8000 < timings[0] < 10000
    assert -5000 < timings[1] < -4000


def test_profile_resolves_state_and_off_commands() -> None:
    """A validated profile resolves its lookup-table dimensions."""
    command = _packet(10, 20, 30)
    profile = SmartIrClimateProfile.from_json(json.dumps(_profile(command)))

    assert profile.manufacturer == "Example"
    assert profile.off_command().value == command
    assert (
        profile.state_command(
            operation_mode="cool",
            fan_mode="auto",
            swing_mode="static",
            temperature=16,
        ).value
        == command
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"commandsEncoding": "Raw"}, "only Broadlink"),
        ({"supportedController": "MQTT"}, "only Broadlink"),
        ({"operationModes": ["not_a_mode"]}, "unsupported operation"),
        ({"precision": 0}, "precision is invalid"),
    ],
)
def test_profile_rejects_unsupported_metadata(
    mutation: dict[str, object], message: str
) -> None:
    """Reject profiles the generic decoder cannot safely represent."""
    data = _profile(_packet(10, 20, 30))
    data.update(mutation)

    with pytest.raises(SmartIrProfileError, match=message):
        SmartIrClimateProfile.from_dict(data)


def test_profile_rejects_empty_or_missing_command() -> None:
    """Empty SmartIR matrix entries are reported as unsupported states."""
    data = _profile(_packet(10, 20, 30))
    commands = data["commands"]
    assert isinstance(commands, dict)
    commands["cool"]["auto"]["static"]["16"] = ""  # type: ignore[index]
    with pytest.raises(SmartIrProfileError, match="no complete"):
        SmartIrClimateProfile.from_dict(data)


def test_sparse_profile_exposes_largest_complete_matrix() -> None:
    """Sparse dimensions are pruned instead of failing during a service call."""
    command = _packet(10, 20, 30)
    data = _profile(command)
    data["fanModes"] = ["auto", "high"]
    commands = data["commands"]
    assert isinstance(commands, dict)
    commands["cool"]["high"] = {  # type: ignore[index]
        "static": {"16": command, "17": ""}
    }

    profile = SmartIrClimateProfile.from_dict(data)

    assert profile.operation_modes == ("cool",)
    assert profile.fan_modes == ("auto",)
    assert profile.swing_modes == ("static",)


def test_invalid_base64_and_packet_type_are_rejected() -> None:
    """Malformed captures fail during profile validation or lookup."""
    with pytest.raises(SmartIrProfileError, match="valid Base64"):
        BroadlinkBase64Command("not base64")

    packet = base64.b64encode(b"\x00\x00\x01\x00\x01").decode()
    with pytest.raises(SmartIrProfileError, match="not a Broadlink"):
        BroadlinkBase64Command(packet)
