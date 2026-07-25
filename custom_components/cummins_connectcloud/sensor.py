"""Sensor platform for Cummins Connect Cloud."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import CumminsDataUpdateCoordinator

# Straight telemetry -> sensor mappings. Field meanings are documented in
# docs/DESIGN.md section 3; enum fields (gensetStatus, loadStatus,
# powerSource) are left out until their integer codes are confirmed.
SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="batteryVoltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="engineRuntime",
        translation_key="engine_runtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:engine",
    ),
    SensorEntityDescription(
        key="gensetPercentLoad",
        translation_key="load",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
    ),
    SensorEntityDescription(
        key="gensetVoltage",
        translation_key="output_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="frequencyOP",
        translation_key="output_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="averageEngineSpeed",
        translation_key="engine_speed",
        native_unit_of_measurement="RPM",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    SensorEntityDescription(
        key="SoftwareVersion",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chip",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for a config entry."""
    coordinator: CumminsDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        CumminsSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(CumminsLastCheckInSensor(coordinator, entry))
    async_add_entities(entities)


def _device_info(coordinator: CumminsDataUpdateCoordinator, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.asset_id)},
        name=entry.title,
        manufacturer="Cummins",
        model="Home Standby Generator",
    )


class CumminsSensor(CoordinatorEntity[CumminsDataUpdateCoordinator], SensorEntity):
    """Generic telemetry sensor sourced straight from Assets/Detail."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CumminsDataUpdateCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.asset_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> Any:
        return self.coordinator.data.get(self.entity_description.key)


class CumminsLastCheckInSensor(CoordinatorEntity[CumminsDataUpdateCoordinator], SensorEntity):
    """Timestamp of the generator's last telemetry check-in."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_check_in"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CumminsDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.asset_id}_last_check_in"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> datetime | None:
        raw = self.coordinator.data.get("LastCheckIn")
        if not raw:
            return None
        return dt_util.parse_datetime(raw)
