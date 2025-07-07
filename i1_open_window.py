import pytz
from datetime import datetime, timedelta
import time
import json
import appdaemon.plugins.hass.hassapi as hass

DEFAULT_LAMP_TIMEOUT = 300
timezone = pytz.timezone('Europe/Stockholm')

"""
vind_temperatur_notis:
  module: main
  class: ValueNotification
  persons:
    - anders:
      notify: notify.mobile_app_pixel_9_pro
      tracker: device_tracker.pixel_9_pro
  presence:
    - device_tracker.pixel_9_pro
  temperature:
    sensor: sensor.verisure_vindsvaning_temperature
    below: 18
    above: 20
  window:
    sensor: binary_sensor.balkongdorr_sovrum
    below: on
    above: off
  messages:
    below: "Stäng balkongdörren i sovrummet"
    above: "Öppna balkingdörren i sovrummet"
    title: "Sovrumtemperat"
  when:
    after: 16
    before: 21
"""

class ValueNotification(hass.Hass):
  def initialize(self):
    self.log("Loading ValueNotification()")

    self.temperature = self.args.get("temperature")
    self.window = self.args.get("window")
    self.messages = self.args.get("messages")
    self.when = self.args.get("when")
    self.persons = self.args.get("persons", [])
    self.msg_cooldown = {}

    self.run_every(self.check_temperature, "now",  1 * 60)
    #self.listen_state(self.temperature_change, self.temperature.get("sensor"))
    self.listen_event(self.phone_action, event="mobile_app_notification_action")

    # trigger on first start
#    self.temperature_change(self.temperature.get("sensor"), {}, None, self.get_state(self.temperature.get("sensor")), {}) 


  def check_temperature(self, kwargs):
    state = self.get_state(self.temperature.get("sensor"))
    if state == "unavailable" or state == "unknown" or state is None:
      return

    temperature = float(state)

    window = self.get_state(self.window.get("sensor")) == "on"

    self.log("temperature: {}, {}-{} window open: {}".format(temperature, self.temperature.get("below"), self.temperature.get("above"), window), level="DEBUG")

    now = datetime.now()
    hour = now.hour
    if (hour < self.when.get("after") or hour >= self.when.get("before")):
      self.log("Hour out of bounds: {} < {} || {} >= {}".format(hour, self.when.get("after"), hour, self.when.get("before")), level="DEBUG")
      return

    if (temperature >= self.temperature.get("above") and window == self.window.get("above")):
      self.log("ALERT: {}".format(self.messages.get("above")), level="DEBUG")
      self.notify(self.messages.get("title"), "{} ({}°C)".format(self.messages.get("above"), temperature))
      return

    if (temperature < self.temperature.get("below") and window == self.window.get("below")):
      self.log("ALERT: {}".format(self.messages.get("below")), level="DEBUG")
      self.notify(self.messages.get("title"), "{} ({}°C)".format(self.messages.get("below"), temperature))
      return
    

  # notify anyone home
  def notify(self, title, message):
    for person in self.persons:
      if time.time() - self.msg_cooldown.get(person.get("notify"), 0) < int(self.messages.get("cooldown")):
        self.log("cooldown activated for {}, last msg sent {}s ago".format(person.get("notify"), time.time() - self.msg_cooldown.get(person.get("notify"), 0)), level="DEBUG")
      elif person.get("tracker") is not None and self.get_state(person.get("tracker")) == "home":
        self.call_service("notify/{}".format(person.get("notify")), message=message, data={"actions":[{"action": "{}.{}.{}".format(self.name, "ignore", person.get("notify")), "title":"Ignorera idag"}]})
        self.msg_cooldown[person.get("notify")] = time.time()
        self.log("notify/{}".format(person.get("notify")), level="DEBUG")

  # handle notification action
  """
{
    "event_type": "mobile_app_notification_action",
    "data": {
        "action_1_key": "ignore",
        "action_1_title": "Ignorera idag",
        "action_1_uri": "null",
        "message": "Öppna balkingdörren i sovrummet",
        "action": "ignore",
        "device_id": "41e243a31ab95a55"
    },
    "origin": "REMOTE",
    "time_fired": "2022-05-03T14:15:35.176865+00:00",
    "context": {
        "id": "b5df1d663d5dc1b9f1e69f4ef1e20102",
        "parent_id": null,
        "user_id": "80c63e949b2a4d9ea0a47f36d4232529"
    }
}
  """
  def phone_action(self, event_name, data, kwargs):
    action = str(data.get("action")).split(".")
    if action[0] != self.name:
      return


    if action[1] == "ignore":
      dt_now = datetime.now(timezone)
      tomorrow_start = datetime(dt_now.year, dt_now.month, dt_now.day, tzinfo=timezone) + timedelta(1)
      self.msg_cooldown[action[2]] = tomorrow_start.timestamp()
      self.log("IGNORE {} until tomorrow {}".format(action[2], self.msg_cooldown), level="DEBUG")
      
            

