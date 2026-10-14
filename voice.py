"""Giant Eye voice: says a funny line to people walking by.

Runs inside tracker.py and uses its camera pictures. Lines play on any phone that has
the controls page (/calibrate) open with "Turn on sound" tapped (Voice tab).

How a line happens:
  1. The tracker starts following someone new.
  2. We wait until their feet are out of every "no-photo" zone (drawn on the Voice tab,
     e.g. the far edges where people are too small for a good joke), then crop them from
     the camera picture.
  3. The cropped photo goes to Claude, which writes a one-liner. (No key, an error, or
     the daily limit reached -> a free canned line.)
  4. Piper speaks it, on this computer.
  5. It plays once their feet are in a "talk" zone (e.g. by the speaker in the flowerbed),
     or right away if they already walked through one while the line was being made.
"""
import asyncio
import base64
import datetime
import io
import json
import os
import random
import re
import time
import traceback
import wave

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "anthropic_key.txt")
ELEVEN_KEY_FILE = os.path.join(HERE, "elevenlabs_key.txt")
VOICES_DIR = os.path.join(HERE, "voice", "voices")   # Piper voices (the free backup)

# ElevenLabs: expressive voices that follow delivery cues like [whispers] or [cackles].
ELEVEN_URL = "https://api.us.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128"
ELEVEN_MODEL, ELEVEN_PRICE = "eleven_v4_turbo", 0.04   # $ per 1,000 characters
PIPER_VOICE = "en_US-ryan-high"   # free backup voice, on this computer

# The characters the eye can be. Each has an ElevenLabs voice, a writing style for Claude,
# and optionally a sound effect (ffmpeg filter, done on this computer: free and near-instant).
CHARACTERS = {
    "george": {
        "name": "George (spooky)",
        "voice": "JBFqnCBsd6RMkjVDRZzb",   # ElevenLabs built-in voice "George"
        "style": ("Style: eerie and slow, like the watchful spirit of a haunted house who finds the living amusing.\n"
                  "Spooky-fun, never truly frightening: kids walk by too, so no threats, no harm, no gore.\n"
                  "Ominous pauses, whispers and the odd unsettling laugh suit you."),
        "effect": "asetrate=44100*0.88,aresample=44100,atempo=1.05,chorus=0.6:0.9:55|70:0.4|0.32:0.25|0.4:2|1.3",
        "preview": "[whispers] I've been watching you... for a very... long... time. [laughs]",
    },
    "witch": {
        "name": "Witch",
        "voice": "sssn4wp3AspuK2kvy3Ym",   # from ElevenLabs' Voice Library (needs credits on the account)
        "style": ("Style: a cackling, mischievous old witch who lives in the house and watches from the window.\n"
                  "Call people \"dearie\" now and then; potions, cauldrons, brooms, black cats and silly spells are\n"
                  "your world. Wicked-fun, never truly frightening: kids walk by too, so no threats and no real curses.\n"
                  "Start the line with [raspy old witch voice]; a [cackles] fits well."),
        "effect": None,
        "preview": "[raspy old witch voice] Cutting across my lawn, dearie? [cackles] My broom does that too!",
    },
}
SETTLE = 0.15             # seconds after someone appears before taking their photo (lets the box settle)

DEFAULTS = {
    "enabled": True,
    "cooldown": 15,             # seconds between lines
    "dailyCap": 150,            # Claude lines per day; after that, canned lines (free)
    "quietFrom": 22, "quietTo": 7,   # no talking between these hours (same number = never quiet)
    "model": "claude-opus-5-5",
    "character": "george",      # see CHARACTERS
    "talkZones": [],            # shapes (lists of [x, y], 0-1 in the camera picture): play when feet are inside
    "noPhotoZones": [],         # shapes: don't take the photo while feet are inside
    "zoneWait": 20,             # seconds to wait for a photo, and then for them to reach a talk zone
    "lingerSeconds": 20,        # someone still here this long after their line gets one more (once). 0 = off
    "mustMove": True,           # only talk to "people" whose feet moved (a quarter of their height): skips static objects
}
MODELS = {
    # Thinking can't be turned off on Opus 5.5, so keep effort low. If a line is ever
    # refused, Anthropic's server retries it on a backup model ("fallbacks").
    "claude-opus-5-5": dict(output_config={"effort": "low"},
                            betas=["server-side-fallback-2026-07-01"], fallbacks="default"),
    # Sonnet 5.5: about 3.5 s faster than Opus per line (measured), just as funny in testing.
    "claude-sonnet-5-5": dict(output_config={"effort": "low"},
                              betas=["server-side-fallback-2026-07-01"], fallbacks="default"),
    "claude-haiku-5-5": dict(output_config={"effort": "low"}, thinking={"type": "disabled"}),
}
PRICE = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
         "claude-haiku-5-5": (0.10, 0.50)}   # $ per million tokens in/out
