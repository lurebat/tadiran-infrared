"""Climate platform for Tadiran Infrared."""

from typing import Any, override

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_SWING_MODE,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.infrared import (
    InfraredEmitterConsumerEntity,
    InfraredReceivedSignal,
    InfraredReceiverConsumerEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_INFRARED_ENTITY_ID,
    CONF_INFRARED_RECEIVER_ENTITY_ID,
    CONF_PROFILE_DATA,
)
from .entity import TadiranInfraredEntity
from .protocol import (
    MAX_TEMP,
    MIN_TEMP,
    TadiranAcCommand,
    TadiranFanSpeed,
    TadiranMode,
    TadiranSwingMode,
)
from .smartir import SmartIrClimateProfile
from .smartir_climate import SmartIrClimateEntity

PARALLEL_UPDATES = 1

PRESET_TURBO = "turbo"
SWING_STATIC = "static"
SWING_TOP = "top"
SWING_BOTTOM = "bottom"
SWING_AUTO = "swing"
_LAST_ACTIVE_HVAC_MODE = "last_active_hvac_mode"

_HA_MODE_TO_PROTOCOL = {
    HVACMode.AUTO: TadiranMode.AUTO,
    HVACMode.COOL: TadiranMode.COOL,
    HVACMode.DRY: TadiranMode.DRY,
    HVACMode.FAN_ONLY: TadiranMode.FAN_ONLY,
    HVACMode.HEAT: TadiranMode.HEAT,
    HVACMode.OFF: TadiranMode.OFF,
}
_PROTOCOL_MODE_TO_HA = {value: key for key, value in _HA_MODE_TO_PROTOCOL.items()}

_HA_FAN_TO_PROTOCOL = {
    FAN_AUTO: TadiranFanSpeed.AUTO,
    FAN_LOW: TadiranFanSpeed.LOW,
    FAN_MEDIUM: TadiranFanSpeed.MEDIUM,
    FAN_HIGH: TadiranFanSpeed.HIGH,
}
_PROTOCOL_FAN_TO_HA = {value: key for key, value in _HA_FAN_TO_PROTOCOL.items()}

