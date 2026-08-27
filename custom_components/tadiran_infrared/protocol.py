"""Tadiran YB1FA infrared protocol encoder and decoder.

The remote uses the 64-bit Gree air-conditioner protocol with YB1FA-specific
fixed bits. Frames and state mappings were derived from SmartIR code set 1344.
"""

from enum import IntEnum
from typing import Self, override

from infrared_protocols.commands import Command

MIN_TEMP = 16
MAX_TEMP = 30

_HDR_MARK = 9000
_HDR_SPACE = 4500
_BIT_MARK = 620
_BIT_ONE_SPACE = 1600
_BIT_ZERO_SPACE = 540
_BLOCK_SPACE = 19980
_MODULATION = 38000

_FOOTER_BITS = (0, 1, 0)
_OFF_STATE = bytes((0x01, 0x09, 0xE0, 0x50, 0x00, 0x20, 0x00, 0x60))
_AUTO_TEMP = 25
_FRAME_LENGTH = 140
_FIXED_BYTE_3 = 0x50
_FIXED_BYTE_5_MASK = 0x38
_FIXED_BYTE_5 = 0x20

_MARK_TOLERANCE = 350
_HEADER_MARK_TOLERANCE = 2500
_HEADER_SPACE_TOLERANCE = 1200
_BLOCK_SPACE_TOLERANCE = 5000


class TadiranMode(IntEnum):
    """Operating mode values in the YB1FA frame."""

    AUTO = 0
    COOL = 1
    DRY = 2
    FAN_ONLY = 3
    HEAT = 4
    OFF = 8


class TadiranFanSpeed(IntEnum):
    """Fan speed values in the YB1FA frame."""

    AUTO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class TadiranSwingMode(IntEnum):
    """Vertical louver values in the YB1FA frame."""

    STATIC = 0
    SWING = 1
    TOP = 2
    BOTTOM = 6


def _checksum(state: bytes | bytearray) -> int:
    """Return the Gree/Kelvinator checksum nibble."""
    return (
        10
        + sum(value & 0x0F for value in state[:4])
        + sum(value >> 4 for value in state[4:7])
    ) & 0x0F


def _append_bit(timings: list[int], bit: int) -> None:
    timings.extend((_BIT_MARK, -(_BIT_ONE_SPACE if bit else _BIT_ZERO_SPACE)))


def _encode_state(state: bytes) -> list[int]:
    """Encode an eight-byte state as canonical Gree raw timings."""
    timings = [_HDR_MARK, -_HDR_SPACE]
    for value in state[:4]:
        for bit in range(8):
            _append_bit(timings, (value >> bit) & 1)
    for bit in _FOOTER_BITS:
        _append_bit(timings, bit)
    timings.extend((_BIT_MARK, -_BLOCK_SPACE))
    for value in state[4:]:
        for bit in range(8):
            _append_bit(timings, (value >> bit) & 1)
    timings.extend((_BIT_MARK, -_BLOCK_SPACE))
    return timings


def _close(actual: int, expected: int, tolerance: int) -> bool:
    return abs(abs(actual) - expected) <= tolerance


def _decode_bit(mark: int, space: int) -> int | None:
    if not _close(mark, _BIT_MARK, _MARK_TOLERANCE):
        return None
    if _close(space, _BIT_ZERO_SPACE, _MARK_TOLERANCE):
        return 0
    if _close(space, _BIT_ONE_SPACE, _MARK_TOLERANCE):
        return 1
    return None


def _decode_byte(timings: list[int], offset: int) -> int | None:
    value = 0
    for bit_index in range(8):
        bit_offset = offset + bit_index * 2
        bit = _decode_bit(timings[bit_offset], timings[bit_offset + 1])
        if bit is None:
            return None
        value |= bit << bit_index
    return value


def _decode_state(timings: list[int]) -> bytes | None:
    """Decode and validate canonical or captured Gree raw timings."""
    if len(timings) < _FRAME_LENGTH:
        return None
    if not _close(timings[0], _HDR_MARK, _HEADER_MARK_TOLERANCE) or not _close(
        timings[1], _HDR_SPACE, _HEADER_SPACE_TOLERANCE
    ):
        return None

    values: list[int] = []
    for offset in (2, 18, 34, 50):
        value = _decode_byte(timings, offset)
        if value is None:
            return None
        values.append(value)

    footer_offset = 66
    for index, expected in enumerate(_FOOTER_BITS):
        if (
            _decode_bit(
                timings[footer_offset + index * 2],
                timings[footer_offset + index * 2 + 1],
            )
            != expected
        ):
            return None
    if not _close(timings[72], _BIT_MARK, _MARK_TOLERANCE) or not _close(
        timings[73], _BLOCK_SPACE, _BLOCK_SPACE_TOLERANCE
    ):
        return None

    for offset in (74, 90, 106, 122):
        value = _decode_byte(timings, offset)
        if value is None:
            return None
        values.append(value)

    if not _close(timings[138], _BIT_MARK, _MARK_TOLERANCE):
        return None

    state = bytes(values)
    if state[7] & 0x0F or state[7] >> 4 != _checksum(state):
        return None
    if state[3] != _FIXED_BYTE_3 or state[5] & _FIXED_BYTE_5_MASK != _FIXED_BYTE_5:
        return None
    return state


