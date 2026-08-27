"""Common entity for the Tadiran Infrared integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class TadiranInfraredEntity(Entity):
    """Base entity for a Tadiran air conditioner."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        unique_id_suffix: str,
        *,
        device_name: str = "Tadiran inverter AC",
        manufacturer: str = "Tadiran",
        model: str = "YB1FA infrared",
    ) -> None:
        """Initialize the entity."""
        self._attr_unique_id = f"{entry.entry_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer=manufacturer,
            model=model,
        )
