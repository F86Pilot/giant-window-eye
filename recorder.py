"""Giant Eye recordings: saves a video clip each time someone walks by.

Runs inside tracker.py. The tracker's camera connection also writes the camera's own video,
unchanged, into short pieces in a "ring" folder (the last minute or so). When a visit ends,
the pieces covering it (from a few seconds before the person appeared to a few seconds after
they left) are joined into one clip, with the eye's spoken line added as sound.

Clips go to a folder per day, each with a thumbnail and a small .json file (time, length,
the line, and whether the person stopped to look). Where: the folder named in
recordings_folder.txt (one line; read before each clip, so edits apply without a restart),
or the "recordings" folder next to this file if there's no recordings_folder.txt.
Watch them on the controls page (/calibrate), Recordings tab.
"""
import asyncio
import datetime
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER_FILE = os.path.join(HERE, "recordings_folder.txt")
DEFAULT_FOLDER = os.path.join(HERE, "recordings")


def record_dir():
    """Where clips go (see the top of this file)."""
    try:
        folder = open(FOLDER_FILE).read().strip()
    except OSError:
        folder = ""
    return os.path.expanduser(folder) if folder else DEFAULT_FOLDER

PIECE_SECONDS = 2        # the ring's pieces of video
KEEP_RING = 90           # seconds of ring kept when nobody is around
BEFORE, AFTER = 5, 3     # seconds of video kept before someone appears and after they leave
GONE_AFTER = 2.5         # seconds without seeing them before a visit counts as over
MAX_VISIT = 180          # longest clip, in seconds
STOP_SECONDS = 3         # standing still this long = "stopped to look"
MERGE_GAP = 3            # a "new" person this soon after one vanished, near the same spot, is the same visit
                         # (the tracker sometimes loses someone for a moment and gives them a new number)

# sound = the camera's microphone. zones = shapes (lists of [x, y] corners, 0-1 in the camera picture):
# a visit is only saved if the person's feet were inside one at some point. No zones = save every visit.
# minSeconds = time they must spend inside a recording area; mustMove = skip "people" whose feet never
# moved by at least their own height (a mailbox or bush mistaken for a person).
DEFAULTS = {"enabled": True, "sound": False, "keepDays": 30, "maxGB": 20, "zones": [],
            "minSeconds": 2, "mustMove": True}


