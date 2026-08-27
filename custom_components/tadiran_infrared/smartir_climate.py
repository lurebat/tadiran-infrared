"""Generic SmartIR lookup-table climate entity."""

import asyncio
from typing import Any, override

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.infrared import InfraredEmitterConsumerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import TadiranInfraredEntity
from .smartir import SmartIrClimateProfile

_LAST_ACTIVE_HVAC_MODE = "last_active_hvac_mode"
_COMMAND_DELAY = 0.5


class SmartIrClimateEntity(
    TadiranInfraredEntity,
    InfraredEmitterConsumerEntity,
    ClimateEntity,
    RestoreEntity,
):
    """Climate entity driven by a generic SmartIR command table."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(
        self,
        entry: ConfigEntry,
        emitter_entity_id: str,
        profile: SmartIrClimateProfile,
    ) -> None:
        """Initialize a generic SmartIR climate entity."""
        models = ", ".join(profile.supported_models[:3])
        super().__init__(
            entry,
            unique_id_suffix="climate",
            device_name=f"{profile.manufacturer} infrared AC",
            manufacturer=profile.manufacturer,
            model=models,
        )
        self._infrared_emitter_entity_id = emitter_entity_id
        self._profile = profile
        self._attr_hvac_modes = [
            HVACMode.OFF,
            *(HVACMode(mode) for mode in profile.operation_modes),
        ]
        self._attr_fan_modes = list(profile.fan_modes)
        self._attr_swing_modes = (
            list(profile.swing_modes) if profile.swing_modes else None
        )
        self._attr_min_temp = profile.min_temperature
        self._attr_max_temp = profile.max_temperature
        self._attr_target_temperature_step = profile.precision
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if profile.swing_modes:
            self._attr_supported_features |= ClimateEntityFeature.SWING_MODE

        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = profile.min_temperature
        self._attr_fan_mode = profile.fan_modes[0]
        self._attr_swing_mode = profile.swing_modes[0] if profile.swing_modes else None
        self._last_active_hvac_mode = self._attr_hvac_modes[1]
        self._command_lock = asyncio.Lock()
        self._attr_extra_state_attributes = {
            _LAST_ACTIVE_HVAC_MODE: self._last_active_hvac_mode
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the last assumed climate state."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        if last_state.state in self._attr_hvac_modes:
            self._attr_hvac_mode = HVACMode(last_state.state)
        if last_state.attributes.get("fan_mode") in self._profile.fan_modes:
            self._attr_fan_mode = last_state.attributes["fan_mode"]
        if last_state.attributes.get("swing_mode") in self._profile.swing_modes:
            self._attr_swing_mode = last_state.attributes["swing_mode"]
        if (temperature := last_state.attributes.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = self._quantize_temperature(
                float(temperature)
            )
        last_active = last_state.attributes.get(_LAST_ACTIVE_HVAC_MODE)
        if last_active in self._attr_hvac_modes and last_active != HVACMode.OFF:
            self._last_active_hvac_mode = HVACMode(last_active)
        elif self._attr_hvac_mode is not HVACMode.OFF:
            self._last_active_hvac_mode = self._attr_hvac_mode
        self._attr_extra_state_attributes[_LAST_ACTIVE_HVAC_MODE] = (
            self._last_active_hvac_mode
        )

    def _quantize_temperature(self, temperature: float) -> float:
        """Round a temperature to an exact key supported by the profile."""
        steps = round(
            (temperature - self._profile.min_temperature) / self._profile.precision
        )
        value = self._profile.min_temperature + steps * self._profile.precision
        return min(
            self._profile.max_temperature,
            max(self._profile.min_temperature, value),
        )

    async def _async_send_state(
        self,
        mode: HVACMode,
        temperature: float,
        fan_mode: str,
        swing_mode: str | None,
        *,
        send_discrete_on: bool,
    ) -> None:
        """Send an off command or a complete SmartIR state."""
        if mode is HVACMode.OFF:
            await self._send_command(self._profile.off_command())
            return

        state_command = self._profile.state_command(
            operation_mode=mode.value,
            fan_mode=fan_mode,
            swing_mode=swing_mode,
            temperature=temperature,
        )
        if send_discrete_on and (on_command := self._profile.on_command()):
            await self._send_command(on_command)
            await asyncio.sleep(_COMMAND_DELAY)
            try:
                await self._send_command(state_command)
            except HomeAssistantError:
                try:
                    await self._send_command(self._profile.off_command())
                except HomeAssistantError:
                    self._attr_hvac_mode = None
                    self.async_write_ha_state()
                raise
        else:
            await self._send_command(state_command)

    def _commit_state(
        self,
        mode: HVACMode,
        temperature: float,
        fan_mode: str,
        swing_mode: str | None,
    ) -> None:
        self._attr_hvac_mode = mode
        self._attr_target_temperature = temperature
        self._attr_fan_mode = fan_mode
        self._attr_swing_mode = swing_mode
        if mode is not HVACMode.OFF:
            self._last_active_hvac_mode = mode
            self._attr_extra_state_attributes[_LAST_ACTIVE_HVAC_MODE] = mode
        self.async_write_ha_state()

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode atomically."""
        async with self._command_lock:
            await self._async_set_hvac_mode_locked(hvac_mode)

    async def _async_set_hvac_mode_locked(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode while the caller holds the transaction lock."""
        old_mode = self._attr_hvac_mode or HVACMode.OFF
        temperature = self._attr_target_temperature or self._profile.min_temperature
        fan_mode = self._attr_fan_mode or self._profile.fan_modes[0]
        await self._async_send_state(
            hvac_mode,
            temperature,
            fan_mode,
            self._attr_swing_mode,
            send_discrete_on=old_mode is HVACMode.OFF,
        )
        self._commit_state(
            hvac_mode,
            temperature,
            fan_mode,
            self._attr_swing_mode,
        )

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature and optionally HVAC mode atomically."""
        temperature = self._quantize_temperature(float(kwargs[ATTR_TEMPERATURE]))
        requested_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        if requested_mode is not None:
            self._valid_mode_or_raise("hvac", requested_mode, self.hvac_modes)
        async with self._command_lock:
            old_mode = self._attr_hvac_mode or HVACMode.OFF
            mode = requested_mode or old_mode
            fan_mode = self._attr_fan_mode or self._profile.fan_modes[0]
            if mode is not HVACMode.OFF or requested_mode is HVACMode.OFF:
                await self._async_send_state(
                    mode,
                    temperature,
                    fan_mode,
                    self._attr_swing_mode,
                    send_discrete_on=old_mode is HVACMode.OFF,
                )
            self._commit_state(mode, temperature, fan_mode, self._attr_swing_mode)

    @override
    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode atomically."""
        async with self._command_lock:
            mode = self._attr_hvac_mode or HVACMode.OFF
            temperature = self._attr_target_temperature or self._profile.min_temperature
            if mode is not HVACMode.OFF:
                await self._async_send_state(
                    mode,
                    temperature,
                    fan_mode,
                    self._attr_swing_mode,
                    send_discrete_on=False,
                )
            self._commit_state(mode, temperature, fan_mode, self._attr_swing_mode)

    @override
    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set swing mode atomically."""
        async with self._command_lock:
            mode = self._attr_hvac_mode or HVACMode.OFF
            temperature = self._attr_target_temperature or self._profile.min_temperature
            fan_mode = self._attr_fan_mode or self._profile.fan_modes[0]
            if mode is not HVACMode.OFF:
                await self._async_send_state(
                    mode,
                    temperature,
                    fan_mode,
                    swing_mode,
                    send_discrete_on=False,
                )
            self._commit_state(mode, temperature, fan_mode, swing_mode)

    @override
    async def async_turn_on(self) -> None:
        """Turn on using the last active HVAC mode."""
        async with self._command_lock:
            await self._async_set_hvac_mode_locked(self._last_active_hvac_mode)

    @override
    async def async_turn_off(self) -> None:
        """Turn off."""
        await self.async_set_hvac_mode(HVACMode.OFF)
