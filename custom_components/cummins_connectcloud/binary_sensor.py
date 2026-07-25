"""Binary sensor platform for Cummins Connect Cloud."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STALE_AFTER
from .coordinator import CumminsDataUpdateCoordinator

BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="isRunning",
        translation_key="running",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="utilityAvailable",
        translation_key="utility_power_available",
        device_class=BinarySensorDeviceClass.POWER,
    ),
    BinarySensorEntityDescription(
        key="isExercising",
        translation_key="exercising",
        icon="mdi:test-tube",
    ),
    BinarySensorEntityDescription(
        key="isStandbyEnabled",
        translation_key="standby_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="isRemoteEnabled",
        translation_key="remote_control_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensors for a config entry."""
    coordinator: CumminsDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        CumminsBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    ]
    entities.append(CumminsFaultBinarySensor(coordinator, entry))
    entities.append(CumminsStaleDataBinarySensor(coordinator, entry))
    async_add_entities(entities)


def _device_info(coordinator: CumminsDataUpdateCoordinator, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.asset_id)},
        name=entry.title,
        manufacturer="Cummins",
        model="Home Standby Generator",
    )


class CumminsBinarySensor(CoordinatorEntity[CumminsDataUpdateCoordinator], BinarySensorEntity):
    """Generic boolean telemetry sensor (0/1 fields from Assets/Detail)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CumminsDataUpdateCoordinator,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.asset_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self.entity_description.key)
        if value is None:
            return None
        return bool(value)


class CumminsFaultBinarySensor(CoordinatorEntity[CumminsDataUpdateCoordinator], BinarySensorEntity):
    """On when the generator is reporting a fault (faultType != 0)."""

    _attr_has_entity_name = True
    _attr_translation_key = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: CumminsDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.asset_id}_fault"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def is_on(self) -> bool | None:
        fault_type = self.coordinator.data.get("faultType")
        if fault_type is None:
            return None
        return fault_type != 0

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"fault_type": self.coordinator.data.get("faultType")}


class CumminsStaleDataBinarySensor(
    CoordinatorEntity[CumminsDataUpdateCoordinator], BinarySensorEntity
):
    """On when the generator hasn't checked in recently (likely offline)."""

    _attr_has_entity_name = True
    _attr_translation_key = "data_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CumminsDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.asset_id}_data_stale"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def is_on(self) -> bool | None:
        raw = self.coordinator.data.get("LastCheckIn")
        if not raw:
            return None
        last = dt_util.parse_datetime(raw)
        if last is None:
            return None
        return dt_util.utcnow() - dt_util.as_utc(last) > STALE_AFTER
