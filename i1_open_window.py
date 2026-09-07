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
  nowcast: true              # rain check via MET.no nowcast API (H-35)
  # latitude/longitude: optional overrides; defaults to HA's configured home
  # nowcast_user_agent: identifying UA string per MET.no TOS
"""

import time
import urllib.request
from email.utils import parsedate_to_datetime
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
            # H-35: the rain check calls MET.no's nowcast API directly.
            # The old nowcast_sensor read the weather entity's `forecast`
            # attribute, which HA removed in 2024 -- dead ever since.
            if self.args.get("nowcast_sensor"):
                self.log("nowcast_sensor is deprecated (H-35): the rain "
                         "check now calls MET.no nowcast directly; remove "
                         "the key from the config", level="WARNING")
            self.nowcast_enabled = bool(self.args.get("nowcast", True))
            self.nowcast_lat = self.args.get("latitude")
            self.nowcast_lon = self.args.get("longitude")
            # MET.no TOS: an identifying User-Agent or requests get
            # throttled/banned. Overridable for a different contact string.
            self.nowcast_user_agent = self.args.get(
                "nowcast_user_agent",
                "appdaemon-open-window/1.0 (+https://louie.se)")

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
            for key in ("latitude", "longitude"):
                v = self.args.get(key)
                if v is not None and not isinstance(v, (int, float)):
                    raise ValueError(f"{key} must be a number if provided")
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
            # valid_until honours MET.no's Expires header with a 300s floor
            # (nowcast updates every 5 minutes; hammering is a ban risk).
            self._precipitation_cache = {"result": False, "valid_until": 0.0}

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



    def _resolve_coords(self):
        """House coordinates: explicit args win, else HA's own config via
        the plugin (so no coordinates need to live in any tracked file)."""
        if self.nowcast_lat is not None and self.nowcast_lon is not None:
            return float(self.nowcast_lat), float(self.nowcast_lon)
        try:
            cfg = self.get_plugin_config() or {}
            return float(cfg["latitude"]), float(cfg["longitude"])
        except Exception as e:
            self.log(f"Could not resolve coordinates for nowcast: {e}",
                     level="WARNING")
            return None, None

    @staticmethod
    def _parse_nowcast(data, now_aware, horizon_seconds=1800):
        """True/False/None from a nowcast payload: rain in the horizon,
        no rain, or unknown (area not radar-covered / shape unexpected).

        Pure so the response shape is pinned by tests (H-35 DoD).
        """
        try:
            timeseries = data["properties"]["timeseries"]
        except (KeyError, TypeError):
            return None
        saw_rate = False
        for entry in timeseries:
            try:
                t = datetime.fromisoformat(
                    entry["time"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            delta = (t - now_aware).total_seconds()
            if not (-300 <= delta <= horizon_seconds):
                continue
            details = (entry.get("data", {}).get("instant", {})
                       .get("details", {}))
            rate = details.get("precipitation_rate")
            if rate is None:
                # precipitation_rate absent = area outside radar coverage
                continue
            saw_rate = True
            if float(rate) > 0:
                return True
        return False if saw_rate else None

    def _fetch_nowcast(self, lat, lon):
        """One nowcast GET. Returns (payload|None, expires_epoch|None)."""
        url = (f"https://api.met.no/weatherapi/nowcast/2.0/complete"
               f"?lat={lat:.4f}&lon={lon:.4f}")
        req = urllib.request.Request(
            url, headers={"User-Agent": self.nowcast_user_agent})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 203:
                    self.log("MET.no nowcast returned 203: the endpoint "
                             "version is deprecated -- check for 2.x "
                             "successor", level="WARNING")
                expires_epoch = None
                expires = resp.headers.get("Expires")
                if expires:
                    try:
                        expires_epoch = parsedate_to_datetime(
                            expires).timestamp()
                    except (TypeError, ValueError):
                        pass
                return json.load(resp), expires_epoch
        except Exception as e:
            self.log(f"MET.no nowcast fetch failed: {e}", level="WARNING")
            return None, None

    def _precipitation_expected(self) -> bool:
        """True if the MET.no nowcast shows rain within 30 minutes.

        Unknown (fetch failed, area uncovered, shape change) fails toward
        the old behaviour: no rain claim, window notification proceeds.
        Results cache until MET.no's Expires or 300s, whichever is later
        -- failures cache too, so an outage cannot turn into hammering.
        """
        if not self.nowcast_enabled:
            return False
        now = time.time()
        if now < self._precipitation_cache["valid_until"]:
            return self._precipitation_cache["result"]

        lat, lon = self._resolve_coords()
        result = None
        expires_epoch = None
        if lat is not None:
            data, expires_epoch = self._fetch_nowcast(lat, lon)
            if data is not None:
                result = self._parse_nowcast(data, self.get_now())
                if result is None:
                    self.log("Nowcast gave no precipitation_rate for the "
                             "window -- treating as unknown, no rain claim",
                             level="WARNING")
                else:
                    self.log(f"Nowcast precipitation within 30 min: "
                             f"{result}", level="INFO")
        valid_until = max(now + 300, expires_epoch or 0)
        self._precipitation_cache = {"result": bool(result),
                                     "valid_until": valid_until}
        return bool(result)

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