class TadiranAcCommand(Command):
    """A complete Tadiran YB1FA air-conditioner state command."""

    mode: TadiranMode
    temperature: int | None
    fan: TadiranFanSpeed | None
    swing: TadiranSwingMode | None
    turbo: bool

    def __init__(
        self,
        *,
        mode: TadiranMode,
        temperature: int | None = None,
        fan: TadiranFanSpeed = TadiranFanSpeed.AUTO,
        swing: TadiranSwingMode = TadiranSwingMode.STATIC,
        turbo: bool = False,
        modulation: int = _MODULATION,
    ) -> None:
        """Initialize a Tadiran AC command."""
        super().__init__(modulation=modulation)

        if mode is TadiranMode.OFF:
            self.mode = mode
            self.temperature = None
            self.fan = None
            self.swing = None
            self.turbo = False
            return

        if temperature is None:
            raise ValueError(f"temperature is required for mode {mode.name}")
        if not MIN_TEMP <= temperature <= MAX_TEMP:
            raise ValueError(
                f"temperature {temperature} out of range {MIN_TEMP}..{MAX_TEMP}"
            )
        if turbo and mode not in (TadiranMode.COOL, TadiranMode.HEAT):
            raise ValueError("turbo is only available in COOL and HEAT modes")

        self.mode = mode
        self.temperature = _AUTO_TEMP if mode is TadiranMode.AUTO else temperature
        if mode is TadiranMode.DRY:
            self.fan = TadiranFanSpeed.LOW
        else:
            self.fan = TadiranFanSpeed.HIGH if turbo else fan
        self.swing = (
            TadiranSwingMode.STATIC
            if mode in (TadiranMode.DRY, TadiranMode.FAN_ONLY)
            and swing is TadiranSwingMode.TOP
            else swing
        )
        self.turbo = turbo

    def get_state(self) -> bytes:
        """Build the eight-byte YB1FA state."""
        if self.mode is TadiranMode.OFF:
            return _OFF_STATE

        assert self.temperature is not None
        assert self.fan is not None
        assert self.swing is not None

        state = bytearray(
            (
                self.mode.value | 0x08 | (self.fan.value << 4),
                self.temperature - MIN_TEMP,
                0x60 | (0x10 if self.turbo else 0),
                0x50,
                self.swing.value,
                0x20,
                0x00,
                0x00,
            )
        )
        if self.swing is TadiranSwingMode.SWING:
            state[0] |= 0x40
        state[7] = _checksum(state) << 4
        return bytes(state)

    @override
    def get_raw_timings(self) -> list[int]:
        """Return raw pulse/space timings for this command."""
        return _encode_state(self.get_state())

    @classmethod
    def from_raw_timings(cls, timings: list[int]) -> Self | None:
        """Decode a received YB1FA frame."""
        state = _decode_state(timings)
        if state is None:
            return None
        if not state[0] & 0x08:
            return cls(mode=TadiranMode.OFF)
        if not state[2] & 0x40:
            return None

        try:
            mode = TadiranMode(state[0] & 0x07)
            fan = TadiranFanSpeed((state[0] >> 4) & 0x03)
            swing = TadiranSwingMode(state[4] & 0x0F)
        except ValueError:
            return None

        swing_auto = bool(state[0] & 0x40)
        if swing_auto != (swing is TadiranSwingMode.SWING):
            return None

        temperature = (state[1] & 0x0F) + MIN_TEMP
        if not MIN_TEMP <= temperature <= MAX_TEMP:
            return None
        if mode is TadiranMode.AUTO and temperature != _AUTO_TEMP:
            return None

        turbo = bool(state[2] & 0x10)
        if turbo and mode not in (TadiranMode.COOL, TadiranMode.HEAT):
            return None
        return cls(
            mode=mode,
            temperature=temperature,
            fan=fan,
            swing=swing,
            turbo=turbo,
        )
