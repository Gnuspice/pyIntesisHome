"""Support for IntesisHome and airconwithme Smart AC Controllers."""
import logging
from random import randrange

from pyintesishome import IHAuthenticationError, IHConnectionError, IntesisHome
import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateDevice
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    HVAC_MODE_COOL,
    HVAC_MODE_DRY,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_OFF,
    SUPPORT_FAN_MODE,
    SUPPORT_SWING_MODE,
    SUPPORT_TARGET_TEMPERATURE,
    SWING_BOTH,
    SWING_HORIZONTAL,
    SWING_OFF,
    SWING_VERTICAL,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_DEVICE,
    CONF_PASSWORD,
    CONF_USERNAME,
    TEMP_CELSIUS,
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)

IH_DEVICE_INTESISHOME = "IntesisHome"
IH_DEVICE_AIRCONWITHME = "airconwithme"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_DEVICE, default=IH_DEVICE_INTESISHOME): vol.In(
            [IH_DEVICE_AIRCONWITHME, IH_DEVICE_INTESISHOME]
        ),
    }
)

MAP_IH_TO_HVAC_MODE = {
    "auto": HVAC_MODE_HEAT_COOL,
    "cool": HVAC_MODE_COOL,
    "dry": HVAC_MODE_DRY,
    "fan": HVAC_MODE_FAN_ONLY,
    "heat": HVAC_MODE_HEAT,
    "off": HVAC_MODE_OFF,
}

MAP_HVAC_MODE_TO_IH = {v: k for k, v in MAP_IH_TO_HVAC_MODE.items()}

MAP_STATE_ICONS = {
    HVAC_MODE_COOL: "mdi:snowflake",
    HVAC_MODE_DRY: "mdi:water-off",
    HVAC_MODE_FAN_ONLY: "mdi:fan",
    HVAC_MODE_HEAT: "mdi:white-balance-sunny",
    HVAC_MODE_HEAT_COOL: "mdi:cached",
}

