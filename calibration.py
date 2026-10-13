"""Turns "where the camera sees a person" into "where the eye should look".

The calibration page saves spots: where the camera saw you, plus where you aimed
the eye by hand. From those spots we work out two simple formulas:

    eye left/right = a + b * camera_x + c * person_size
    eye up/down    = d + e * camera_y + f * person_size

person_size (how tall you look on camera) stands in for distance. That's what
lets this correct for the camera being mounted off to the side of the eye:
the correction needed is big for close people and small for far ones.

Eye directions run from -1 to 1 (left/up to right/down), with 1 being as far
as the iris can move.
"""
import json
import os

import numpy as np

# Starting guess before any spots are saved: camera image mirrored, eye looks a bit down.
DEFAULT_X = [1.0, -2.0, 0.0]
DEFAULT_Y = [-0.35, 1.2, 0.5]
# How strongly the answer is pulled toward the starting guess. This keeps things
# sensible with only one or two saved spots; with more spots it barely matters.
STIFFNESS = 0.02


def _fit(columns, targets, guess):
    a = np.column_stack(columns)
    t = np.array(targets)
    g = np.array(guess)
    return np.linalg.solve(a.T @ a + STIFFNESS * np.eye(len(g)), a.T @ t + STIFFNESS * g).tolist()


class Calibration:
    def __init__(self, path):
        self.path = path
        self.points = []
        try:
            with open(path) as f:
                self.points = json.load(f)["points"]
            print(f"Loaded {len(self.points)} calibration spots")
        except FileNotFoundError:
            print("No calibration saved yet, using the starting guess")
        except Exception as e:
            print(f"Couldn't read {path}, starting fresh: {e}")
        self._refit()

    def look(self, x, y, size):
        lx = self.cx[0] + self.cx[1] * x + self.cx[2] * size
        ly = self.cy[0] + self.cy[1] * y + self.cy[2] * size
        return lx, ly

    def add(self, x, y, size, lx, ly):
        self.points.append({"x": round(x, 3), "y": round(y, 3), "size": round(size, 3),
                            "lx": round(lx, 3), "ly": round(ly, 3)})
        self._changed()

    def delete(self, index):
        if 0 <= index < len(self.points):
            del self.points[index]
            self._changed()

    def clear(self):
        self.points = []
        self._changed()

    def _changed(self):
        self._refit()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"points": self.points}, f, indent=1)
        os.replace(tmp, self.path)

    def _refit(self):
        p = self.points
        if not p:
            self.cx, self.cy = list(DEFAULT_X), list(DEFAULT_Y)
            return
        ones = [1] * len(p)
        sizes = [q["size"] for q in p]
        self.cx = _fit([ones, [q["x"] for q in p], sizes], [q["lx"] for q in p], DEFAULT_X)
        self.cy = _fit([ones, [q["y"] for q in p], sizes], [q["ly"] for q in p], DEFAULT_Y)


class IgnoreZones:
    """Spots in the camera view where things that look like people aren't
    (a mailbox, a statue). Added from the calibration page. Positions are
    fractions of the camera picture (0 to 1)."""

    def __init__(self, path):
        self.path = path
        self.zones = []
        try:
            with open(path) as f:
                self.zones = json.load(f)["zones"]
            print(f"Loaded {len(self.zones)} ignored spots")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Couldn't read {path}, starting with none: {e}")

    def contains(self, x, y):
        return any(z[0] <= x <= z[2] and z[1] <= y <= z[3] for z in self.zones)

    def add(self, x1, y1, x2, y2, pad=0.25):
        # Make the spot a bit bigger than the box, since detections jiggle around.
        px, py = (x2 - x1) * pad, (y2 - y1) * pad
        self.zones.append([round(max(0, x1 - px), 3), round(max(0, y1 - py), 3),
                           round(min(1, x2 + px), 3), round(min(1, y2 + py), 3)])
        self._save()

    def clear(self):
        self.zones = []
        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"zones": self.zones}, f, indent=1)
        os.replace(tmp, self.path)
