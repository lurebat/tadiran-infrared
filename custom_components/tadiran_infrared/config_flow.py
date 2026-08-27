"""Config flow for Tadiran Infrared."""

import json
from typing import Any, override

import aiohttp
import voluptuous as vol
from homeassistant.components.infrared import DOMAIN as INFRARED_DOMAIN
from homeassistant.components.infrared import (
    async_get_emitters,
    async_get_receivers,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_DEVICE_CODE,
    CONF_INFRARED_ENTITY_ID,
    CONF_INFRARED_RECEIVER_ENTITY_ID,
    CONF_PROFILE_DATA,
    DOMAIN,
    TADIRAN_DEVICE_CODE,
)
from .smartir import SmartIrClimateProfile, SmartIrProfileError

_SMARTIR_PROFILE_URLS = (
    "https://raw.githubusercontent.com/smartHomeHub/SmartIR/master/"
    "codes/climate/{code}.json",
    "https://raw.githubusercontent.com/smartHomeHub/SmartIR/master/"
    "codes/climate/{code}",
)
_MAX_PROFILE_SIZE = 2 * 1024 * 1024
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30)


class TadiranInfraredConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Tadiran Infrared config flow."""

    VERSION = 2

    def _entity_name(self, entity_id: str) -> str:
        """Return the display name of an entity."""
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry.name or entry.original_name or entity_id if entry else entity_id

    async def _async_download_profile(self, device_code: str) -> dict[str, Any] | None:
        """Download and validate a SmartIR climate profile."""
        session = async_get_clientsession(self.hass)
        for template in _SMARTIR_PROFILE_URLS:
            try:
                async with session.get(
                    template.format(code=device_code),
                    timeout=_DOWNLOAD_TIMEOUT,
                ) as response:
                    if response.status == 404:
                        continue
                    response.raise_for_status()
                    if (
                        response.content_length is not None
                        and response.content_length > _MAX_PROFILE_SIZE
                    ):
                        raise SmartIrProfileError("profile exceeds the size limit")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(
                        _DOWNLOAD_CHUNK_SIZE
                    ):
                        if len(raw) + len(chunk) > _MAX_PROFILE_SIZE:
                            raise SmartIrProfileError("profile exceeds the size limit")
                        raw.extend(chunk)
            except (TimeoutError, aiohttp.ClientError) as err:
                raise SmartIrProfileError("unable to download the profile") from err

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as err:
                raise SmartIrProfileError("profile is invalid JSON") from err
            if not isinstance(data, dict):
                raise SmartIrProfileError("profile root must be an object")
            SmartIrClimateProfile.from_dict(data)
            return data
        return None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an infrared emitter and optional receiver."""
        emitters = async_get_emitters(self.hass)
        if not emitters:
            return self.async_abort(reason="no_infrared_emitters")

        errors: dict[str, str] = {}
        if user_input is not None:
            emitter_id = user_input[CONF_INFRARED_ENTITY_ID]
            device_code = user_input[CONF_DEVICE_CODE].strip()
            if not device_code.isdecimal():
                errors[CONF_DEVICE_CODE] = "invalid_device_code"
            elif (
                device_code != TADIRAN_DEVICE_CODE
                and CONF_INFRARED_RECEIVER_ENTITY_ID in user_input
            ):
                errors["base"] = "receiver_unsupported"
            else:
                if any(
                    entry.data.get(CONF_INFRARED_ENTITY_ID) == emitter_id
                    and entry.data.get(CONF_DEVICE_CODE, TADIRAN_DEVICE_CODE)
                    == device_code
                    for entry in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                if device_code == TADIRAN_DEVICE_CODE:
                    user_input[CONF_DEVICE_CODE] = device_code
                    return self.async_create_entry(
                        title=f"Tadiran AC via {self._entity_name(emitter_id)}",
                        data=user_input,
                    )
                else:
                    try:
                        profile_data = await self._async_download_profile(device_code)
                    except SmartIrProfileError:
                        errors["base"] = "profile_invalid"
                    else:
                        if profile_data is None:
                            errors["base"] = "profile_not_found"
                        else:
                            profile = SmartIrClimateProfile.from_dict(profile_data)
                            user_input[CONF_DEVICE_CODE] = device_code
                            user_input[CONF_PROFILE_DATA] = profile_data
                            return self.async_create_entry(
                                title=(
                                    f"{profile.manufacturer} AC via "
                                    f"{self._entity_name(emitter_id)}"
                                ),
                                data=user_input,
                            )

        if user_input is None:
            user_input = {}

        default_code = user_input.get(CONF_DEVICE_CODE, TADIRAN_DEVICE_CODE)
        default_emitter = user_input.get(CONF_INFRARED_ENTITY_ID)
        default_receiver = user_input.get(CONF_INFRARED_RECEIVER_ENTITY_ID)

        emitter_marker = (
            vol.Required(CONF_INFRARED_ENTITY_ID, default=default_emitter)
            if default_emitter is not None
            else vol.Required(CONF_INFRARED_ENTITY_ID)
        )
        receiver_marker = (
            vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID, default=default_receiver)
            if default_receiver is not None
            else vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID)
        )
        schema = {
            vol.Required(CONF_DEVICE_CODE, default=default_code): TextSelector(
                TextSelectorConfig()
            ),
            emitter_marker: EntitySelector(
                EntitySelectorConfig(
                    domain=INFRARED_DOMAIN,
                    include_entities=emitters,
                )
            ),
            receiver_marker: EntitySelector(
                EntitySelectorConfig(
                    domain=INFRARED_DOMAIN,
                    include_entities=async_get_receivers(self.hass),
                )
            ),
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