IH_HVAC_MODES = [
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_DRY,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_OFF,
]


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Create the IntesisHome climate devices."""
    ih_user = config[CONF_USERNAME]
    ih_pass = config[CONF_PASSWORD]
    device_type = config[CONF_DEVICE]

    controller = IntesisHome(
        ih_user,
        ih_pass,
        hass.loop,
        websession=async_get_clientsession(hass),
        device_type=device_type,
    )
    try:
        await controller.poll_status()
    except IHAuthenticationError:
        _LOGGER.error("Invalid username or password")
        return
    except IHConnectionError:
        _LOGGER.error("Error connecting to the %s server", device_type)
        raise PlatformNotReady

    ih_devices = controller.get_devices()
    if ih_devices:
        async_add_entities(
            [
                IntesisAC(ih_device_id, device, controller)
                for ih_device_id, device in ih_devices.items()
            ],
            True,
        )
    else:
        _LOGGER.error(
            "Error getting device list from %s API: %s",
            device_type,
            controller.error_message,
        )
        await controller.stop()


class IntesisAC(ClimateDevice):
    """Represents an Intesishome air conditioning device."""

    def __init__(self, ih_device_id, ih_device, controller):
        """Initialize the thermostat."""
        self._controller = controller
        self._device_id = ih_device_id
        self._ih_device = ih_device
        self._device_name = ih_device.get("name")
        self._device_type = controller.device_type
        self._connected = None
        self._setpoint_step = 1
        self._current_temp = None
        self._max_temp = None
        self._min_temp = None
        self._target_temp = None
        self._outdoor_temp = None
        self._run_hours = None
        self._rssi = None
        self._swing = None
        self._swing_list = None
        self._vvane = None
        self._hvane = None
        self._power = False
        self._fan_speed = None
        self._hvac_mode = None
        self._fan_modes = controller.get_fan_speed_list(ih_device_id)
        self._support = SUPPORT_TARGET_TEMPERATURE | SUPPORT_FAN_MODE
        self._swing_list = [SWING_OFF]

        if ih_device.get("config_vertical_vanes"):
            self._swing_list.append(SWING_VERTICAL)
        if ih_device.get("config_horizontal_vanes"):
            self._swing_list.append(SWING_HORIZONTAL)
        if len(self._swing_list) == 3:
            self._swing_list.append(SWING_BOTH)
            self._support |= SUPPORT_SWING_MODE
        elif len(self._swing_list) == 2:
            self._support |= SUPPORT_SWING_MODE

    async def async_added_to_hass(self):
        """Subscribe to event updates."""
        _LOGGER.debug("Added climate device with state: %s", repr(self._ih_device))
        await self._controller.add_update_callback(self.async_update_callback)
        try:
            await self._controller.connect()
        except IHConnectionError as e:
            _LOGGER.error("Exception connecting to IntesisHome:  %s", repr(e))

    @property
    def name(self):
        """Return the name of the AC device."""
        return self._device_name

    @property
    def temperature_unit(self):
        """Intesishome API uses celsius on the backend."""
        return TEMP_CELSIUS

    @property
    def device_state_attributes(self):
        """Return the device specific state attributes."""
        attrs = {}
        if len(self._swing_list) > 1:
            attrs["vertical_vane"] = self._vvane
            attrs["horizontal_vane"] = self._hvane
        if self._outdoor_temp:
            attrs["outdoor_temp"] = self._outdoor_temp
        return attrs

    @property
    def unique_id(self):
        """Return unique ID for this device."""
        return self._device_id

    @property
    def target_temperature_step(self) -> float:
        """Return whether setpoint should be whole or half degree precision."""
        return self._setpoint_step

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        if hvac_mode:
            await self.async_set_hvac_mode(hvac_mode)

        if temperature:
            _LOGGER.debug("Setting %s to %s degrees", self._device_type, temperature)
            await self._controller.set_temperature(self._device_id, temperature)
            self._target_temp = temperature

        # Write updated temperature to HA state to avoid flapping (API confirmation is slow)
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set operation mode."""
        _LOGGER.debug("Setting %s to %s mode", self._device_type, hvac_mode)
        if hvac_mode == HVAC_MODE_OFF:
            self._power = False
            await self._controller.set_power_off(self._device_id)
            # Write changes to HA, API can be slow to push changes
            self.async_write_ha_state()
            return

        # First check device is turned on
        if not self._controller.is_on(self._device_id):
            self._power = True
            await self._controller.set_power_on(self._device_id)

        # Set the mode
        await self._controller.set_mode(self._device_id, MAP_HVAC_MODE_TO_IH[hvac_mode])

        # Send the temperature again in case changing modes has changed it
        if self._target_temp:
            await self._controller.set_temperature(self._device_id, self._target_temp)

        # Updates can take longer than 2 seconds, so update locally
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode):
        """Set fan mode (from quiet, low, medium, high, auto)."""
        await self._controller.set_fan_speed(self._device_id, fan_mode)

        # Updates can take longer than 2 seconds, so update locally
        self._fan_speed = fan_mode
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode):
        """Set the vertical vane."""
        if swing_mode == SWING_OFF:
            await self._controller.set_vertical_vane(self._device_id, "auto/stop")
            await self._controller.set_horizontal_vane(self._device_id, "auto/stop")
        elif swing_mode == SWING_BOTH:
            await self._controller.set_vertical_vane(self._device_id, "swing")
            await self._controller.set_horizontal_vane(self._device_id, "swing")
        elif swing_mode == SWING_HORIZONTAL:
            await self._controller.set_vertical_vane(self._device_id, "auto/stop")
            await self._controller.set_horizontal_vane(self._device_id, "swing")
        elif swing_mode == SWING_VERTICAL:
            await self._controller.set_vertical_vane(self._device_id, "swing")
            await self._controller.set_horizontal_vane(self._device_id, "auto/stop")
        self._swing = swing_mode

    async def async_update(self):
        """Copy values from controller dictionary to climate device."""
        # Update values from controller's device dictionary
        self._connected = self._controller.is_connected
        self._current_temp = self._controller.get_temperature(self._device_id)
        self._fan_speed = self._controller.get_fan_speed(self._device_id)
        self._power = self._controller.is_on(self._device_id)
        self._min_temp = self._controller.get_min_setpoint(self._device_id)
        self._max_temp = self._controller.get_max_setpoint(self._device_id)
        self._rssi = self._controller.get_rssi(self._device_id)
        self._run_hours = self._controller.get_run_hours(self._device_id)
        self._target_temp = self._controller.get_setpoint(self._device_id)
        self._outdoor_temp = self._controller.get_outdoor_temperature(self._device_id)

        # Operation mode
        mode = self._controller.get_mode(self._device_id)
        self._hvac_mode = MAP_IH_TO_HVAC_MODE.get(mode)

        # Swing mode
        # Climate module only supports one swing setting.
        self._vvane = self._controller.get_vertical_swing(self._device_id)
        self._hvane = self._controller.get_horizontal_swing(self._device_id)

        if self._vvane == "swing" and self._hvane == "swing":
            self._swing = SWING_BOTH
        elif self._vvane == "swing":
            self._swing = SWING_VERTICAL
        elif self._hvane == "swing":
            self._swing = SWING_HORIZONTAL
        else:
            self._swing = SWING_OFF

    async def async_will_remove_from_hass(self):
        """Shutdown the controller when the device is being removed."""
        await self._controller.stop()

    @property
    def icon(self):
        """Return the icon for the current state."""
        icon = None
        if self._power:
            icon = MAP_STATE_ICONS.get(self._hvac_mode)
        return icon

    async def async_update_callback(self, device_id=None):
        """Let HA know there has been an update from the controller."""
        # Track changes in connection state
        if not self._controller.is_connected and self._connected:
            # Connection has dropped
            self._connected = False
            reconnect_minutes = 1 + randrange(10)
            _LOGGER.error(
                "Connection to %s API was lost. Reconnecting in %i minutes",
                self._device_type,
                reconnect_minutes,
            )
            # Schedule reconnection
            async_call_later(
                self.hass, reconnect_minutes * 60, self._controller.connect()
            )

        if self._controller.is_connected and not self._connected:
            # Connection has been restored
            self._connected = True
            _LOGGER.debug("Connection to %s API was restored", self._device_type)

        if not device_id or self._device_id == device_id:
            # Update all devices if no device_id was specified
            _LOGGER.debug(
                "%s API sent a status update for device %s",
                self._device_type,
                device_id,
            )
            self.async_schedule_update_ha_state(True)

    @property
    def min_temp(self):
        """Return the minimum temperature for the current mode of operation."""
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature for the current mode of operation."""
        return self._max_temp

    @property
    def should_poll(self):
        """Poll for updates if pyIntesisHome doesn't have a socket open."""
        return False

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return IH_HVAC_MODES

    @property
    def fan_mode(self):
        """Return whether the fan is on."""
        return self._fan_speed

    @property
    def swing_mode(self):
        """Return current swing mode."""
        return self._swing

    @property
    def fan_modes(self):
        """List of available fan modes."""
        return self._fan_modes

    @property
    def swing_modes(self):
        """List of available swing positions."""
        return self._swing_list

    @property
    def available(self) -> bool:
        """If the device hasn't been able to connect, mark as unavailable."""
        return self._connected or self._connected is None

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temp

    @property
    def hvac_mode(self):
        """Return the current mode of operation if unit is on."""
        if self._power:
            return self._hvac_mode
        return HVAC_MODE_OFF

    @property
    def target_temperature(self):
        """Return the current setpoint temperature if unit is on."""
        return self._target_temp

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support
