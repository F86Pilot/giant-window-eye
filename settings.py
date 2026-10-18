"""Eye settings that can be changed from the phone page.

Defaults come from web/config.js. Anything changed on the phone is saved
here (data/settings.json) and overrides config.js. Every open eye page gets
changes immediately.

SCHEMA is the list of settings the phone page shows, in order. Each one is
checked against its min/max here, so a bad value can never reach the eye.
"""
import json
import os

# kind "range": a slider. kind "toggle": an on/off switch.
SCHEMA = [
    # Position & size
    {"key": "showGuide", "group": "Position & size", "label": "Show alignment guide", "kind": "toggle",
     "help": "Draws a ring around the eyeball's edge and a cross at its center, to line it up with the window. Turn off when done."},
    {"key": "eyeShiftX", "group": "Position & size", "label": "Move left / right", "kind": "range",
     "min": -0.6, "max": 0.6, "step": 0.005, "help": "Negative = left, positive = right."},
    {"key": "eyeShiftY", "group": "Position & size", "label": "Move up / down", "kind": "range",
     "min": -0.3, "max": 0.3, "step": 0.005, "help": "Negative = up, positive = down."},
    {"key": "eyeScale", "group": "Position & size", "label": "Eye size", "kind": "range",
     "min": 0.4, "max": 1.3, "step": 0.01, "help": "1 = eyeball touches the top and bottom of the screen."},

    # Look
    {"key": "sauron", "group": "Look", "label": "Eye of Sauron", "kind": "toggle",
     "help": "Swaps the eyeball for a lidless eye wreathed in flame, with a slit pupil. "
             "Turn off to go back to the normal eye. The color, white-of-the-eye and vein settings only change the normal eye."},
    {"key": "irisSize", "group": "Look", "label": "Iris size", "kind": "range", "min": 0.25, "max": 0.6, "step": 0.01},
    {"key": "pupilSize", "group": "Look", "label": "Pupil size", "kind": "range", "min": 0.2, "max": 0.7, "step": 0.01},
    {"key": "scleraBrightness", "group": "Look", "label": "White of the eye brightness", "kind": "range",
     "min": 0, "max": 1, "step": 0.01, "help": "Lower = darker, more mysterious; the glowing iris stands out more."},
    {"key": "veins", "group": "Look", "label": "Red veins", "kind": "toggle"},

    # Color
    {"key": "hueDrift", "group": "Color", "label": "Slowly change color", "kind": "toggle"},
    {"key": "hueFrom", "group": "Color", "label": "Color range: from", "kind": "range", "min": 0, "max": 360, "step": 1,
     "help": "0 red, 22 orange, 45 gold, 120 green, 200 blue, 280 purple, 360 red."},
    {"key": "hueTo", "group": "Color", "label": "Color range: to", "kind": "range", "min": 0, "max": 360, "step": 1},
    {"key": "hueCycleMinutes", "group": "Color", "label": "Minutes per color cycle", "kind": "range",
     "min": 1, "max": 60, "step": 1, "help": "Time to go from one end of the range to the other and back."},
    {"key": "irisHue", "group": "Color", "label": "Fixed color (when not changing)", "kind": "range",
     "min": 0, "max": 360, "step": 1},

    # Movement
    {"key": "maxTravel", "group": "Movement", "label": "How far the iris can move", "kind": "range",
     "min": 0.2, "max": 0.6, "step": 0.01},
    {"key": "smoothing", "group": "Movement", "label": "Follow speed", "kind": "range", "min": 1, "max": 15, "step": 0.5,
     "help": "Higher = snappier when following a person."},
    {"key": "idleAfterSec", "group": "Movement", "label": "Seconds before looking around", "kind": "range",
     "min": 0.5, "max": 10, "step": 0.5, "help": "After nobody is seen this long, it starts glancing around."},
    {"key": "idleUpMost", "group": "Movement", "label": "Idle: how far up it glances", "kind": "range",
     "min": 0, "max": 0.8, "step": 0.01},
    {"key": "idleDownMost", "group": "Movement", "label": "Idle: how far down it glances", "kind": "range",
     "min": 0, "max": 0.6, "step": 0.01},

    # Blinking
    {"key": "blinkMinSec", "group": "Blinking", "label": "Shortest time between blinks", "kind": "range",
     "min": 0.5, "max": 15, "step": 0.5},
    {"key": "blinkMaxSec", "group": "Blinking", "label": "Longest time between blinks", "kind": "range",
     "min": 1, "max": 30, "step": 0.5},

    # Moods
    {"key": "moods", "group": "Moods", "label": "Moods on", "kind": "toggle",
     "help": "Eye rolls and double-takes."},
    {"key": "eyeRollChance", "group": "Moods", "label": "Eye roll chance", "kind": "range",
     "min": 0, "max": 0.5, "step": 0.01, "help": "Chance at each idle glance. 0.04 = roughly once a minute or two."},
    {"key": "doubleTakeChance", "group": "Moods", "label": "Double-take chance", "kind": "range",
     "min": 0, "max": 1, "step": 0.05, "help": "Chance when someone new shows up."},

    # Talking (when the voice says a line)
    {"key": "talkAnim", "group": "Talking", "label": "Move while talking", "kind": "toggle",
     "help": "The pupil and iris pulse with the words. Cues add reactions: whispers narrow the eye, "
             "laughs make it jiggle, sarcastic lines end in an eye roll."},
    {"key": "talkDelay", "group": "Talking", "label": "Sync delay (seconds)", "kind": "range",
     "min": 0, "max": 1.5, "step": 0.05,
     "help": "If the eye moves before you hear the words, raise this; if after, lower it. "
             "Use \"Test yard speaker\" on the Voice tab to check."},
    {"key": "talkPulse", "group": "Talking", "label": "Pulse strength", "kind": "range",
     "min": 0, "max": 1, "step": 0.05,
     "help": "How much the pupil (black center) and iris grow with each syllable."},
    {"key": "talkJiggle", "group": "Talking", "label": "Laugh jiggle", "kind": "range",
     "min": 0, "max": 1, "step": 0.05,
     "help": "How much the eye shakes up and down on lines with a laugh or cackle. 0 = no shaking."},

    # Detection (used by tracker.py, not the eye page)
    {"key": "detectConfidence", "group": "Detection", "label": "Detection sensitivity", "kind": "range",
     "min": 0.15, "max": 0.7, "step": 0.01,
     "help": "How sure the camera must be that something is a person before the eye starts following it. "
             "Lower = notices people sooner and in the dark, but may follow bushes or shadows "
             "(use \"Ignore what's in the yellow box\" on the Aim tab for those). Default 0.35."},
]
BY_KEY = {s["key"]: s for s in SCHEMA}


class Settings:
    def __init__(self, path):
        self.path = path
        self.values = {}
        try:
            with open(path) as f:
                self.values = {k: v for k, v in json.load(f).items() if k in BY_KEY}
            print(f"Loaded {len(self.values)} saved settings")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Couldn't read {path}, using config.js defaults: {e}")

    def set(self, key, value):
        """Save one setting. value None = back to the config.js default. Returns False if not allowed."""
        spec = BY_KEY.get(key)
        if spec is None:
            return False
        if value is None:
            self.values.pop(key, None)
        elif spec["kind"] == "toggle":
            self.values[key] = bool(value)
        else:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False
            self.values[key] = round(min(spec["max"], max(spec["min"], v)), 4)
        self._save()
        return True

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.values, f, indent=1)
        os.replace(tmp, self.path)
