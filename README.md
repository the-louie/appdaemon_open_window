# Temperature Window Notification AppDaemon Script

A cleaned up and improved AppDaemon script for Home Assistant that monitors temperature and window/door sensors, sending notifications when conditions are met.

## Features

- **Temperature Monitoring**: Monitors temperature sensors and compares against configurable thresholds
- **Window/Door State Tracking**: Monitors binary sensors for window/door open/closed states
- **Smart Notifications**: Sends notifications only when temperature and window states don't match expected conditions
- **Time Window Support**: Only operates during specified hours
- **Presence Detection**: Only sends notifications to people who are home
- **Cooldown System**: Prevents notification spam with configurable cooldown periods
- **Action Buttons**: Mobile notifications include "Ignore today" action button
- **Weather Integration**: Suppresses open window notifications when rain is forecasted (MET.no nowcast)
- **Error Handling**: Robust error handling for sensor failures and invalid states
- **Configuration Validation**: Comprehensive type and value validation on startup
- **PEP 8 Compliant**: Follows Python style guidelines for maintainability

## Configuration

Add the following to your `apps.yaml` file:

```yaml
bedroom_temperature_notification:
  module: i1_open_window
  class: TemperatureWindowNotification
  persons:
    - name: "Your Name"
      notify: mobile_app_your_device
      tracker: device_tracker.your_device
  temperature:
    sensor: sensor.bedroom_temperature
    below: 16
    above: 20
  window:
    sensor: binary_sensor.bedroom_window
    below: on
    above: off
  messages:
    below: "Close bedroom window"
    above: "Open bedroom window"
    title: "Bedroom Temperature"
    cooldown: 1800
  when:
    after: 15
    before: 22
  nowcast_sensor: sensor.met_nowcast_precipitation  # Optional: MET.no nowcast precipitation sensor
```

## Configuration Options

### `persons`
List of people to notify:
- `name`: Human-readable name (optional, string)
- `notify`: Notification service (required, string, e.g., `mobile_app_iphone`)
- `tracker`: Device tracker to check if person is home (optional, string)

### `temperature`
Temperature sensor configuration:
- `sensor`: Entity ID of the temperature sensor (required, string)
- `below`: Temperature threshold for "too cold" condition (required, number)
- `above`: Temperature threshold for "too warm" condition (required, number)
- **Note**: `below` must be less than `above`

### `window`
Window/door sensor configuration:
- `sensor`: Entity ID of the binary sensor (required, string)
- `below`: Expected state when temperature is below threshold (required, "on" or "off")
- `above`: Expected state when temperature is above threshold (required, "on" or "off")

### `messages`
Notification message configuration:
- `below`: Message to send when temperature is too low (required, non-empty string)
- `above`: Message to send when temperature is too high (required, non-empty string)
- `title`: Notification title (required, non-empty string)
- `cooldown`: Cooldown period in seconds between notifications (required, positive integer)

### `when`
Time window configuration:
- `after`: Hour to start monitoring (required, integer 0-23)
- `before`: Hour to stop monitoring (required, integer 0-23)
- **Note**: `after` and `before` cannot be the same value

### `nowcast_sensor`
Weather integration (optional):
- `nowcast_sensor`: Entity ID of MET.no nowcast precipitation sensor (optional, string)
- **Note**: When configured, suppresses open window notifications if rain is detected or forecasted within 30 minutes

## How It Works

1. **Smart Scheduling**: The script only runs checks during the configured time window
2. **Periodic Checks**: During active hours, conditions are checked every minute
3. **Temperature Evaluation**: Compares current temperature against configured thresholds
4. **Window State Check**: Verifies if window/door state matches expected state for current temperature
5. **Time Window**: Only operates during specified hours (optimized to avoid unnecessary checks)
6. **Presence Check**: Only notifies people who are home
7. **Cooldown**: Respects cooldown periods to prevent spam
8. **Action Handling**: Processes "Ignore today" actions from mobile notifications

## Notification Logic

The script sends notifications when:
- Temperature ≥ `above` threshold AND window state ≠ `above` expected state (unless rain is forecasted)
- Temperature < `below` threshold AND window state ≠ `below` expected state

**Weather Integration**: If a `nowcast_sensor` is configured, the app checks for current precipitation and forecasts up to 30 minutes ahead. Open window notifications are suppressed if rain is detected or expected.

## Improvements Made

### Code Quality
- ✅ Removed unused imports and variables
- ✅ Added comprehensive error handling
- ✅ Improved code structure with private methods
- ✅ Added proper docstrings and comments
- ✅ Better variable naming and readability
- ✅ PEP 8 compliant (with sensible line length exceptions)
- ✅ Comprehensive type validation for all configuration values

### Functionality
- ✅ Configuration validation on startup
- ✅ Robust sensor state validation
- ✅ Better error logging and debugging
- ✅ Improved notification action handling
- ✅ More reliable timezone handling

### Maintainability
- ✅ Modular code structure
- ✅ Clear separation of concerns
- ✅ Better exception handling
- ✅ Comprehensive logging
- ✅ Well-documented with expanded docstrings
- ✅ Clean, readable code following Python best practices

## Installation

1. Copy `i1_open_window.py` to your AppDaemon `apps` directory
2. Add configuration to your `apps.yaml` file
3. Restart AppDaemon
4. Check the logs for any configuration errors

## Configuration Validation

The script includes comprehensive configuration validation that checks:

### Type Validation
- **Strings**: All sensor entity IDs, messages, and notification services must be strings
- **Numbers**: Temperature thresholds must be numeric values
- **Integers**: Time hours (0-23) and cooldown periods must be integers
- **Lists**: Persons configuration must be a list
- **Dictionaries**: Each person configuration must be a dictionary

### Value Validation
- **Temperature range**: `below` must be less than `above`
- **Time range**: Hours must be between 0-23, and `after` cannot equal `before`
- **Window states**: Must be either "on" or "off"
- **Positive values**: Cooldown must be a positive integer
- **Non-empty strings**: Messages and titles cannot be empty

### Required Fields
- All configuration sections must be present
- All required fields within each section must be provided
- At least one person must be configured

If any validation fails, the app will log a clear error message and stop initialization.

## Troubleshooting

### Common Issues

1. **Import Error**: The linter may show import errors for `appdaemon.plugins.hass.hassapi`. This is normal in development environments and won't affect the script in AppDaemon.

2. **Sensor Unavailable**: Check that your sensor entity IDs are correct and the sensors are online.

3. **No Notifications**: Verify that:
   - Time window is correct
   - People are marked as home
   - Cooldown period hasn't expired
   - Notification service is properly configured

### Debug Mode

Enable debug logging by setting the log level to DEBUG in your AppDaemon configuration:

```yaml
appdaemon:
  log_level: DEBUG
```

## License

Copyright (c) 2025, the_louie

This project is licensed under the BSD 2-Clause License - see the [LICENSE](LICENSE) file for details.

The BSD 2-Clause License is a permissive license that allows for:
- Commercial use
- Modification
- Distribution
- Private use

The only requirements are:
1. Include the original copyright notice
2. Include the license disclaimer

This makes the script suitable for both personal and commercial use with minimal restrictions.