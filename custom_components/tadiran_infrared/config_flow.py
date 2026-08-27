"""Config flow for Tadiran Infrared."""

from typing import Any, override

import voluptuous as vol
from homeassistant.components.infrared import DOMAIN as INFRARED_DOMAIN
from homeassistant.components.infrared import (
    async_get_emitters,
    async_get_receivers,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_INFRARED_ENTITY_ID,
    CONF_INFRARED_RECEIVER_ENTITY_ID,
    DOMAIN,
)


class TadiranInfraredConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Tadiran Infrared config flow."""

    VERSION = 1

    def _entity_name(self, entity_id: str) -> str:
        """Return the display name of an entity."""
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry.name or entry.original_name or entity_id if entry else entity_id

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an infrared emitter and optional receiver."""
        emitters = async_get_emitters(self.hass)
        if not emitters:
            return self.async_abort(reason="no_infrared_emitters")

        if user_input is not None:
            emitter_id = user_input[CONF_INFRARED_ENTITY_ID]
            self._async_abort_entries_match({CONF_INFRARED_ENTITY_ID: emitter_id})
            return self.async_create_entry(
                title=f"Tadiran AC via {self._entity_name(emitter_id)}",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INFRARED_ENTITY_ID): EntitySelector(
                        EntitySelectorConfig(
                            domain=INFRARED_DOMAIN,
                            include_entities=emitters,
                        )
                    ),
                    vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID): EntitySelector(
                        EntitySelectorConfig(
                            domain=INFRARED_DOMAIN,
                            include_entities=async_get_receivers(self.hass),
                        )
                    ),
                }
            ),
        )