SKIP = "SKIP"   # what Claude answers when there's nobody to talk to

PROMPT = """You are the voice of a giant glowing eyeball displayed in the upstairs window of a house.
You watch the sidewalk and say ONE short line out loud to people walking by.

STYLE
Eye puns are welcome but not every time. Under 15 words. Plain spoken text only:
no emojis, hashtags, quotes or stage directions, because it is read aloud by text-to-speech.

You get a small, often blurry security-camera crop of the person.
Rules:
- Riff on what you can clearly see: what they're doing or carrying, a dog, their outfit or
  costume, the weather, the time, the date (holidays!).
- Never count people or mention how many have walked by (no "18th visitor today").
- Outfit lines are teasing-but-flattering, never mocking.
- Never comment on bodies, faces, weight, age, race, gender or attractiveness. No insults.
- Don't repeat or closely echo any of your recent lines, including their sentence pattern.
- If there's clearly no person to talk to, reply with exactly SKIP.
TAGS_RULE
Reply with only the line."""
TAGS = """- The voice understands delivery cues in square brackets. Use one or two where they add to the joke,
  e.g. [sarcastic], [whispers], [laughs], [sighs], [curious], [mischievously], [excited], [deadpan].
  Use an ellipsis (...) for a dramatic pause. Cues don't count toward the 15 words.
"""
NO_TAGS = "- No square-bracket cues.\n"
CANNED = [
    "I see you.", "Don't mind me. Just watching.", "Lovely day to be observed.",
    "You walk, I watch. That's our arrangement.", "I never blink first.",
    "Keep walking. I've got my eye on you.", "Nice of you to stop by my sidewalk.",
    "I'm not staring. Okay, I'm staring.", "Another fine specimen.",
    "Have a good one. I'll be watching.", "Hello there, passerby.", "Eyes front. Well, eye.",
]


TALK_STEP = 0.04   # seconds per loudness step sent to the eye (25 a second)


