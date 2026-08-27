"""Generic SmartIR climate profile support.

SmartIR Broadlink profiles are lookup tables containing complete AC state
frames. This module converts those captures into hardware-independent raw
timings accepted by Home Assistant's infrared emitter entities.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Self, override

from infrared_protocols.commands import Command

_BROADLINK_IR_PACKET = 0x26
_BROADLINK_TICK_NUMERATOR = 8192
_BROADLINK_TICK_DENOMINATOR = 269
_DEFAULT_MODULATION = 38000
_SUPPORTED_HVAC_MODES = frozenset(
    {"auto", "cool", "dry", "fan_only", "heat", "heat_cool"}
)


class SmartIrProfileError(ValueError):
    """A SmartIR profile is malformed or unsupported."""


class BroadlinkBase64Command(Command):
    """A Broadlink Base64 IR capture exposed as raw timings."""

    def __init__(
        self,
        value: str,
        *,
        modulation: int = _DEFAULT_MODULATION,
    ) -> None:
        """Decode and validate a Broadlink packet."""
        super().__init__(modulation=modulation)
        self.value = value
        self._timings = self._decode(value)

    @staticmethod
    def _decode(value: str) -> tuple[int, ...]:
        if not value:
            raise SmartIrProfileError("the SmartIR command is empty")
        try:
            packet = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as err:
            raise SmartIrProfileError(
                "the SmartIR command is not valid Base64"
            ) from err

        if len(packet) < 5 or packet[0] != _BROADLINK_IR_PACKET:
            raise SmartIrProfileError(
                "the SmartIR command is not a Broadlink IR packet"
            )

        payload_length = int.from_bytes(packet[2:4], "little")
        if payload_length == 0 or payload_length > len(packet) - 4:
            raise SmartIrProfileError(
                "the Broadlink packet has an invalid payload length"
            )

        payload = packet[4 : 4 + payload_length]
        durations: list[int] = []
        index = 0
        while index < len(payload):
            ticks = payload[index]
            index += 1
            if ticks == 0:
                if index + 2 > len(payload):
                    raise SmartIrProfileError(
                        "the Broadlink packet ends inside a duration"
                    )
                ticks = int.from_bytes(payload[index : index + 2], "big")
                index += 2
            if ticks == 0:
                raise SmartIrProfileError(
                    "the Broadlink packet contains a zero duration"
                )

            duration = round(
                ticks * _BROADLINK_TICK_NUMERATOR / _BROADLINK_TICK_DENOMINATOR
            )
            durations.append(duration if len(durations) % 2 == 0 else -duration)

        if len(durations) < 3:
            raise SmartIrProfileError("the Broadlink packet contains too few durations")
        return tuple(durations)

    @override
    def get_raw_timings(self) -> list[int]:
        """Return a copy of the decoded pulse/space timings."""
        return list(self._timings)


@dataclass(frozen=True, slots=True)
class SmartIrClimateProfile:
    """A validated SmartIR climate lookup profile."""

    manufacturer: str
    supported_models: tuple[str, ...]
    min_temperature: float
    max_temperature: float
    precision: float
    operation_modes: tuple[str, ...]
    fan_modes: tuple[str, ...]
    swing_modes: tuple[str, ...]
    commands: Mapping[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Validate and construct a profile from decoded JSON."""
        try:
            manufacturer = data["manufacturer"]
            supported_models = data["supportedModels"]
            controller = data["supportedController"]
            encoding = data["commandsEncoding"]
            min_temperature = data["minTemperature"]
            max_temperature = data["maxTemperature"]
            precision = data["precision"]
            operation_modes = data["operationModes"]
            fan_modes = data["fanModes"]
            commands = data["commands"]
        except KeyError as err:
            raise SmartIrProfileError(
                f"missing required SmartIR field: {err.args[0]}"
            ) from err

        if controller != "Broadlink" or encoding != "Base64":
            raise SmartIrProfileError(
                "only Broadlink profiles with Base64 commands are supported"
            )
        if not isinstance(manufacturer, str) or not manufacturer.strip():
            raise SmartIrProfileError("manufacturer must be a non-empty string")
        if not cls._string_list(supported_models):
            raise SmartIrProfileError("supportedModels must be a non-empty string list")
        if not cls._string_list(operation_modes):
            raise SmartIrProfileError("operationModes must be a non-empty string list")
        if invalid_modes := set(operation_modes) - _SUPPORTED_HVAC_MODES:
            invalid = ", ".join(sorted(invalid_modes))
            raise SmartIrProfileError(f"unsupported operation modes: {invalid}")
        if not cls._string_list(fan_modes):
            raise SmartIrProfileError("fanModes must be a non-empty string list")
        swing_modes = data.get("swingModes", ())
        if swing_modes and not cls._string_list(swing_modes):
            raise SmartIrProfileError("swingModes must be a string list")
        if not isinstance(commands, Mapping) or "off" not in commands:
            raise SmartIrProfileError("commands must contain an off command")

        if not all(
            isinstance(value, (int, float))
            for value in (min_temperature, max_temperature, precision)
        ):
            raise SmartIrProfileError("temperature range and precision must be numeric")
        if (
            min_temperature >= max_temperature
            or precision <= 0
            or (max_temperature - min_temperature) / precision > 1000
        ):
            raise SmartIrProfileError("temperature range or precision is invalid")

        profile = cls(
            manufacturer=manufacturer.strip(),
            supported_models=tuple(supported_models),
            min_temperature=float(min_temperature),
            max_temperature=float(max_temperature),
            precision=float(precision),
            operation_modes=tuple(operation_modes),
            fan_modes=tuple(fan_modes),
            swing_modes=tuple(swing_modes),
            commands=commands,
        )
        profile.off_command()
        profile.on_command()
        return profile._with_largest_complete_matrix()

    @staticmethod
    def _string_list(value: object) -> bool:
        return (
            isinstance(value, list | tuple)
            and bool(value)
            and all(isinstance(item, str) and item for item in value)
        )

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> Self:
        """Construct a profile from JSON text."""
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as err:
            raise SmartIrProfileError("the SmartIR profile is invalid JSON") from err
        if not isinstance(data, Mapping):
            raise SmartIrProfileError("the SmartIR profile root must be an object")
        return cls.from_dict(data)

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        """Load a profile from a UTF-8 JSON file."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def off_command(self) -> BroadlinkBase64Command:
        """Return the profile's power-off command."""
        return self._command_from_value(self.commands["off"], "off")

    def on_command(self) -> BroadlinkBase64Command | None:
        """Return an optional discrete power-on command."""
        value = self.commands.get("on")
        return None if value is None else self._command_from_value(value, "on")

    def state_command(
        self,
        *,
        operation_mode: str,
        fan_mode: str,
        temperature: float,
        swing_mode: str | None = None,
    ) -> BroadlinkBase64Command:
        """Resolve one complete state command from the profile dimensions."""
        if operation_mode not in self.operation_modes:
            raise SmartIrProfileError(f"unsupported operation mode: {operation_mode}")
        if fan_mode not in self.fan_modes:
            raise SmartIrProfileError(f"unsupported fan mode: {fan_mode}")
        if not self.min_temperature <= temperature <= self.max_temperature:
            raise SmartIrProfileError(
                f"temperature {temperature:g} is outside the profile range"
            )

        key = f"{temperature:g}"
        path = [operation_mode, fan_mode]
        if self.swing_modes:
            if swing_mode not in self.swing_modes:
                raise SmartIrProfileError(f"unsupported swing mode: {swing_mode}")
            path.append(swing_mode)
        path.append(key)

        value: object = self.commands
        for part in path:
            if not isinstance(value, Mapping) or part not in value:
                joined = "/".join(path)
                raise SmartIrProfileError(f"the profile has no command for {joined}")
            value = value[part]
        return self._command_from_value(value, "/".join(path))

    def _with_largest_complete_matrix(self) -> Self:
        """Keep the largest rectangular state matrix that is safe to expose.

        Home Assistant advertises global mode, fan, and swing lists, while some
        SmartIR profiles contain mode-specific holes. A maximal complete subset
        prevents the UI from offering combinations that fail after an optional
        discrete power-on command has already been transmitted.
        """
        swing_values: tuple[str | None, ...] = self.swing_modes or (None,)
        complete_dimensions = {
            (mode, fan, swing)
            for mode in self.operation_modes
            for fan in self.fan_modes
            for swing in swing_values
            if self._mode_is_complete(mode, (fan,), (swing,))
        }
        best: (
            tuple[
                tuple[int, int, int, int],
                tuple[str, ...],
                tuple[str, ...],
                tuple[str, ...],
            ]
            | None
        ) = None

        for fan_count in range(1, len(self.fan_modes) + 1):
            for fans in combinations(self.fan_modes, fan_count):
                for swing_count in range(1, len(swing_values) + 1):
                    for swings in combinations(swing_values, swing_count):
                        modes = tuple(
                            mode
                            for mode in self.operation_modes
                            if all(
                                (mode, fan, swing) in complete_dimensions
                                for fan in fans
                                for swing in swings
                            )
                        )
                        if not modes:
                            continue
                        score = (
                            len(modes) * len(fans) * len(swings),
                            len(modes),
                            len(fans),
                            len(swings),
                        )
                        exposed_swings = tuple(
                            swing for swing in swings if swing is not None
                        )
                        candidate = (score, modes, fans, exposed_swings)
                        if best is None or candidate[0] > best[0]:
                            best = candidate

        if best is None:
            raise SmartIrProfileError(
                "the profile contains no complete climate state matrix"
            )
        _, modes, fans, swings = best
        return type(self)(
            manufacturer=self.manufacturer,
            supported_models=self.supported_models,
            min_temperature=self.min_temperature,
            max_temperature=self.max_temperature,
            precision=self.precision,
            operation_modes=modes,
            fan_modes=fans,
            swing_modes=swings,
            commands=self.commands,
        )

    def _mode_is_complete(
        self,
        mode: str,
        fans: tuple[str, ...],
        swings: tuple[str | None, ...],
    ) -> bool:
        """Return whether every state in a mode subset has a valid command."""
        step_count = round(
            (self.max_temperature - self.min_temperature) / self.precision
        )
        for fan in fans:
            for swing in swings:
                for step in range(step_count + 1):
                    temperature = self.min_temperature + step * self.precision
                    try:
                        self.state_command(
                            operation_mode=mode,
                            fan_mode=fan,
                            swing_mode=swing,
                            temperature=temperature,
                        )
                    except SmartIrProfileError:
                        return False
        return True

    @staticmethod
    def _command_from_value(value: object, path: str) -> BroadlinkBase64Command:
        if not isinstance(value, str):
            raise SmartIrProfileError(f"the command at {path} must be a Base64 string")
        try:
            return BroadlinkBase64Command(value)
        except SmartIrProfileError as err:
            raise SmartIrProfileError(f"invalid command at {path}: {err}") from err
