"""
AppDaemon script for Home Assistant that sends notifications based on temperature
and window/door sensor states.

Configuration example:
open_window_notification:
  module: i1_open_window
  class: TemperatureWindowNotification
  persons:
    - name: Lars
      notify: mobile_app_iphone_28
      tracker: device_tracker.iphone_28
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
    title: "Bedroom temp"
    cooldown: 1800
  when:
    after: 15
    before: 22
"""

import time
from datetime import datetime, timedelta

import pytz
import appdaemon.plugins.hass.hassapi as hass

# Default timezone
DEFAULT_TIMEZONE = pytz.timezone('Europe/Stockholm')


class TemperatureWindowNotification(hass.Hass):
    """
    AppDaemon app that monitors temperature and window/door sensors
    and sends notifications when conditions are met.

    This app intelligently monitors temperature sensors and binary window/door sensors,
    sending notifications only when the temperature and window state combination
    requires user action. It includes features like time windows, presence detection,
    cooldown periods, and action buttons for user interaction.

    The app operates on the principle that:
    - When temperature is too high (≥ above_threshold), the window should be open
    - When temperature is too low (< below_threshold), the window should be closed

    Notifications are only sent when these conditions are not met.
    """

    def initialize(self):
        """
        Initialize the app and set up event listeners.

        This method is called by AppDaemon when the app starts. It:
        1. Loads and validates the configuration
        2. Initializes the message cooldown tracking
        3. Sets up periodic condition checking (every 60 seconds)
        4. Registers event listener for notification actions

        Raises:
            ValueError: If required configuration is missing or invalid
        """
        self.log("Loading TemperatureWindowNotification")

        # Load configuration
        self._load_config()

        # Initialize cooldown tracking
        self._message_cooldowns = {}

        # Set up periodic checks and event listeners
        self.run_every(self._check_conditions, "now", 60)  # Check every minute
        self.listen_event(self._handle_notification_action, "mobile_app_notification_action")

        self.log("TemperatureWindowNotification initialized successfully")

    def _load_config(self):
        """
        Load and validate configuration from args.

        Extracts configuration from self.args and validates that all required
        keys are present and have the correct types. Configuration includes
        temperature sensor settings, window sensor settings, notification
        messages, time windows, and person configurations.

        Raises:
            ValueError: If any required configuration section or key is missing,
                       or if configuration values have incorrect types
        """
        self.temperature_config = self.args.get("temperature", {})
        self.window_config = self.args.get("window", {})
        self.messages_config = self.args.get("messages", {})
        self.time_config = self.args.get("when", {})
        self.persons = self.args.get("persons", [])

        # Validate required configuration sections and keys
        self._validate_required_configs()

        # Validate configuration value types
        self._validate_config_types()

        # Validate person configurations
        self._validate_persons_config()

    def _validate_required_configs(self):
        """Validate that all required configuration sections and keys are present."""
        required_configs = [
            ("temperature", self.temperature_config, ["sensor", "below", "above"]),
            ("window", self.window_config, ["sensor", "below", "above"]),
            ("messages", self.messages_config, ["below", "above", "title", "cooldown"]),
            ("when", self.time_config, ["after", "before"])
        ]

        for config_name, config, required_keys in required_configs:
            if not config:
                raise ValueError(f"Missing required configuration: {config_name}")
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required key '{key}' in {config_name} configuration")

    def _validate_config_types(self):
        """Validate that configuration values have the correct types."""
        # Validate temperature configuration
        self._validate_temperature_config()

        # Validate window configuration
        self._validate_window_config()

        # Validate messages configuration
        self._validate_messages_config()

        # Validate time configuration
        self._validate_time_config()

    def _validate_temperature_config(self):
        """Validate temperature configuration types."""
        config = self.temperature_config

        # Validate sensor is a string
        if not isinstance(config["sensor"], str):
            raise ValueError("temperature.sensor must be a string")

        # Validate below and above are numbers
        try:
            below = float(config["below"])
            above = float(config["above"])
        except (ValueError, TypeError):
            raise ValueError("temperature.below and temperature.above must be numbers")

        # Validate temperature range (below should be less than above)
        if below >= above:
            raise ValueError("temperature.below must be less than temperature.above")

        # Store validated values
        self.temperature_config["below"] = below
        self.temperature_config["above"] = above

    def _validate_window_config(self):
        """Validate window configuration types."""
        config = self.window_config

        # Validate sensor is a string
        if not isinstance(config["sensor"], str):
            raise ValueError("window.sensor must be a string")

        # Validate below and above are valid window states
        valid_states = ["on", "off"]
        if config["below"] not in valid_states:
            raise ValueError("window.below must be 'on' or 'off'")
        if config["above"] not in valid_states:
            raise ValueError("window.above must be 'on' or 'off'")

    def _validate_messages_config(self):
        """Validate messages configuration types."""
        config = self.messages_config

        # Validate all message fields are strings
        string_fields = ["below", "above", "title"]
        for field in string_fields:
            if not isinstance(config[field], str):
                raise ValueError(f"messages.{field} must be a string")
            if not config[field].strip():
                raise ValueError(f"messages.{field} cannot be empty")

        # Validate cooldown is a positive integer
        try:
            cooldown = int(config["cooldown"])
            if cooldown <= 0:
                raise ValueError("messages.cooldown must be a positive integer")
        except (ValueError, TypeError):
            raise ValueError("messages.cooldown must be a positive integer")

        # Store validated value
        self.messages_config["cooldown"] = cooldown

    def _validate_time_config(self):
        """Validate time configuration types."""
        config = self.time_config

        # Validate after and before are integers in valid range
        try:
            after = int(config["after"])
            before = int(config["before"])
        except (ValueError, TypeError):
            raise ValueError("when.after and when.before must be integers")

        # Validate hour range (0-23)
        if not (0 <= after <= 23):
            raise ValueError("when.after must be between 0 and 23")
        if not (0 <= before <= 23):
            raise ValueError("when.before must be between 0 and 23")

        # Validate time window makes sense
        if after == before:
            raise ValueError("when.after and when.before cannot be the same")

        # Store validated values
        self.time_config["after"] = after
        self.time_config["before"] = before

    def _validate_persons_config(self):
        """Validate persons configuration types."""
        if not isinstance(self.persons, list):
            raise ValueError("persons must be a list")

        if not self.persons:
            raise ValueError("persons list cannot be empty")

        for i, person in enumerate(self.persons):
            if not isinstance(person, dict):
                raise ValueError(f"person {i} must be a dictionary")

            # Validate required person fields
            if "notify" not in person:
                raise ValueError(f"person {i} missing required field 'notify'")

            if not isinstance(person["notify"], str):
                raise ValueError(f"person {i}.notify must be a string")

            if not person["notify"].strip():
                raise ValueError(f"person {i}.notify cannot be empty")

            # Validate optional fields if present
            if "name" in person and not isinstance(person["name"], str):
                raise ValueError(f"person {i}.name must be a string")

            if "tracker" in person and not isinstance(person["tracker"], str):
                raise ValueError(f"person {i}.tracker must be a string")

    def _check_conditions(self, kwargs):
        """
        Check temperature and window conditions and send notifications if needed.

        This is the main monitoring method called every 60 seconds. It:
        1. Retrieves current temperature from the configured sensor
        2. Validates the temperature value is numeric and available
        3. Retrieves current window/door state from the configured sensor
        4. Checks if current time is within the allowed time window
        5. Evaluates conditions and sends notifications if needed

        Args:
            kwargs: AppDaemon callback arguments (unused)
        """
        # Get current temperature
        temp_sensor = self.temperature_config["sensor"]
        temp_state = self.get_state(temp_sensor)

        if not self._is_valid_state(temp_state):
            self.log(f"Temperature sensor {temp_sensor} is unavailable: {temp_state}", level="WARNING")
            return

        try:
            temperature = float(temp_state)
        except (ValueError, TypeError):
            self.log(f"Invalid temperature value: {temp_state}", level="ERROR")
            return

        # Get window state
        window_sensor = self.window_config["sensor"]
        window_state = self.get_state(window_sensor)
        window_open = window_state == "on"

        self.log(f"Temperature: {temperature}°C, Window open: {window_open}", level="DEBUG")

        # Check time constraints
        if not self._is_within_time_window():
            return

        # Check conditions and send notifications
        self._evaluate_and_notify(temperature, window_open)

    def _is_valid_state(self, state):
        """
        Check if a sensor state is valid.

        Validates that a sensor state is not None, "unavailable", or "unknown".
        These states typically indicate that the sensor is offline or not
        providing valid data.

        Args:
            state: The sensor state to validate

        Returns:
            bool: True if the state is valid, False otherwise
        """
        return state not in ["unavailable", "unknown", None]

    def _is_within_time_window(self):
        """
        Check if current time is within the configured time window.

        Compares the current hour against the configured 'after' and 'before'
        hours. The time window is inclusive of the 'after' hour and exclusive
        of the 'before' hour (e.g., after: 15, before: 22 means 15:00-21:59).

        Returns:
            bool: True if current time is within the allowed window, False otherwise
        """
        current_hour = datetime.now().hour
        after_hour = self.time_config["after"]
        before_hour = self.time_config["before"]

        if current_hour < after_hour or current_hour >= before_hour:
            self.log(f"Outside time window: {current_hour}h (allowed: {after_hour}-{before_hour}h)", level="DEBUG")
            return False

        return True

    def _evaluate_and_notify(self, temperature, window_open):
        """
        Evaluate temperature and window conditions and send notifications.

        Compares the current temperature and window state against configured
        thresholds and expected states. Sends notifications when:
        - Temperature is ≥ above_threshold but window is not in the expected "above" state
        - Temperature is < below_threshold but window is not in the expected "below" state

        Args:
            temperature (float): Current temperature in degrees Celsius
            window_open (bool): True if window/door is open, False if closed
        """
        temp_above = self.temperature_config["above"]
        temp_below = self.temperature_config["below"]
        window_above = self.window_config["above"]
        window_below = self.window_config["below"]

        # Check if temperature is too high and window should be open
        if temperature >= temp_above and window_open == (window_above == "on"):
            message = self.messages_config["above"]
            self.log(f"ALERT: {message}", level="INFO")
            self._send_notification(message, temperature)
            return

        # Check if temperature is too low and window should be closed
        if temperature < temp_below and window_open == (window_below == "on"):
            message = self.messages_config["below"]
            self.log(f"ALERT: {message}", level="INFO")
            self._send_notification(message, temperature)
            return

    def _send_notification(self, message, temperature):
        """
        Send notification to all persons at home.

        Iterates through all configured persons and sends notifications to those
        who are currently home. Respects cooldown periods to prevent notification
        spam. Only sends notifications to persons with valid notification services.

        Args:
            message (str): The notification message to send
            temperature (float): Current temperature to include in the message
        """
        title = self.messages_config["title"]
        full_message = f"{message} ({temperature}°C)"
        cooldown_seconds = self.messages_config["cooldown"]  # Already validated as int

        for person in self.persons:
            notify_service = person.get("notify")
            tracker = person.get("tracker")

            if not notify_service:
                continue

            # Check cooldown
            last_notification = self._message_cooldowns.get(notify_service, 0)
            time_since_last = time.time() - last_notification

            if time_since_last < cooldown_seconds:
                self.log(f"Cooldown active for {notify_service}, last notification {time_since_last:.0f}s ago", level="DEBUG")
                continue

            # Check if person is home
            if tracker and self.get_state(tracker) != "home":
                self.log(f"Person {person.get('name', notify_service)} not home", level="DEBUG")
                continue

            # Send notification
            self._send_single_notification(notify_service, title, full_message)
            self._message_cooldowns[notify_service] = time.time()

    def _send_single_notification(self, notify_service, title, message):
        """
        Send a single notification with action buttons.

        Sends a notification to a specific notification service with an
        "Ignore today" action button. The action button allows users to
        suppress notifications for the rest of the day.

        Args:
            notify_service (str): The notification service to use (e.g., mobile_app_iphone)
            title (str): The notification title
            message (str): The notification message body
        """
        action_data = {
            "actions": [{
                "action": f"{self.name}.ignore.{notify_service}",
                "title": "Ignore today"
            }]
        }

        try:
            self.call_service(f"notify/{notify_service}", message=message, data=action_data)
            self.log(f"Notification sent to {notify_service}", level="DEBUG")
        except Exception as e:
            self.log(f"Failed to send notification to {notify_service}: {e}", level="ERROR")

    def _handle_notification_action(self, event_name, data, kwargs):
        """
        Handle notification action responses from mobile app.

        Processes action button responses from mobile notifications. Currently
        supports the "ignore" action which suppresses notifications for the
        rest of the day for a specific notification service.

        Args:
            event_name (str): The event name (mobile_app_notification_action)
            data (dict): Event data containing the action information
            kwargs: Additional event arguments (unused)
        """
        action = data.get("action", "")

        if not action or "." not in action:
            return

        action_parts = action.split(".")
        if len(action_parts) != 3 or action_parts[0] != self.name:
            return

        action_type, notify_service = action_parts[1], action_parts[2]

        if action_type == "ignore":
            self._set_ignore_until_tomorrow(notify_service)

    def _set_ignore_until_tomorrow(self, notify_service):
        """
        Set ignore cooldown until tomorrow for a specific notification service.

        When a user clicks "Ignore today" on a notification, this method sets
        a cooldown that prevents further notifications until the start of the
        next day. The cooldown is calculated using the configured timezone.

        Args:
            notify_service (str): The notification service to ignore (e.g., mobile_app_iphone)
        """
        try:
            # Calculate tomorrow's start time in the configured timezone
            tz = DEFAULT_TIMEZONE
            now = datetime.now(tz)
            tomorrow_start = datetime(now.year, now.month, now.day, tzinfo=tz) + timedelta(days=1)

            self._message_cooldowns[notify_service] = tomorrow_start.timestamp()
            self.log(f"Ignore set for {notify_service} until tomorrow", level="INFO")
        except Exception as e:
            self.log(f"Failed to set ignore until tomorrow for {notify_service}: {e}", level="ERROR")
