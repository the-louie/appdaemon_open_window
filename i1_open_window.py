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
  nowcast_sensor: sensor.met_nowcast_precipitation  # Optional: MET.no nowcast precipitation sensor
"""

import time
import traceback
from datetime import datetime, timedelta
from typing import Dict, Any

import appdaemon.plugins.hass.hassapi as hass

import json
import os

import ha_states
import notification_policy as policy


class TemperatureWindowNotification(hass.Hass):
    """AppDaemon app that monitors temperature and window/door sensors and sends notifications when conditions are met."""

    def initialize(self):
        """Initialize the app and set up event listeners."""
        self.log("Loading TemperatureWindowNotification")
        try:
            # Load and validate configuration
            self.temperature_config = self.args.get("temperature", {})
            self.window_config = self.args.get("window", {})
            self.messages_config = self.args.get("messages", {})
            self.time_config = self.args.get("when", {})
            self.persons = self.args.get("persons", [])
            self.nowcast_sensor = self.args.get("nowcast_sensor")

            # Android companion-app delivery settings. The default HA notification channel
            # can be disabled on the phone, which silently discards every notification sent
            # to it - HA reports success and nothing arrives. Sending on a dedicated channel
            # keeps these alerts independent of that setting and lets them be muted on
            # their own without affecting other apps. See backlog T-52.
            self.notification_channel = self.args.get("notification_channel", "temperature_alerts")
            self.notification_priority = self.args.get("notification_priority", "high")

            # Validate required sections and keys
            if not self.temperature_config:
                raise ValueError("Missing required configuration: temperature")
            for key in ["sensor", "below", "above"]:
                if key not in self.temperature_config:
                    raise ValueError(f"Missing required key '{key}' in temperature configuration")

            if not self.window_config:
                raise ValueError("Missing required configuration: window")
            for key in ["sensor", "below", "above"]:
                if key not in self.window_config:
                    raise ValueError(f"Missing required key '{key}' in window configuration")

            if not self.messages_config:
                raise ValueError("Missing required configuration: messages")
            for key in ["below", "above", "title", "cooldown"]:
                if key not in self.messages_config:
                    raise ValueError(f"Missing required key '{key}' in messages configuration")

            if not self.time_config:
                raise ValueError("Missing required configuration: when")
            for key in ["after", "before"]:
                if key not in self.time_config:
                    raise ValueError(f"Missing required key '{key}' in when configuration")

            # Validate types and values
            if self.nowcast_sensor is not None and not isinstance(self.nowcast_sensor, str):
                raise ValueError("nowcast_sensor must be a string if provided")
            if not isinstance(self.temperature_config["sensor"], str):
                raise ValueError("temperature.sensor must be a string")
            try:
                below, above = float(self.temperature_config["below"]), float(self.temperature_config["above"])
                if below >= above:
                    raise ValueError("temperature.below must be less than temperature.above")
                self.temperature_config["below"], self.temperature_config["above"] = below, above
            except (ValueError, TypeError):
                raise ValueError("temperature.below and temperature.above must be numbers")
            if not isinstance(self.window_config["sensor"], str):
                raise ValueError("window.sensor must be a string")
            if self.window_config["below"] not in ["on", "off"] or self.window_config["above"] not in ["on", "off"]:
                raise ValueError("window.below and window.above must be 'on' or 'off'")
            for field in ["below", "above", "title"]:
                if not isinstance(self.messages_config[field], str) or not self.messages_config[field].strip():
                    raise ValueError(f"messages.{field} must be a non-empty string")
            try:
                cooldown = int(self.messages_config["cooldown"])
                if cooldown <= 0:
                    raise ValueError("messages.cooldown must be a positive integer")
                self.messages_config["cooldown"] = cooldown
            except (ValueError, TypeError):
                raise ValueError("messages.cooldown must be a positive integer")
            try:
                after, before = int(self.time_config["after"]), int(self.time_config["before"])
                if not (0 <= after <= 23) or not (0 <= before <= 23):
                    raise ValueError("when.after and when.before must be between 0 and 23")
                if after == before:
                    raise ValueError("when.after and when.before cannot be the same")
                self.time_config["after"], self.time_config["before"] = after, before
            except (ValueError, TypeError):
                raise ValueError("when.after and when.before must be integers")
            if not isinstance(self.persons, list) or not self.persons:
                raise ValueError("persons must be a non-empty list")
            for i, person in enumerate(self.persons):
                if not isinstance(person, dict) or "notify" not in person:
                    raise ValueError(f"person {i} must be a dictionary with 'notify' field")
                if not isinstance(person["notify"], str) or not person["notify"].strip():
                    raise ValueError(f"person {i}.notify must be a non-empty string")
                if "name" in person and not isinstance(person["name"], str):
                    raise ValueError(f"person {i}.name must be a string")
                if "tracker" in person and not isinstance(person["tracker"], str):
                    raise ValueError(f"person {i}.tracker must be a string")

            # Initialize state
            # Policy D2. Two halves were missing:
            #
            #   Quiet hours -- there were none. The 30-minute cooldown means a
            #   window left open on a cold night re-notified every half hour
            #   until dawn.
            #
            #   Persistence -- this dict died with the process. Every AppDaemon
            #   restart cleared every cooldown, and with it every "Ignore
            #   today": the action promises silence until tomorrow and quietly
            #   delivered silence until the next restart.
            #
            # `decide()` rather than `apply()`, deliberately. apply() drops keys
            # absent from the active set, which would erase the ignore of anyone
            # who happened to be away when the check ran. decide() does not
            # mutate, so entries survive a pass that skipped their owner.
            self.quiet_hours = self.args.get("quiet_hours", True)
            self.quiet_start = self.args.get("quiet_start", policy.DEFAULT_QUIET_START)
            self.quiet_end = self.args.get("quiet_end", policy.DEFAULT_QUIET_END)
            self.state_file = self.args.get(
                "state_file", f"/conf/open_window_{self.name}.json"
            )
            self._message_cooldowns: Dict[str, float] = self._load_state()
            self._precipitation_cache = {"result": False, "timestamp": 0}

            # Set up event listeners and scheduling
            self.listen_event(self._handle_notification_action, "mobile_app_notification_action")

            # Schedule checks
            current_hour = self.get_now().hour
            after_hour, before_hour = self.time_config["after"], self.time_config["before"]
            if after_hour <= current_hour < before_hour:
                self.run_every(self._check_conditions, "now", 60)
                self.log("Started periodic checks (within active time window)")
            else:
                now = self.get_now()
                after_hour = self.time_config["after"]
                if now.hour < after_hour:
                    next_check = now.replace(hour=after_hour, minute=0, second=0, microsecond=0)
                else:
                    next_check = (now + timedelta(days=1)).replace(hour=after_hour, minute=0, second=0, microsecond=0)
                self.run_at(self._start_checks, next_check)

            after_hour = self.time_config["after"]
            self.run_daily(self._start_checks, f"{after_hour:02d}:00:00")

            self.log("TemperatureWindowNotification initialized successfully")
        except ValueError as e:
            self.log(f"Configuration error: {e}", level="ERROR")
            raise

    def _start_checks(self, kwargs):
        """Start periodic condition checking."""
        self.run_every(self._check_conditions, "now", 60)

    def _check_conditions(self, kwargs):
        """Check temperature and window conditions and send notifications if needed."""
        # Get temperature
        temp_state = self.get_state(self.temperature_config["sensor"])
        if ha_states.not_reporting(temp_state):
            return
        try:
            temperature = float(temp_state)
        except (ValueError, TypeError):
            return

        # Get window state
        window_state = self.get_state(self.window_config["sensor"])
        window_open = window_state == "on"

        # Evaluate conditions and send notifications
        temp_above, temp_below = self.temperature_config["above"], self.temperature_config["below"]
        window_above, window_below = self.window_config["above"] == "on", self.window_config["below"] == "on"

        # Check if temperature is too high and window should be open but isn't
        if temperature >= temp_above and window_open != window_above:
            if window_above and self._precipitation_expected():
                self.log("Skipping open window notification due to precipitation forecast")
                return
            message = self.messages_config["above"]
            self.log(f"ALERT: {message}")
            self._send_notification(message, temperature)
            return

        # Check if temperature is too low and window should be closed but isn't
        if temperature < temp_below and window_open != window_below:
            message = self.messages_config["below"]
            self.log(f"ALERT: {message}")
            self._send_notification(message, temperature)
            return



    def _precipitation_expected(self) -> bool:
        """Return True if precipitation is detected or forecasted within 30 minutes, else False."""
        if not self.nowcast_sensor:
            return False

        # Cache result for 5 minutes to avoid repeated API calls
        now = time.time()
        if now - self._precipitation_cache["timestamp"] < 300:
            return self._precipitation_cache["result"]

        state = self.get_state(self.nowcast_sensor, attribute=None)
        if ha_states.not_reporting(state):
            self._precipitation_cache = {"result": False, "timestamp": now}
            return False

        try:
            if float(state) > 0:
                self._precipitation_cache = {"result": True, "timestamp": now}
                return True
        except Exception:
            pass

        forecast = self.get_state(self.nowcast_sensor, attribute="forecast")
        if not isinstance(forecast, list):
            self._precipitation_cache = {"result": False, "timestamp": now}
            return False

        # Aware, in HA's zone: forecast datetimes parse aware ('Z' becomes +00:00),
        # and aware-minus-naive raised TypeError into the bare except below --
        # every Z-suffixed entry was silently skipped (found in S8-05).
        current_time = self.get_now()
        for entry in forecast:
            dt_str = entry.get("datetime")
            precip = entry.get("precipitation")
            if dt_str is None or precip is None:
                continue
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if 0 <= (dt - current_time).total_seconds() <= 1800 and float(precip) > 0:
                    self._precipitation_cache = {"result": True, "timestamp": now}
                    return True
            except Exception:
                continue

        self._precipitation_cache = {"result": False, "timestamp": now}
        return False

    def _notification_data(self) -> dict:
        """Build the companion-app data block for a notification.

        Returns the Android delivery hints every notify call in this app must carry:
        a dedicated channel, plus priority/ttl so the message is not deferred by Doze.
        Returns an empty dict if no channel is configured, so the caller can pass it
        unconditionally.
        """
        if not self.notification_channel:
            return {}
        data = {"channel": self.notification_channel}
        if self.notification_priority:
            data["priority"] = self.notification_priority
            data["ttl"] = 0
        return data

    def _send_notification(self, message: str, temperature: float):
        """Send notification to all persons at home."""
        title = self.messages_config["title"]
        full_message = f"{message} ({temperature}°C)"
        cooldown_seconds = self.messages_config["cooldown"]

        for person in self.persons:
            notify_service = person.get("notify")
            if not notify_service:
                continue

            tracker = person.get("tracker")
            if tracker and self.get_state(tracker) != "home":
                continue

            send, reason = policy.decide(
                notify_service, time.time(), self.get_now().hour,
                self._message_cooldowns,
                quiet_start=self.quiet_start, quiet_end=self.quiet_end,
                repeat_after=cooldown_seconds,
            )
            if not send:
                self.log(f"Holding notification to {notify_service}: {reason}",
                         level="DEBUG")
                continue

            try:
                action_data = {
                    "actions": [{
                        "action": f"{self.name}.ignore.{notify_service}",
                        "title": "Ignore today"
                    }]
                }
                # Channel keys spread LAST so a future edit to action_data cannot
                # silently override them and reintroduce the dropped-notification bug.
                self.call_service(f"notify/{notify_service}", message=full_message, data={**action_data, **self._notification_data()})
                self._message_cooldowns[notify_service] = time.time()
                self._save_state()
                self.log(f"Notification sent to {notify_service}")
            except Exception as e:
                line_num = traceback.extract_tb(e.__traceback__)[-1].lineno
                self.log(f"Failed to send notification to {notify_service}: {e} (line {line_num})", level="ERROR")

    def _load_state(self):
        """Read persisted cooldowns. Missing or corrupt starts empty."""
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            return {k: v for k, v in data.items() if isinstance(v, (int, float))}
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as e:
            self.log(f"Cooldown state unreadable, starting fresh: {e}", level="WARNING")
            return {}

    def _save_state(self):
        """Persist atomically. Failure must not stop the alert going out."""
        tmp = f"{self.state_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._message_cooldowns, fh)
            os.replace(tmp, self.state_file)
        except OSError as e:
            self.log(
                f"Could not persist cooldown state: {e} -- cooldowns and any "
                f"'Ignore today' will be lost at the next restart", level="WARNING")

    def _handle_notification_action(self, event_name: str, data: Dict[str, Any], kwargs):
        """Handle notification action responses from mobile app."""
        action = data.get("action", "")
        if not action or "." not in action:
            return

        action_parts = action.split(".")
        if len(action_parts) != 3 or action_parts[0] != self.name:
            return

        action_type, notify_service = action_parts[1], action_parts[2]
        if action_type == "ignore":
            try:
                now = self.get_now()
                tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                self._message_cooldowns[notify_service] = tomorrow_start.timestamp()
                self._save_state()
                self.log(f"Ignore set for {notify_service} until tomorrow")
            except Exception as e:
                line_num = traceback.extract_tb(e.__traceback__)[-1].lineno
                self.log(f"Failed to set ignore until tomorrow for {notify_service}: {e} (line {line_num})", level="ERROR")