def inside(x, y, shape):
    hit = False
    for (x1, y1), (x2, y2) in zip(shape, shape[1:] + shape[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def volume_of(path):
    """/Volumes/<name> for a path on an external drive, else None."""
    parts = os.path.abspath(path).split(os.sep)
    return os.sep.join(parts[:3]) if len(parts) > 2 and parts[1] == "Volumes" else None


class Recorder:
    """read_person() -> the tracker's state (present, id, box); send(msg) -> async, to the controls pages."""

    def __init__(self, data_dir, read_person, send):
        self.read_person, self.send = read_person, send
        self.ring = os.path.join(data_dir, "ring")
        self.settings_path = os.path.join(data_dir, "recordings.json")
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update({k: v for k, v in json.load(open(self.settings_path)).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        os.makedirs(self.ring, exist_ok=True)
        for f in glob.glob(os.path.join(self.ring, "*.ts")):
            os.remove(f)   # old pieces from before a restart
        self.visit = None
        self.ended = None        # the visit that just ended (it may turn out to continue, see MERGE_GAP)
        self.saving = []         # visits that ended but whose clip isn't made yet (keep their pieces)
        self.lines = []          # (time played, audio bytes, content type, text) from the voice
        self.problem = None      # why recording isn't working, shown on the page

    def ffmpeg_output(self):
        """Extra ffmpeg output for the camera reader: the camera's own video (copied, no extra work)
        in short pieces, plus its microphone if "sound" is on. None when recording is off."""
        if not self.settings["enabled"]:
            return None
        audio = ["-map", "0:a?", "-c:a", "aac", "-b:a", "48k"] if self.settings["sound"] else []
        return (["-map", "0:v", "-c:v", "copy"] + audio +
                ["-f", "segment", "-segment_time", str(PIECE_SECONDS), "-reset_timestamps", "1",
                 "-strftime", "1", os.path.join(self.ring, "%s.ts")])

    # ---- Settings ----
    def set(self, key, value):
        if key in ("enabled", "sound", "mustMove"):
            value = bool(value)
        elif key == "zones":
            try:
                value = [[[round(min(1, max(0, float(x))), 4), round(min(1, max(0, float(y))), 4)] for x, y in shape]
                         for shape in value]
            except (TypeError, ValueError):
                return False
            if len(value) > 20 or any(not 3 <= len(shape) <= 40 for shape in value):
                return False
        elif key in ("keepDays", "maxGB", "minSeconds"):
            try:
                lo, hi = {"keepDays": (1, 365), "maxGB": (1, 200), "minSeconds": (0, 60)}[key]
                value = max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                return False
        else:
            return False
        self.settings[key] = value
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=1)
        os.replace(tmp, self.settings_path)
        return True

    def can_write(self):
        """The SSD must be plugged in (never write into an empty /Volumes folder on the Mac's own disk)."""
        vol = volume_of(record_dir())
        if vol and not os.path.ismount(vol):
            self.problem = f"The drive {os.path.basename(vol)} isn't plugged in"
            return False
        try:
            os.makedirs(record_dir(), exist_ok=True)
            test = os.path.join(record_dir(), ".write-test")
            open(test, "w").close()
            os.remove(test)
        except OSError as e:
            self.problem = f"Can't save to {record_dir()}: {e.strerror}"
            return False
        self.problem = None
        return True

    # ---- Hooks ----
    def line_played(self, audio, kind, text):
        """Called by the voice when a line plays."""
        self.lines.append((time.time(), audio, kind, text))
        del self.lines[:-40]   # enough for a long visit with "still here" lines every 15 s

    # ---- Watching visits ----
    async def run(self):
        print(f"Recordings: saving to {record_dir()}" if self.can_write() else f"Recordings: {self.problem}")
        last_clean = 0
        while True:
            await asyncio.sleep(0.2)
            try:
                if not self.settings["enabled"]:
                    self.visit = None
                    continue
                now = time.time()
                p = self.read_person()
                v = self.visit
                if p["present"] and p["box"] and (v is None or p["id"] == v["id"]):
                    if v is None:
                        v = self.visit = self.continued(p, now) or {
                            "id": p["id"], "start": now, "seen": now, "track": [], "still": 0.0, "zoneTime": 0.0}
                    step = now - v["seen"]
                    v["seen"] = now
                    x1, y1, x2, y2 = p["box"]
                    v["track"].append((now, (x1 + x2) / 2, y2, y2 - y1))
                    zones = self.settings["zones"]
                    if not zones or any(inside((x1 + x2) / 2, y2, z) for z in zones):
                        v["zoneTime"] += min(step, 1.0)
                    v["still"] = max(v["still"], self.still_for(v["track"]))
                if v and (now - v["seen"] > GONE_AFTER or now - v["start"] > MAX_VISIT
                          or (p["present"] and p["id"] != v["id"])):
                    self.visit = None
                    v["maxed"] = now - v["start"] > MAX_VISIT
                    self.ended = v
                    if self.worth_saving(v):
                        v["task"] = asyncio.create_task(self.save_later(v))
                if now - last_clean > 5:
                    last_clean = now
                    self.clean_ring()
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(2)

    def continued(self, p, now):
        """If this "new" person is really the one who just vanished (soon after, near the same spot),
        pick their visit back up instead of starting a second clip."""
        old = self.ended
        if not old or old.get("maxed") or now - old["seen"] > MERGE_GAP or not old["track"]:
            return None
        _, x, y, h = old["track"][-1]
        x1, y1, x2, y2 = p["box"]
        if ((x1 + x2) / 2 - x) ** 2 + (y2 - y) ** 2 > (1.5 * h) ** 2:
            return None
        if old.get("task"):
            old["task"].cancel()   # its clip isn't made yet: it becomes part of this one
            old.pop("task")
        self.ended = None
        old["id"] = p["id"]
        print(f"Recordings: person #{p['id']} is the same visit, continuing the clip")
        return old

    def worth_saving(self, v):
        """Long enough in a recording area, and (if mustMove) actually moved."""
        if v["zoneTime"] < self.settings["minSeconds"]:
            return False
        if self.settings["mustMove"]:
            t0, x0, y0, _ = v["track"][0]
            height = sorted(h for _, _, _, h in v["track"])[len(v["track"]) // 2]   # their typical height
            moved = max(((x - x0) ** 2 + (y - y0) ** 2) ** 0.5 for _, x, y, _ in v["track"])
            if moved < height:
                return False
        return True

    @staticmethod
    def still_for(track):
        """How long the person has stood (nearly) still up to now: their feet stayed within half
        their own height, which works for people both near and far."""
        t_end, x_end, y_end, h_end = track[-1]
        for t, x, y, h in reversed(track):
            if abs(x - x_end) > h_end * 0.5 or abs(y - y_end) > h_end * 0.5:
                return t_end - t
        return t_end - track[0][0]

    def clean_ring(self):
        keep_from = time.time() - KEEP_RING
        for v in [self.visit] + self.saving:
            if v:
                keep_from = min(keep_from, v["start"] - BEFORE - PIECE_SECONDS)
        for f in glob.glob(os.path.join(self.ring, "*.ts")):
            if os.path.getmtime(f) < keep_from:
                os.remove(f)

    # ---- Making the clip ----
    async def save_later(self, v):
        self.saving.append(v)
        await asyncio.sleep(AFTER + PIECE_SECONDS)   # let the last pieces finish writing
        try:
            clip = await asyncio.to_thread(self.save, v)
            if clip:
                print(f"Recordings: saved {clip['file']}" + (" (stopped to look)" if clip["stopped"] else ""))
                await self.send({"type": "rec_clip", "clip": clip, "usage": self.usage()})
        except asyncio.CancelledError:
            pass   # the visit continued (see continued)
        except Exception:
            traceback.print_exc()
        finally:
            if v in self.saving:
                self.saving.remove(v)

    def save(self, v):
        if not self.can_write():
            return None
        start, end = v["start"] - BEFORE, v["seen"] + AFTER
        pieces = []
        for f in sorted(glob.glob(os.path.join(self.ring, "*.ts")), key=lambda f: int(os.path.basename(f)[:-3])):
            p_start, p_end = int(os.path.basename(f)[:-3]), os.path.getmtime(f)
            if p_end >= start and p_start <= end:
                pieces.append((p_start, f))
        if not pieces:
            return None
        clip_start = pieces[0][0]
        when = datetime.datetime.fromtimestamp(v["start"])
        day = os.path.join(record_dir(), when.strftime("%Y-%m-%d"))
        os.makedirs(day, exist_ok=True)
        stopped = v["still"] >= STOP_SECONDS
        name = when.strftime("%H-%M-%S") + ("_stopped" if stopped else "")
        out = os.path.join(day, name + ".mp4")

        with tempfile.TemporaryDirectory() as tmp:
            joined = os.path.join(tmp, "joined.ts")
            with open(joined, "wb") as j:
                for _, f in pieces:
                    with open(f, "rb") as piece:
                        shutil.copyfileobj(piece, j)
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", joined]
            mic = self.has_audio(joined)
            # Every line said during the visit (people who stay get "still here" lines), each where it played.
            lines = [x for x in self.lines if v["start"] - 1 <= x[0] <= v["seen"] + AFTER]
            if lines:
                parts = []
                for i, line in enumerate(lines, 1):
                    sound = os.path.join(tmp, f"line{i}" + (".mp3" if "mpeg" in line[2] else ".wav"))
                    open(sound, "wb").write(line[1])
                    cmd += ["-i", sound]
                    parts.append(f"[{i}:a]adelay={max(0, int((line[0] - clip_start) * 1000))}:all=1[eye{i}]")
                ins = ("[0:a]" if mic else "") + "".join(f"[eye{i}]" for i in range(1, len(lines) + 1))
                n = len(lines) + mic
                mix = ";".join(parts) + ";" + (f"{ins}amix=inputs={n}:duration={'first' if mic else 'longest'}:normalize=0[out]"
                                               if n > 1 else f"{ins}anull[out]")
                cmd += ["-filter_complex", mix, "-map", "0:v", "-map", "[out]", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "96k"]
            elif mic:
                cmd += ["-map", "0:v", "-map", "0:a", "-c:v", "copy", "-c:a", "copy"]
            else:
                cmd += ["-map", "0:v", "-c:v", "copy"]
            cmd += ["-movflags", "+faststart", out]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode != 0:
                print(f"Recordings: couldn't make clip: {r.stderr.decode(errors='replace')[:300]}")
                return None
        length = max(0.0, (pieces[-1][0] + PIECE_SECONDS) - clip_start)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(min(BEFORE + 1, length / 2)), "-i", out,
                        "-frames:v", "1", "-vf", "scale=320:-2", os.path.join(day, name + ".jpg")],
                       capture_output=True, timeout=30)
        clip = {"file": f"{os.path.basename(day)}/{name}.mp4", "time": when.strftime("%a %b %-d, %-I:%M:%S %p"),
                "start": v["start"], "seconds": round(length), "stopped": stopped,
                "stoodStill": round(v["still"], 1), "line": lines[0][3] if lines else None,
                "lines": [x[3] for x in lines], "sound": mic}
        with open(os.path.join(day, name + ".json"), "w") as f:
            json.dump(clip, f, indent=1)
        self.tidy()
        return clip

    @staticmethod
    def has_audio(path):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15)
        return bool(r.stdout.strip())

    # ---- Clips on the drive ----
    def clips(self, limit=200):
        if not os.path.isdir(record_dir()):
            return []
        found = []
        for meta in glob.glob(os.path.join(record_dir(), "*", "*.json")):
            try:
                found.append(json.load(open(meta)))
            except (OSError, ValueError):
                pass
        found.sort(key=lambda c: c.get("start", 0), reverse=True)
        return found[:limit]

    def usage(self):
        total = 0
        for f in glob.glob(os.path.join(record_dir(), "*", "*")):
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        return {"gb": round(total / 1e9, 2), "maxGB": self.settings["maxGB"], "problem": self.problem,
                "folder": record_dir()}

    def delete(self, file):
        """Delete one clip (its video, thumbnail and .json). file is like '2026-10-13/14-05-12.mp4'."""
        day, _, name = file.partition("/")
        if not day or "/" in name or ".." in file or not name.endswith(".mp4"):
            return False
        base = os.path.join(record_dir(), day, name[:-4])
        for ext in (".mp4", ".jpg", ".json"):
            try:
                os.remove(base + ext)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(os.path.join(record_dir(), day))   # only if the day is now empty
        except OSError:
            pass
        return True

    def tidy(self):
        """Delete clips older than keepDays, then the oldest until under maxGB."""
        clips = sorted(self.clips(limit=100000), key=lambda c: c.get("start", 0))
        cutoff = time.time() - self.settings["keepDays"] * 86400
        for c in [c for c in clips if c.get("start", 0) < cutoff]:
            self.delete(c["file"])
            clips.remove(c)
        while clips and self.usage()["gb"] > self.settings["maxGB"]:
            self.delete(clips.pop(0)["file"])

    # ---- The phone page ----
    def hello(self):
        self.can_write()
        return {"type": "rec_hello", "settings": self.settings, "clips": self.clips(), "usage": self.usage()}

    async def handle(self, msg):
        kind = msg.get("type")
        if kind == "rec_setting" and self.set(msg.get("key"), msg.get("value")):
            await self.send({"type": "rec_settings", "settings": self.settings,
                             "note": "Takes effect after the camera reconnects (within a few seconds)."
                             if msg.get("key") in ("enabled", "sound") else None})
            if msg.get("key") in ("enabled", "sound") and self.on_toggle:
                self.on_toggle()
        elif kind == "rec_delete" and self.delete(str(msg.get("file", ""))):
            await self.send({"type": "rec_deleted", "file": msg["file"], "usage": self.usage()})

    on_toggle = None   # set by the tracker: restarts the camera connection

    async def file_handler(self, request):
        """Serves a clip or thumbnail from the drive (supports seeking, which phones need for video)."""
        from aiohttp import web
        day, name = request.match_info["day"], request.match_info["name"]
        if ".." in day or ".." in name or "/" in name or not name.endswith((".mp4", ".jpg")):
            raise web.HTTPNotFound()
        path = os.path.join(record_dir(), day, name)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)