_HA_SWING_TO_PROTOCOL = {
    SWING_STATIC: TadiranSwingMode.STATIC,
    SWING_AUTO: TadiranSwingMode.SWING,
    SWING_TOP: TadiranSwingMode.TOP,
    SWING_BOTTOM: TadiranSwingMode.BOTTOM,
}
_PROTOCOL_SWING_TO_HA = {value: key for key, value in _HA_SWING_TO_PROTOCOL.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Tadiran climate entity."""
    emitter_id = entry.data[CONF_INFRARED_ENTITY_ID]
    if profile_data := entry.data.get(CONF_PROFILE_DATA):
        async_add_entities(
            [
                SmartIrClimateEntity(
                    entry,
                    emitter_id,
                    SmartIrClimateProfile.from_dict(profile_data),
                )
            ]
        )
        return
    if receiver_id := entry.data.get(CONF_INFRARED_RECEIVER_ENTITY_ID):
        async_add_entities([TadiranClimateWithReceiver(entry, emitter_id, receiver_id)])
    else:
        async_add_entities([TadiranClimateEntity(entry, emitter_id)])


class TadiranClimateEntity(
    TadiranInfraredEntity,
    InfraredEmitterConsumerEntity,
    ClimateEntity,
    RestoreEntity,
):
    """Tadiran inverter AC controlled through an infrared emitter."""

    _attr_name = None
    _attr_translation_key = "tadiran_ac"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_min_temp = float(MIN_TEMP)
    _attr_max_temp = float(MAX_TEMP)
    _attr_should_poll = False
    _attr_assumed_state = True
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, entry: ConfigEntry, emitter_entity_id: str) -> None:
        """Initialize the climate entity."""
        super().__init__(entry, unique_id_suffix="climate")
        self._infrared_emitter_entity_id = emitter_entity_id
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.AUTO,
            HVACMode.COOL,
            HVACMode.DRY,
            HVACMode.FAN_ONLY,
            HVACMode.HEAT,
        ]
        self._attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
        self._attr_swing_modes = [
            SWING_STATIC,
            SWING_AUTO,
            SWING_TOP,
            SWING_BOTTOM,
        ]
        self._attr_preset_modes = [PRESET_NONE, PRESET_TURBO]
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = 24.0
        self._attr_fan_mode = FAN_AUTO
        self._attr_swing_mode = SWING_STATIC
        self._attr_preset_mode = PRESET_NONE
        self._last_active_hvac_mode = HVACMode.COOL
        self._attr_extra_state_attributes = {
            _LAST_ACTIVE_HVAC_MODE: self._last_active_hvac_mode
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the last assumed state."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        if last_state.state in self._attr_hvac_modes:
            self._attr_hvac_mode = HVACMode(last_state.state)
            if self._attr_hvac_mode is not HVACMode.OFF:
                self._last_active_hvac_mode = self._attr_hvac_mode
        if (
            last_active_mode := last_state.attributes.get(_LAST_ACTIVE_HVAC_MODE)
        ) in self._attr_hvac_modes and last_active_mode != HVACMode.OFF:
            self._last_active_hvac_mode = HVACMode(last_active_mode)
        self._attr_extra_state_attributes[_LAST_ACTIVE_HVAC_MODE] = (
            self._last_active_hvac_mode
        )
        if (fan := last_state.attributes.get(ATTR_FAN_MODE)) in self._attr_fan_modes:
            self._attr_fan_mode = fan
        if (swing := last_state.attributes.get(ATTR_SWING_MODE)) in (
            self._attr_swing_modes
        ):
            self._attr_swing_mode = swing
        if (preset := last_state.attributes.get(ATTR_PRESET_MODE)) in (
            self._attr_preset_modes
        ):
            self._attr_preset_mode = preset
        if (temperature := last_state.attributes.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = float(temperature)

    def _build_command(
        self,
        mode: HVACMode,
        temperature: float,
        fan_mode: str,
        swing_mode: str,
        preset_mode: str,
    ) -> TadiranAcCommand:
        """Build a command from a proposed climate state."""
        protocol_mode = _HA_MODE_TO_PROTOCOL[mode]
        if protocol_mode is TadiranMode.OFF:
            return TadiranAcCommand(mode=protocol_mode)

        return TadiranAcCommand(
            mode=protocol_mode,
            temperature=int(temperature),
            fan=_HA_FAN_TO_PROTOCOL[fan_mode],
            swing=_HA_SWING_TO_PROTOCOL[swing_mode],
            turbo=preset_mode == PRESET_TURBO,
        )

    @staticmethod
    def _normalize_state(
        mode: HVACMode,
        fan_mode: str,
        swing_mode: str,
        preset_mode: str,
    ) -> tuple[str, str, str]:
        """Normalize controls that the selected mode cannot represent."""
        if mode is HVACMode.DRY:
            fan_mode = FAN_LOW
        if mode in (HVACMode.DRY, HVACMode.FAN_ONLY) and swing_mode == SWING_TOP:
            swing_mode = SWING_STATIC
        if mode not in (HVACMode.COOL, HVACMode.HEAT):
            preset_mode = PRESET_NONE
        return fan_mode, swing_mode, preset_mode

    async def _send_state(
        self,
        mode: HVACMode,
        temperature: float,
        fan_mode: str,
        swing_mode: str,
        preset_mode: str,
    ) -> None:
        await self._send_command(
            self._build_command(
                mode,
                temperature,
                fan_mode,
                swing_mode,
                preset_mode,
            )
        )

    def _commit_state(
        self,
        mode: HVACMode,
        temperature: float,
        fan_mode: str,
        swing_mode: str,
        preset_mode: str,
    ) -> None:
        """Commit a successfully transmitted state."""
        if mode is HVACMode.AUTO:
            temperature = 25.0
        if preset_mode == PRESET_TURBO:
            fan_mode = FAN_HIGH
        self._attr_hvac_mode = mode
        self._attr_target_temperature = temperature
        self._attr_fan_mode = fan_mode
        self._attr_swing_mode = swing_mode
        self._attr_preset_mode = preset_mode
        if mode is not HVACMode.OFF:
            self._last_active_hvac_mode = mode
            self._attr_extra_state_attributes[_LAST_ACTIVE_HVAC_MODE] = mode
        self.async_write_ha_state()

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        fan_mode, swing_mode, preset_mode = self._normalize_state(
            hvac_mode,
            self._attr_fan_mode or FAN_AUTO,
            self._attr_swing_mode or SWING_STATIC,
            self._attr_preset_mode or PRESET_NONE,
        )
        temperature = self._attr_target_temperature or float(MIN_TEMP)
        await self._send_state(
            hvac_mode, temperature, fan_mode, swing_mode, preset_mode
        )
        self._commit_state(hvac_mode, temperature, fan_mode, swing_mode, preset_mode)

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature and optionally the HVAC mode."""
        temperature = float(int(float(kwargs[ATTR_TEMPERATURE]) + 0.5))
        requested_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        if requested_mode is not None:
            self._valid_mode_or_raise("hvac", requested_mode, self.hvac_modes)
        mode = requested_mode or self._attr_hvac_mode or HVACMode.OFF
        fan_mode, swing_mode, preset_mode = self._normalize_state(
            mode,
            self._attr_fan_mode or FAN_AUTO,
            self._attr_swing_mode or SWING_STATIC,
            self._attr_preset_mode or PRESET_NONE,
        )
        if mode is not HVACMode.OFF or requested_mode is HVACMode.OFF:
            await self._send_state(mode, temperature, fan_mode, swing_mode, preset_mode)
        self._commit_state(mode, temperature, fan_mode, swing_mode, preset_mode)

    @override
    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode, disabling turbo when selecting a normal fan speed."""
        if self._attr_hvac_mode is HVACMode.DRY and fan_mode != FAN_LOW:
            raise ServiceValidationError("Dry mode only supports low fan speed")
        mode = self._attr_hvac_mode or HVACMode.OFF
        temperature = self._attr_target_temperature or float(MIN_TEMP)
        if self._attr_hvac_mode is not HVACMode.OFF:
            await self._send_state(
                mode,
                temperature,
                fan_mode,
                self._attr_swing_mode or SWING_STATIC,
                PRESET_NONE,
            )
        self._commit_state(
            mode,
            temperature,
            fan_mode,
            self._attr_swing_mode or SWING_STATIC,
            PRESET_NONE,
        )

    @override
    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set vertical louver mode."""
        if swing_mode == SWING_TOP and self._attr_hvac_mode in (
            HVACMode.DRY,
            HVACMode.FAN_ONLY,
        ):
            raise ServiceValidationError(
                "The top louver position is unavailable in dry and fan-only modes"
            )
        mode = self._attr_hvac_mode or HVACMode.OFF
        temperature = self._attr_target_temperature or float(MIN_TEMP)
        if self._attr_hvac_mode is not HVACMode.OFF:
            await self._send_state(
                mode,
                temperature,
                self._attr_fan_mode or FAN_AUTO,
                swing_mode,
                self._attr_preset_mode or PRESET_NONE,
            )
        self._commit_state(
            mode,
            temperature,
            self._attr_fan_mode or FAN_AUTO,
            swing_mode,
            self._attr_preset_mode or PRESET_NONE,
        )

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set or clear turbo mode."""
        if preset_mode == PRESET_TURBO and self._attr_hvac_mode not in (
            HVACMode.COOL,
            HVACMode.HEAT,
        ):
            raise ServiceValidationError(
                "Turbo mode is only available while cooling or heating"
            )
        mode = self._attr_hvac_mode or HVACMode.OFF
        temperature = self._attr_target_temperature or float(MIN_TEMP)
        if self._attr_hvac_mode is not HVACMode.OFF:
            await self._send_state(
                mode,
                temperature,
                self._attr_fan_mode or FAN_AUTO,
                self._attr_swing_mode or SWING_STATIC,
                preset_mode,
            )
        self._commit_state(
            mode,
            temperature,
            self._attr_fan_mode or FAN_AUTO,
            self._attr_swing_mode or SWING_STATIC,
            preset_mode,
        )

    @override
    async def async_turn_on(self) -> None:
        """Turn on using the last active mode, defaulting to cool."""
        await self.async_set_hvac_mode(self._last_active_hvac_mode)

    @override
    async def async_turn_off(self) -> None:
        """Turn the AC off."""
        await self.async_set_hvac_mode(HVACMode.OFF)


class TadiranClimateWithReceiver(TadiranClimateEntity, InfraredReceiverConsumerEntity):
    """Tadiran climate entity that follows a configured IR receiver."""

    def __init__(
        self, entry: ConfigEntry, emitter_entity_id: str, receiver_entity_id: str
    ) -> None:
        """Initialize the receiver-enabled climate entity."""
        super().__init__(entry, emitter_entity_id)
        self._infrared_receiver_entity_id = receiver_entity_id

    @override
    @callback
    def _handle_signal(self, signal: InfraredReceivedSignal) -> None:
        """Update state from a received YB1FA frame."""
        command = TadiranAcCommand.from_raw_timings(signal.timings)
        if command is None:
            return

        self._attr_hvac_mode = _PROTOCOL_MODE_TO_HA[command.mode]
        if self._attr_hvac_mode is not HVACMode.OFF:
            self._last_active_hvac_mode = self._attr_hvac_mode
            self._attr_extra_state_attributes[_LAST_ACTIVE_HVAC_MODE] = (
                self._last_active_hvac_mode
            )
        if command.fan is not None:
            self._attr_fan_mode = _PROTOCOL_FAN_TO_HA[command.fan]
        if command.swing is not None:
            self._attr_swing_mode = _PROTOCOL_SWING_TO_HA[command.swing]
        if command.temperature is not None:
            self._attr_target_temperature = float(command.temperature)
        self._attr_preset_mode = PRESET_TURBO if command.turbo else PRESET_NONE
        self.async_write_ha_state()