def loudness(audio):
    """How loud the line is every TALK_STEP seconds, 0-1, so the eye can pulse in time with the words."""
    import subprocess
    import numpy as np
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                       input=audio, capture_output=True, timeout=15)
    pcm = np.frombuffer(r.stdout, np.int16).astype(np.float32)
    n = int(8000 * TALK_STEP)
    if len(pcm) < n:
        return []
    rms = np.sqrt((pcm[: len(pcm) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    top = np.percentile(rms, 95) or 1.0
    return [round(float(min(1.0, v / top)), 2) for v in rms]


def cues_in(text):
    """Delivery cues in the line that the eye acts out."""
    t = text.lower()
    found = []
    if "[whisper" in t:
        found.append("whisper")
    if "[laugh" in t or "[cackle" in t or "[giggle" in t:
        found.append("laugh")
    if "[sarcastic" in t or "[deadpan" in t:
        found.append("roll")
    return found


def feet(box):
    """Where the person is standing: bottom middle of their box (0-1 coordinates)."""
    return ((box[0] + box[2]) / 2, box[3]) if box else None


def inside(point, shape):
    """Is the point inside the shape (a list of corners)? Standard ray-crossing test."""
    x, y = point
    hit = False
    for (x1, y1), (x2, y2) in zip(shape, shape[1:] + shape[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def in_any(box, shapes):
    p = feet(box)
    return p is not None and any(inside(p, s) for s in shapes)


def time_of_day(now):
    h = now.hour
    part = ("late night" if h < 5 else "early morning" if h < 9 else "morning" if h < 12 else
            "afternoon" if h < 17 else "evening" if h < 21 else "night")
    return f"It is {now.strftime('%-I:%M %p on %A, %B %-d')} ({part})."


def clean_shapes(value):
    """Check shapes sent from the phone: each needs 3-40 corners inside the picture."""
    if not isinstance(value, list) or len(value) > 20:
        return None
    shapes = []
    for shape in value:
        if not isinstance(shape, list) or not 3 <= len(shape) <= 40:
            return None
        try:
            shapes.append([[round(max(0.0, min(1.0, float(x))), 4), round(max(0.0, min(1.0, float(y))), 4)]
                           for x, y in shape])
        except (TypeError, ValueError):
            return None
    return shapes


class Store:
    """Voice settings and today's count, saved in data/voice.json."""

    def __init__(self, path):
        self.path = path
        self.settings = dict(DEFAULTS)
        self.today = {"date": "", "count": 0, "cost": 0.0}
        try:
            saved = json.load(open(path))
            self.settings.update({k: v for k, v in saved.get("settings", {}).items() if k in DEFAULTS})
            self.today.update(saved.get("today", {}))
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"settings": self.settings, "today": self.today}, f, indent=1)
        os.replace(tmp, self.path)

    def roll_day(self):
        d = datetime.date.today().isoformat()
        if self.today["date"] != d:
            self.today = {"date": d, "count": 0, "cost": 0.0}

    def set(self, key, value):
        if key not in DEFAULTS:
            return False
        if key in ("enabled", "mustMove"):
            value = bool(value)
        elif key == "model":
            if value not in MODELS:
                return False
        elif key == "character":
            if value not in CHARACTERS:
                return False
        elif key in ("talkZones", "noPhotoZones"):
            value = clean_shapes(value)
            if value is None:
                return False
        else:
            limits = {"lingerSeconds": (0, 600), "cooldown": (0, 600), "dailyCap": (0, 2000), "quietFrom": (0, 23), "quietTo": (0, 23),
                      "zoneWait": (3, 120)}
            lo, hi = limits[key]
            try:
                value = max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                return False
        self.settings[key] = value
        self.save()
        return True


class Voice:
    """read_person() -> the tracker's state (present, id, box); read_frame() -> the newest clean
    camera picture (BGR); send(msg) -> async, sends to every open controls page."""

    def __init__(self, data_dir, read_person, read_frame, send):
        self.read_person, self.read_frame, self.send = read_person, read_frame, send
        self.store = Store(os.path.join(data_dir, "voice.json"))
        self.client = None
        if os.path.exists(KEY_FILE):
            import anthropic
            self.client = anthropic.Anthropic(api_key=open(KEY_FILE).read().strip(), timeout=10, max_retries=1)
        else:
            print("Voice: no anthropic_key.txt, so canned lines only")
        self.eleven_key = open(ELEVEN_KEY_FILE).read().strip() if os.path.exists(ELEVEN_KEY_FILE) else None
        self.piper = {}
        self.preview_cache = {}  # character -> (audio, content type), for the Preview button
        self.audio = {}          # line number -> (audio bytes, content type), recent ones only
        self.history = []        # recent lines, shown on the page
        self.busy = False
        self.spoken_to = {}      # person ID -> (when their line played, the line), for "still here?"
        self.followed_up = set() # person IDs that already got their one "still here" line
        self.on_play = None      # called with (audio, content type, text) when a line plays
        self.speakers = {}       # yard speakers (speaker/player.py on a Pi): websocket -> its last status
        self.send_eye = None     # async: sends a message to the eye pages (set by the tracker)
        self.talks = {}          # line number (or "preview") -> {"env", "cues"}, for the eye's talking
        self.talked = set()      # lines the eye already acted out
        self.last_spoken = 0.0
        self.line_number = 0

    # ---- See and write (Claude), speak (Piper) ----
    def crop(self, box):
        """The person's box from the camera picture, padded to show what's around them."""
        f = self.read_frame()
        if f is None:
            return None
        h, w = f.shape[:2]
        x1, y1, x2, y2 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
        pw, ph = (x2 - x1) * 0.6, (y2 - y1) * 0.25
        part = f[max(0, int(y1 - ph)):int(y2 + ph), max(0, int(x1 - pw)):int(x2 + pw)]
        if part.size == 0:
            return None
        im = Image.fromarray(part[:, :, ::-1].copy())   # BGR -> RGB
        im.thumbnail((448, 448))
        return im

    def character(self):
        return CHARACTERS.get(self.store.settings["character"], CHARACTERS["george"])

    def write(self, model, image, context, recent, tags):
        """Returns (line, cost in dollars). Raises on any API problem."""
        import anthropic
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                "data": base64.standard_b64encode(out.getvalue()).decode()}},
                   {"type": "text", "text": f"{context}\nYour recent lines: {json.dumps(recent)}"}]
        system = (PROMPT.replace("STYLE", self.character()["style"])
                  .replace("TAGS_RULE\n", TAGS if tags else NO_TAGS))
        try:
            r = self.client.beta.messages.create(model=model, max_tokens=1024, system=system,
                                                 messages=[{"role": "user", "content": content}], **MODELS[model])
        except anthropic.APIError as e:
            raise RuntimeError(f"Claude error: {e}") from e
        if r.stop_reason == "refusal":
            raise RuntimeError("Claude declined this one")
        line = " ".join(b.text for b in r.content if b.type == "text").strip().strip('"')
        if not line:
            raise RuntimeError("Claude returned no text")
        pin, pout = PRICE[model]
        return line, r.usage.input_tokens * pin / 1e6 + r.usage.output_tokens * pout / 1e6

    def piper_wav(self, text, voice_name):
        from piper import PiperVoice
        if voice_name not in self.piper:
            self.piper[voice_name] = PiperVoice.load(os.path.join(VOICES_DIR, f"{voice_name}.onnx"))
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            self.piper[voice_name].synthesize_wav(text, w)
        return out.getvalue()

    def eleven_mp3(self, text, voice_id):
        import urllib.request
        req = urllib.request.Request(
            ELEVEN_URL.format(voice=voice_id),
            data=json.dumps({"text": text, "model_id": ELEVEN_MODEL}).encode(),
            headers={"xi-api-key": self.eleven_key, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=8).read()

    def add_effect(self, audio):
        """Run the chosen sound effect over the audio. Returns MP3, or the audio unchanged if it fails."""
        import subprocess
        chain = self.character()["effect"]
        if not chain:
            return None
        try:
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-af", chain,
                                "-f", "mp3", "-b:a", "128k", "pipe:1"], input=audio, capture_output=True, timeout=10)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            print(f"Voice: sound effect failed: {r.stderr.decode(errors='replace')[:200]}")
        except Exception as e:
            print(f"Voice: sound effect failed: {e}")
        return None

    def speak(self, text):
        """Returns (audio bytes, content type, cost in dollars, which voice spoke), with the sound effect."""
        audio, kind, cost, who = self.voice_only(text)
        styled = self.add_effect(audio)
        if styled:
            return styled, "audio/mpeg", cost, f"{who} + effect"
        return audio, kind, cost, who

    def voice_only(self, text):
        """The character's ElevenLabs voice; Piper (free, on this computer) if there's no key or it fails."""
        c = self.character()
        if self.eleven_key:
            try:
                audio = self.eleven_mp3(text, c["voice"])
                return audio, "audio/mpeg", len(text) / 1000 * ELEVEN_PRICE, f"ElevenLabs {c['name']}"
            except Exception as e:
                print(f"Voice: ElevenLabs failed ({e}), using Piper")
        plain = " ".join(re.sub(r"\[[^\]]*\]", " ", text).split())   # Piper would read the cues out loud
        return self.piper_wav(plain, PIPER_VOICE), "audio/wav", 0.0, "Piper (backup)"

    async def make_line(self, image, note=""):
        """Returns (line, source, cost). note: extra context for Claude (e.g. "they're still here")."""
        s = self.store
        s.roll_day()
        model = s.settings["model"]
        if self.client is None:
            return random.choice(CANNED), "canned (no key)", 0.0
        if s.today["count"] >= s.settings["dailyCap"]:
            return random.choice(CANNED), "canned (daily limit reached)", 0.0
        recent = [h["text"] for h in self.history[-8:] if h.get("played")]
        try:
            line, cost = await asyncio.to_thread(self.write, model, image,
                                                 time_of_day(datetime.datetime.now()) + note, recent,
                                                 self.eleven_key is not None)
        except Exception as e:
            print(f"Voice: {e}")
            return random.choice(CANNED), "canned (Claude unavailable)", 0.0
        s.today["count"] += 1
        s.today["cost"] += cost
        s.save()
        return line, model, cost

    # ---- Deciding when to talk ----
    async def status(self, text):
        print(f"Voice: {text}")
        await self.send({"type": "voice_status", "text": text})

    def same_person(self, pid):
        p = self.read_person()
        return p if p["present"] and p["id"] == pid and p["box"] else None

    async def comment(self, pid, manual=False, followup=None):
        """Photo -> line -> wait for the talk zone -> play. One at a time.
        followup = (seconds, earlier line): they're still here after their line, so call that out."""
        if self.busy:
            return
        self.busy = True
        try:
            s = self.store.settings
            t0 = time.time()
            # 1. Take the photo once they're out of the no-photo zones and (if mustMove) have moved,
            #    so a mailbox or bush mistaken for a person never gets a line.
            now_or_never = manual or followup is not None   # photo and play right away
            if not now_or_never:
                await asyncio.sleep(SETTLE)
            reached = {"talk": False}
            first = None
            while True:
                p = self.same_person(pid)
                if p is None:
                    if manual:
                        await self.status("Nobody in view right now")
                    return
                if in_any(p["box"], s["talkZones"]):
                    reached["talk"] = True
                fx, fy = feet(p["box"])
                first = first or (fx, fy)
                moved = ((fx - first[0]) ** 2 + (fy - first[1]) ** 2) ** 0.5 >= 0.25 * (p["box"][3] - p["box"][1])
                if now_or_never or (not in_any(p["box"], s["noPhotoZones"]) and (moved or not s["mustMove"])):
                    break
                if time.time() - t0 > s["zoneWait"]:
                    why = "stayed in a no-photo zone" if in_any(p["box"], s["noPhotoZones"]) else "never moved"
                    await self.status(f"#{pid} {why}, no line")
                    return
                await asyncio.sleep(0.1)
            image = self.crop(p["box"])
            if image is None:
                await self.status("No camera picture yet")
                return
            t_photo = time.time()
            await self.status(f"Making a line for #{pid}...")

            # 2. Write and speak it, noting if they walk through a talk zone meanwhile.
            async def watch():
                while True:
                    q = self.same_person(pid)
                    if q and in_any(q["box"], s["talkZones"]):
                        reached["talk"] = True
                    await asyncio.sleep(0.1)
            watcher = asyncio.create_task(watch())
            try:
                note = ""
                if followup:
                    note = (f" This person is STILL standing here, {followup[0]} seconds after you said to them:"
                            f" {json.dumps(followup[1])}. Call out that they're still here; don't repeat that line.")
                line, source, cost = await self.make_line(image, note)
                if followup:
                    source += " (still here)"
                sound = None
                if line.strip().upper().rstrip(".") != SKIP:
                    sound = await asyncio.to_thread(self.speak, line)   # (audio, type, cost, voice)
                    if sound[2]:
                        cost += sound[2]
                        self.store.today["cost"] += sound[2]
                        self.store.save()
                    source += f", {sound[3]}"
            finally:
                watcher.cancel()
            t_ready = time.time()

            # 3. Play it when they're in a talk zone (or already passed through one).
            self.line_number += 1
            entry = {"n": self.line_number, "text": line, "source": source, "cost": cost, "played": False,
                     "time": datetime.datetime.now().strftime("%-I:%M:%S %p"),
                     "seconds": {"photo": round(t_photo - t0, 1), "ready": round(t_ready - t_photo, 1)}}
            if sound is None:
                entry["skipped"] = "nobody there"
            else:
                go = now_or_never or not s["talkZones"] or reached["talk"]
                if not go:
                    await self.status(f"Line ready, waiting for #{pid} to reach a talk zone...")
                    while time.time() - t_ready < s["zoneWait"]:
                        q = self.read_person()
                        if q["present"] and in_any(q["box"], s["talkZones"]):
                            go = True
                            break
                        await asyncio.sleep(0.1)
                if go:
                    self.audio[self.line_number] = sound[:2]
                    for n in [n for n in self.audio if n < self.line_number - 30]:
                        del self.audio[n]
                    entry["played"] = True
                    entry["seconds"]["waited"] = round(time.time() - t_ready, 1)
                    self.last_spoken = time.time()
                    if followup is None:
                        self.spoken_to[pid] = (time.time(), line)
                    if self.on_play:
                        self.on_play(sound[0], sound[1], line)   # e.g. the recorder adds it to the clip
                    self.talks[self.line_number] = {"env": await asyncio.to_thread(loudness, sound[0]),
                                                    "cues": cues_in(line)}
                    for n in [n for n in self.talks if isinstance(n, int) and n < self.line_number - 30]:
                        del self.talks[n]
                    await self.send_speakers({"type": "play", "n": self.line_number})
                    if not any(sp.get("connected") for sp in self.speakers.values()):
                        await self.eye_talk(self.line_number)   # no yard speaker: in time with the phone
                else:
                    entry["skipped"] = "never reached a talk zone"
            entry["seconds"]["total"] = round(time.time() - t0, 1)
            self.history.append(entry)
            del self.history[:-50]
            what = "played" if entry["played"] else f"not played: {entry['skipped']}"
            print(f'Voice #{pid}: "{line}" ({source}, {what}, {entry["seconds"]}, ${cost:.4f})')
            await self.send({"type": "voice_line", **entry, "today": self.store.today})
        except Exception:
            traceback.print_exc()
            await self.status("Something went wrong; see the log")
        finally:
            self.busy = False

    def should_talk(self, ignore_cooldown=False):
        s = self.store.settings
        a, b, h = s["quietFrom"], s["quietTo"], datetime.datetime.now().hour
        quiet = a != b and ((a < b and a <= h < b) or (a > b and (h >= a or h < b)))
        cooled = ignore_cooldown or time.time() - self.last_spoken >= s["cooldown"]
        return s["enabled"] and not self.busy and cooled and not quiet

    async def run(self):
        """Watch who the tracker follows; someone new = someone to talk to."""
        await asyncio.to_thread(self.piper_wav, "Hello.", PIPER_VOICE)   # first run is slow; do it now
        print(f"Voice ready ({'Claude ' + self.store.settings['model'] if self.client else 'canned lines only'})")
        last_id, last_sent = None, 0.0
        while True:
            await asyncio.sleep(0.1)
            try:
                p = self.read_person()
                pid = p["id"] if p["present"] else None
                if pid is not None and pid != last_id:
                    last_id = pid
                    if self.should_talk():
                        asyncio.create_task(self.comment(pid))
                linger = self.store.settings["lingerSeconds"]
                spoke = self.spoken_to.get(pid)
                if (linger and spoke and pid not in self.followed_up and time.time() - spoke[0] >= linger
                        and self.should_talk(ignore_cooldown=True)):
                    self.followed_up.add(pid)   # only ever once per person
                    asyncio.create_task(self.comment(pid, followup=(round(time.time() - spoke[0]), spoke[1])))
                if len(self.spoken_to) > 200:   # forget people long gone
                    for old in sorted(self.spoken_to, key=lambda k: self.spoken_to[k][0])[:100]:
                        self.spoken_to.pop(old, None)
                if time.time() - last_sent > 0.25:   # let pages show where the person is standing
                    last_sent = time.time()
                    box = p["box"] if p["present"] else None
                    s = self.store.settings
                    await self.send({"type": "voice_person", "feet": feet(box),
                                     "talk": bool(box) and in_any(box, s["talkZones"]),
                                     "noPhoto": bool(box) and in_any(box, s["noPhotoZones"])})
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(2)

    # ---- The phone page ----
    def hello(self):
        self.store.roll_day()
        return {"type": "voice_hello", "settings": self.store.settings, "today": self.store.today,
                "history": self.history[-20:], "models": list(MODELS),
                "characters": {k: c["name"] for k, c in CHARACTERS.items()},
                "hasKey": self.client is not None, "hasEleven": self.eleven_key is not None,
                "speakers": self.speaker_list()}

    async def eye_talk(self, n):
        """Tell the eye pages to act out line n, starting now (they add their own sync delay)."""
        talk = self.talks.get(n)
        if not talk or not self.send_eye or (n, round(time.time())) in self.talked:
            return
        self.talked = {k for k in self.talked if time.time() - k[1] < 30} | {(n, round(time.time()))}
        await self.send_eye({"type": "talk", "env": talk["env"], "step": TALK_STEP, "cues": talk["cues"]})

    # ---- Yard speakers ----
    def speaker_list(self):
        return list(self.speakers.values())

    async def send_speakers(self, msg):
        text = json.dumps(msg)
        for ws in list(self.speakers):
            try:
                await ws.send_str(text)
            except Exception:
                self.speakers.pop(ws, None)

    async def speaker_handler(self, request):
        """A yard speaker (speaker/player.py) connects here: it's told when to play each line
        and reports whether its Bluetooth speaker is connected."""
        from aiohttp import web
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        self.speakers[ws] = {"name": request.query.get("name", "speaker"), "connected": False}
        print(f"Voice: yard speaker {self.speakers[ws]['name']} connected")
        try:
            async for m in ws:
                try:
                    msg = json.loads(m.data)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "status":
                    self.speakers[ws] = {"name": str(msg.get("name", "speaker"))[:40],
                                         "connected": bool(msg.get("connected")),
                                         "device": str(msg.get("device", ""))[:40]}
                    await self.send({"type": "voice_speakers", "speakers": self.speaker_list()})
                elif msg.get("type") == "started":
                    n = msg.get("n")
                    await self.eye_talk(n if n == "preview" else int(n))
                elif msg.get("type") == "played":
                    print(f"Voice: yard speaker played line {msg.get('n')}")
        finally:
            gone = self.speakers.pop(ws, {})
            print(f"Voice: yard speaker {gone.get('name', '')} disconnected")
            await self.send({"type": "voice_speakers", "speakers": self.speaker_list()})
        return ws

    async def handle(self, msg):
        kind = msg.get("type")
        if kind == "voice_setting" and self.store.set(msg.get("key"), msg.get("value")):
            await self.send({"type": "voice_settings", "settings": self.store.settings})
        elif kind == "voice_speaker_test":
            await self.send_speakers({"type": "test"})
        elif kind == "voice_say":
            p = self.read_person()
            if p["present"]:
                asyncio.create_task(self.comment(p["id"], manual=True))
            else:
                await self.status("Nobody in view right now")

    def preview(self):
        """A sample line in the current character's voice. Kept per character, so only the first
        preview of each costs anything (the sound effect is added fresh, on this computer)."""
        key = self.store.settings["character"]
        if key not in self.preview_cache:
            audio, kind, cost, _ = self.voice_only(self.character()["preview"])
            self.preview_cache[key] = (audio, kind)
            if cost:
                self.store.roll_day()
                self.store.today["cost"] += cost
                self.store.save()
        audio, kind = self.preview_cache[key]
        styled = self.add_effect(audio)
        audio, kind = (styled, "audio/mpeg") if styled else (audio, kind)
        self.talks["preview"] = {"env": loudness(audio), "cues": cues_in(self.character()["preview"])}
        return audio, kind

    async def preview_handler(self, request):
        from aiohttp import web
        audio, kind = await asyncio.to_thread(self.preview)
        return web.Response(body=audio, content_type=kind, headers={"Cache-Control": "no-store"})

    async def audio_handler(self, request):
        from aiohttp import web
        found = self.audio.get(int(request.match_info["n"]))
        if found is None:
            raise web.HTTPNotFound()
        return web.Response(body=found[0], content_type=found[1])
