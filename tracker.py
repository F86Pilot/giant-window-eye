"""Giant Eye tracker: camera -> person detection -> eye page.

Run:  .venv/bin/python tracker.py
  Uses the camera address in camera_url.txt if that file exists,
  otherwise the computer's built-in webcam.
  To use the webcam anyway:  .venv/bin/python tracker.py 0

Then open:
  http://localhost:8080/            the eye
  http://localhost:8080/calibrate   aim the eye from your phone (see calibration.py)
  http://localhost:8080/debug       camera view with detection boxes
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

import cv2
import numpy as np
import torch
from aiohttp import web
from ultralytics import YOLO

from calibration import Calibration, IgnoreZones
from settings import SCHEMA, Settings

# ---- Settings -------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
URL_FILE = os.path.join(HERE, "camera_url.txt")
if len(sys.argv) > 1:
    SOURCE = sys.argv[1]
elif os.path.exists(URL_FILE):
    SOURCE = open(URL_FILE).read().strip()
else:
    SOURCE = "0"
if SOURCE.isdigit():
    SOURCE = int(SOURCE)   # a number means a webcam on this computer
PORT = int(os.environ.get("PORT", 8080))
START_CONFIDENCE = 0.35  # how sure the model must be to START following someone (phone Settings can change it)
KEEP_CONFIDENCE = 0.1    # once following someone, keep tracking them down to this (helps in the dark)
HEAD_POINT = 0.15       # aim at this fraction down from the top of the person's box (head area)
# On a Mac, detection runs on the Apple GPU ("mps"), roughly 10x faster than a
# typical processor, so it can afford to look at a more detailed picture.
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
DETECT_SIZE = 960 if DEVICE == "mps" else 640   # smaller = faster, bigger = sees people further away
SEND_PER_SECOND = 15
LOST_GRACE = 1.5        # seconds to wait for the followed person to reappear before switching
FRAME_SIZE = (960, 540) if DEVICE == "mps" else (640, 360)  # camera picture is shrunk to this first
BIKE_EVERY = 3          # look for a bicycle under the followed person on every 3rd frame
BIKE_MEMORY = 1.5       # seconds a "this person is on a bike" sighting stays true
# The camera's first frames after connecting can carry out-of-order timestamps, and the JPEG encoder (and
# muxers) refuse those; renumbering both outputs the same way keeps every frame and keeps them in step.
RETIME = "setpts=if(gt(FRAME_RATE\\,5)*lt(FRAME_RATE\\,61)\\,N/FRAME_RATE\\,N/20)/TB"   # steady timeline (20 fps if the camera's rate is unknown)
PHOTO_KEEP = 30         # how many recent full-size camera pictures to keep for the voice's photos
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))  # saved calibration lives here

# ---- Shared state between the camera thread and the web server ------------
state = {"present": False, "id": None, "x": 0.5, "y": 0.5, "size": 0.0, "box": None, "frame": None,
         "rider": False, "aspect": 16 / 9}
debug_jpeg = None
clean_frame = None   # newest camera picture without boxes drawn on it (the voice takes photos from it)
lock = threading.Lock()


def hide_password(source):
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:****@", str(source))


class LatestFrame:
    """Reads the camera in the background and keeps only the newest frame,
    so detection never falls behind a live stream."""

    def __init__(self, source, extra_output=None):
        self.source = source
        self.extra_output = extra_output   # function -> extra ffmpeg output args (recordings), or None
        self.proc = None
        self.frame = None
        self.lock = threading.Lock()
        # Every camera frame gets a number. The shrunken picture (for detection) and the full-size one
        # (for the voice's photos) arrive separately, so the numbers let the voice crop from the very
        # frame its person-box was measured on, even though the full-size picture arrives a little later.
        self.next_index = 0       # number for the next connection's first frame
        self.frame_index = None   # number of the newest shrunken frame
        self.taken_index = None   # number of the frame get() handed out last
        self.photos = {}          # frame number -> full-size JPEG (the last PHOTO_KEEP)
        self.new_photo = threading.Condition(self.lock)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        is_network = isinstance(self.source, str) and "://" in self.source
        while True:
            if is_network:
                self._read_network()
            else:
                self._read_local()
            with self.lock:
                self.frame = None
                self.photos.clear()
            time.sleep(2)

    def _set(self, frame, index=None):
        with self.lock:
            self.frame = frame
            self.frame_index = index

    def _read_network(self):
        # Network cameras are read through the ffmpeg program, which handles the
        # Wyze's encrypted (rtsps) stream more reliably than OpenCV, and shrinks
        # the picture so detection is faster.
        w, h = FRAME_SIZE
        # The shrunken picture goes to detection; the voice's photos come from a second, full-size
        # output (every frame as a JPEG) so Claude sees people in all the detail the camera gives.
        # "-fps_mode passthrough": exactly one picture out per frame in, so the two outputs stay in step.
        photo_r, photo_w = os.pipe()
        transport = ["-rtsp_transport", "tcp", "-timeout", "5000000"] if str(self.source).startswith("rtsp") else []
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"] + transport + [
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-i", self.source, "-map", "0:v", "-an", "-fps_mode", "passthrough", "-vf", f"scale={w}:{h},{RETIME}",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
               "-map", "0:v", "-an", "-fps_mode", "passthrough", "-vf", RETIME, "-q:v", "3",
               "-c:v", "mjpeg", "-flush_packets", "1", "-f", "image2pipe", f"pipe:{photo_w}"]
        extra = self.extra_output() if self.extra_output else None
        if extra:
            cmd += extra   # also save the camera's own video for recordings (see recorder.py)
        print(f"Connecting to camera {hide_password(self.source)} ..." + (" (recording)" if extra else ""))
        try:
            proc = self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                pass_fds=(photo_w,))
        except Exception:
            os.close(photo_r)
            raise
        finally:
            os.close(photo_w)
        base = self.next_index
        threading.Thread(target=self._read_photos, args=(photo_r, base), daemon=True).start()
        threading.Thread(target=self._log_errors, args=(proc.stderr,), daemon=True).start()
        size = w * h * 3
        first = True
        count = 0
        try:
            while True:
                data = proc.stdout.read(size)
                if len(data) < size:
                    print("Camera stream dropped, reconnecting...")
                    return
                if first:
                    print("Camera connected")
                    first = False
                self._set(np.frombuffer(data, np.uint8).reshape(h, w, 3).copy(), base + count)
                count += 1
        finally:
            self.next_index = base + count + 1000   # a reconnect's numbers never overlap this one's
            proc.kill()
            proc.wait()

    def _read_photos(self, fd, base):
        """Collect the full-size pictures, numbered to match the shrunken frames. The pipe carries JPEGs
        back to back; each ends with FF D9 (inside the picture data an FF byte is always followed by 00,
        so that pair can't appear earlier)."""
        buf = b""
        n = 0
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while True:
                chunk = pipe.read(1 << 16)
                if not chunk:
                    return
                buf += chunk
                while (end := buf.find(b"\xff\xd9")) >= 0:
                    jpeg, buf = buf[:end + 2], buf[end + 2:]
                    with self.new_photo:
                        self.photos[base + n] = jpeg
                        n += 1
                        for old in [k for k in self.photos if k < base + n - PHOTO_KEEP]:
                            del self.photos[old]
                        self.new_photo.notify_all()

    def photo_frame(self, index=None, wait=0.8):
        """The full-size camera picture (BGR) of frame number `index`, or the newest one if None.
        It arrives a little after the shrunken frame, so this waits up to `wait` seconds for it.
        None if there isn't one."""
        deadline = time.time() + (wait if index is not None else 0)
        with self.new_photo:
            while True:
                if index is None:
                    key = max(self.photos, default=None)
                elif index in self.photos:
                    key = index
                else:
                    near = min(self.photos, key=lambda k: abs(k - index), default=None)
                    newest = max(self.photos, default=-1)
                    key = near if near is not None and abs(near - index) <= 3 and newest > index else None
                    if key is None and newest <= index and time.time() < deadline:
                        self.new_photo.wait(deadline - time.time())
                        continue
                break
            jpeg = self.photos.get(key) if key is not None else None
        if jpeg is None:
            return None
        return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

    @staticmethod
    def _log_errors(pipe):
        for line in pipe:
            print("camera:", hide_password(line.decode(errors="replace").strip()))

    def _read_local(self):
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            print(f"Can't open camera {self.source!r}, retrying...")
            return
        print(f"Camera {self.source!r} connected")
        # A video file (for testing) would otherwise play as fast as possible.
        delay = 1 / (cap.get(cv2.CAP_PROP_FPS) or 15) if isinstance(self.source, str) else 0
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Camera stream dropped, reconnecting...")
                break
            time.sleep(delay)
            self._set(frame)
        cap.release()

    def reconnect(self):
        """Drop the camera connection; it reconnects by itself (e.g. after recording is switched on/off)."""
        if self.proc:
            self.proc.kill()

    def get(self):
        with self.lock:
            frame, self.frame = self.frame, None
            self.taken_index = self.frame_index
        return frame


camera_reader = None


def photo_frame(index=None):
    """What the voice takes its photos from: the camera's full-size picture of frame number `index`
    (the one the person-box was measured on), or (local cameras, or just after a reconnect) the
    shrunken detection picture."""
    full = camera_reader.photo_frame(index) if camera_reader else None
    return full if full is not None else clean_frame


def detect_loop():
    global camera_reader
    model = YOLO("yolo11n.pt")   # downloads a small (~5 MB) model the first time
    camera = camera_reader = LatestFrame(SOURCE, recorder.ffmpeg_output if recorder else None)
    # Keep going even if one frame causes an error, so tracking never silently stops.
    while True:
        try:
            _detect_loop(model, camera)
        except Exception:
            traceback.print_exc()
            print("Detection error, restarting in 2s...")
            time.sleep(2)


def on_bike(bike_model, frame, box):
    """Is there a bicycle or motorbike under this person (box = x1, y1, x2, y2 in the frame's pixels)?
    A separate model instance, so it can't disturb the person tracking."""
    r = bike_model.predict(frame, imgsz=DETECT_SIZE, classes=[1, 3], conf=0.25, device=DEVICE, verbose=False)[0]
    x1, y1, x2, y2 = box
    for bx1, by1, bx2, by2 in r.boxes.xyxy.tolist():
        ix, iy = max(0, min(x2, bx2) - max(x1, bx1)), max(0, min(y2, by2) - max(y1, by1))
        if ix * iy > 0.15 * (x2 - x1) * (y2 - y1) and by2 > y1 + 0.5 * (y2 - y1):
            return True
    return False


def _detect_loop(model, camera):
    global debug_jpeg, clean_frame
    bike_model = YOLO("yolo11n.pt")
    frame_count = 0
    rider_seen = {}         # person ID -> last time a bicycle was seen under them
    confirmed = {}          # person ID -> last time seen, for people sure enough to follow
    last_print = 0
    following = None        # ID number of the person the eye is locked onto
    following_seen = 0      # last time that person was seen
    last_target = None
    while True:
        frame = camera.get()
        if frame is None:
            time.sleep(0.01)
            continue
        frame_no = camera.taken_index   # which camera frame this is (see LatestFrame)
        h, w = frame.shape[:2]
        now = time.time()

        # track() gives each person an ID number that stays the same from frame to frame.
        # Low-confidence detections are passed to the tracker so it can keep hold of
        # someone who's hard to see (in the dark), but only people who were at least
        # once seen with START confidence count as people.
        result = model.track(frame, imgsz=DETECT_SIZE, classes=[0], conf=KEEP_CONFIDENCE, device=DEVICE,
                             persist=True, tracker="bytetrack.yaml", verbose=False)[0]
        start_conf = settings.values.get("detectConfidence", START_CONFIDENCE)
        people, ignored = {}, []
        if result.boxes.id is not None:
            for box, pid, conf in zip(result.boxes.xyxy.tolist(), result.boxes.id.int().tolist(),
                                      result.boxes.conf.tolist()):
                # Skip "people" in spots marked as not-a-person (e.g. the mailbox).
                if ignore_zones.contains((box[0] + box[2]) / 2 / w, (box[1] + box[3]) / 2 / h):
                    ignored.append(box)
                    continue
                if pid not in confirmed and conf < start_conf:
                    continue    # not sure enough yet that this is a person
                confirmed[pid] = now
                people[pid] = box
        for pid in [p for p, t in confirmed.items() if now - t > 60]:
            del confirmed[pid]   # forget people long gone

        # Stay on the same person. Only if they've been gone for LOST_GRACE seconds,
        # switch to whoever is biggest (usually closest).
        if following in people:
            following_seen = now
        elif now - following_seen > LOST_GRACE:
            if people:
                following = max(people, key=lambda i: (people[i][2] - people[i][0]) * (people[i][3] - people[i][1]))
                following_seen = now
                print(f"now following person #{following}   ({len(people)} in view)")
            elif following is not None:
                following = None
                last_target = None
                print("nobody")

        box = people.get(following)
        frame_count += 1
        if box:
            if frame_count % BIKE_EVERY == 0 and on_bike(bike_model, frame, box):
                rider_seen[following] = now
            x1, y1, x2, y2 = box
            tx, ty = (x1 + x2) / 2, y1 + (y2 - y1) * HEAD_POINT
            last_target = {"present": True, "id": following, "x": tx / w, "y": ty / h, "size": (y2 - y1) / h,
                           "box": [x1 / w, y1 / h, x2 / w, y2 / h], "frame": frame_no,
                           "rider": now - rider_seen.get(following, 0) < BIKE_MEMORY, "aspect": w / h}
            if now - last_print > 2:
                last_print = now
                t = last_target
                print(f"person #{following}  x={t['x']:.2f}  y={t['y']:.2f}  size={t['size']:.2f}")
        # If they briefly vanish (e.g. behind a tree), keep looking where they were.
        new = last_target or {"present": False, "id": None, "x": 0.5, "y": 0.5, "size": 0.0, "box": None, "frame": None,
                              "rider": False, "aspect": w / h}
        with lock:
            state.update(new)

        # Keep a clean copy for the voice's photos; the boxes below are drawn on a copy.
        clean_frame = frame
        frame = frame.copy()

        # Debug picture: everyone in green, the followed person in yellow, aim point as a red dot,
        # ignored spots as gray outlines.
        for z in ignore_zones.zones:
            if isinstance(z[0], (int, float)):
                cv2.rectangle(frame, (int(z[0] * w), int(z[1] * h)), (int(z[2] * w), int(z[3] * h)), (128, 128, 128), 2)
            else:
                cv2.polylines(frame, [np.array([[int(px * w), int(py * h)] for px, py in z], np.int32)], True,
                              (128, 128, 128), 2)
        for b in ignored:
            cv2.rectangle(frame, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (128, 128, 128), 2)
        for pid, b in people.items():
            color = (0, 255, 255) if pid == following else (0, 255, 0)
            cv2.rectangle(frame, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), color, 2)
            cv2.putText(frame, f"#{pid}", (int(b[0]) + 4, int(b[1]) + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        if box:
            cv2.circle(frame, (int(tx), int(ty)), 8, (0, 0, 255), -1)
        ok, jpg = cv2.imencode(".jpg", cv2.resize(frame, (640, int(640 * h / w))))
        if ok:
            debug_jpeg = jpg.tobytes()


# ---- Web server --------------------------------------------------------------
clients = set()


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    clients.add(ws)
    print(f"Eye page connected ({len(clients)} open)")
    await ws.send_str(json.dumps({"type": "settings", "values": settings.values}))
    try:
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)
        print(f"Eye page disconnected ({len(clients)} open)")
    return ws


# ---- Calibration (the /calibrate page on your phone) -------------------------
calibration = None
ignore_zones = None
settings = None
cal_clients = set()
manual = {"on": False, "x": 0.0, "y": 0.0}   # phone is steering the eye by hand


async def cal_ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    cal_clients.add(ws)
    print("Calibration page connected")
    await ws.send_str(json.dumps({"type": "settings", "schema": SCHEMA, "values": settings.values}))
    if voice:
        await ws.send_str(json.dumps(voice.hello()))
    if recorder:
        await ws.send_str(json.dumps(await asyncio.to_thread(recorder.hello)))
    try:
        async for m in ws:
            try:
                msg = json.loads(m.data)
            except (TypeError, ValueError):
                continue
            kind = msg.get("type")
            if isinstance(kind, str) and kind.startswith("voice_"):
                if voice:
                    await voice.handle(msg)
            elif isinstance(kind, str) and kind.startswith("rec_"):
                if recorder:
                    await recorder.handle(msg)
            elif kind == "aim":
                manual.update(on=True, x=float(msg["x"]), y=float(msg["y"]))
            elif kind == "release":
                manual["on"] = False
            elif kind == "save":
                with lock:
                    seen = dict(state)
                if not manual["on"]:
                    reply = {"type": "saved", "ok": False, "msg": "Aim the eye first"}
                elif not seen["present"]:
                    reply = {"type": "saved", "ok": False, "msg": "The camera can't see you right now"}
                else:
                    calibration.add(seen["x"], seen["y"], seen["size"], manual["x"], manual["y"])
                    manual["on"] = False
                    n = len(calibration.points)
                    print(f"Saved calibration spot {n}")
                    reply = {"type": "saved", "ok": True, "msg": f"Saved spot {n}"}
                await ws.send_str(json.dumps(reply))
            elif kind == "delete":
                calibration.delete(int(msg["index"]))
            elif kind == "ignore":
                with lock:
                    seen = dict(state)
                if seen["present"] and seen["box"]:
                    ignore_zones.add(*seen["box"])
                    print(f"Ignoring spot {len(ignore_zones.zones)}: {ignore_zones.zones[-1]}")
                    reply = {"type": "saved", "ok": True, "msg": "OK, ignoring that spot from now on"}
                else:
                    reply = {"type": "saved", "ok": False, "msg": "Nothing is being followed right now"}
                await ws.send_str(json.dumps(reply))
            elif kind == "setting":
                # A setting changed on the phone: save it and update every eye right away.
                if settings.set(msg.get("key"), msg.get("value")):
                    update = json.dumps({"type": "settings", "values": settings.values})
                    await send_all(clients, update)
                    await send_all(cal_clients, update)
            elif kind == "react":
                # Test button on the phone page: tell every eye page to do it now.
                if msg.get("action") in ("sleep", "startle", "roll", "doubletake", "blink"):
                    await send_all(clients, json.dumps({"type": "react", "action": msg["action"]}))
            elif kind == "clear_ignore":
                ignore_zones.clear()
            elif kind == "ignore_shape":
                ok = ignore_zones.add_shape(msg.get("corners") or [])
                await ws.send_str(json.dumps({"type": "saved", "ok": ok,
                                              "msg": "Ignoring that area from now on" if ok else "That shape didn't work"}))
            elif kind == "ignore_delete":
                ignore_zones.delete(int(msg.get("index", -1)))
            elif kind == "clear":
                calibration.clear()
    finally:
        cal_clients.discard(ws)
        if not cal_clients:
            manual["on"] = False   # phone left: go back to following people
        print("Calibration page disconnected")
    return ws


STARTED = int(time.time())


def page_version():
    """Changes whenever the server restarts or a file in web/ is edited.
    Eye pages reload themselves when they see it change, so they never run old code."""
    newest = max(os.path.getmtime(os.path.join("web", f)) for f in os.listdir("web"))
    return f"{STARTED}-{int(newest)}"


def eye_message():
    with lock:
        s = dict(state)
    s["v"] = version
    if manual["on"]:
        s.update(present=True, lx=manual["x"], ly=manual["y"], manual=True)
    elif s["present"]:
        s["lx"], s["ly"] = calibration.look(s["x"], s["y"], s["size"])
    return s


def rounded(d):
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()}


async def send_all(sockets, text):
    for ws in list(sockets):
        try:
            await ws.send_str(text)
        except Exception:
            sockets.discard(ws)


version = None


async def broadcast():
    global version
    tick = 0
    while True:
        await asyncio.sleep(1 / SEND_PER_SECOND)
        if tick % SEND_PER_SECOND == 0:   # check for edited page files once a second
            version = page_version()
        msg = eye_message()
        await send_all(clients, json.dumps(rounded(msg)))
        tick += 1
        if cal_clients and tick % 3 == 0:
            with lock:
                s = dict(state)
            predicted = calibration.look(s["x"], s["y"], s["size"]) if s["present"] else None
            await send_all(cal_clients, json.dumps({
                "type": "status", "person": rounded(s) if s["present"] else None,
                "predicted": predicted, "manual": manual["on"],
                "points": calibration.points, "ignored": len(ignore_zones.zones),
                "ignoreZones": ignore_zones.zones}))


async def debug_page(request):
    return web.Response(content_type="text/html", text=(
        "<body style='margin:0;background:#111;color:#ccc;font-family:sans-serif'>"
        "<p style='margin:8px'>Yellow = person being followed, green = others, red dot = where the eye aims</p>"
        "<img src='/debug.mjpg' style='max-width:100%'></body>"))


async def debug_stream(request):
    resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
    await resp.prepare(request)
    last = None
    try:
        while True:
            jpg = debug_jpeg
            if jpg is not None and jpg is not last:
                last = jpg
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(0.05)
    except ConnectionError:
        pass   # the debug tab was closed
    return resp


async def index(request):
    return web.FileResponse("web/index.html", headers={"Cache-Control": "no-store"})


async def calibrate_page(request):
    return web.FileResponse("web/calibrate.html", headers={"Cache-Control": "no-store"})


async def start_background(app):
    app["broadcast"] = asyncio.create_task(broadcast())
    if voice:
        app["voice"] = asyncio.create_task(voice.run())
    if recorder:
        app["recorder"] = asyncio.create_task(recorder.run())


# ---- Voice (voice.py): funny lines to passersby, played on the phone page -------
voice = None


def start_voice():
    """The voice is optional: if it can't start, the eye carries on without it."""
    global voice
    try:
        from voice import Voice

        def read_person():
            with lock:
                return {"present": state["present"], "id": state["id"], "box": state["box"],
                        "frame": state["frame"],    # which camera frame the box was measured on
                        "rider": state["rider"],    # a bicycle seen under them just now
                        "aspect": state["aspect"]}  # picture width / height

        voice = Voice(DATA_DIR, read_person, photo_frame,
                      lambda msg: send_all(cal_clients, json.dumps(msg)))
        voice.send_eye = lambda msg: send_all(clients, json.dumps(msg))   # the eye acts out each line
    except Exception:
        traceback.print_exc()
        print("Voice couldn't start; the eye runs without it")


# ---- Recordings (recorder.py): a video clip of each visit, on an external drive -------
recorder = None


def start_recorder():
    """Optional, like the voice: if it can't start, the eye carries on without it."""
    global recorder
    if not (isinstance(SOURCE, str) and "://" in SOURCE):
        return   # only for network cameras (it saves the camera's own video stream)
    try:
        from recorder import Recorder

        def read_person():
            with lock:
                return {"present": state["present"], "id": state["id"], "box": state["box"]}

        recorder = Recorder(DATA_DIR, read_person, lambda msg: send_all(cal_clients, json.dumps(msg)))
        recorder.on_toggle = lambda: camera_reader and camera_reader.reconnect()
        if voice:
            voice.on_play = recorder.line_played
    except Exception:
        traceback.print_exc()
        print("Recordings couldn't start; the eye runs without them")


def main():
    if isinstance(SOURCE, int):
        # On a Mac, the webcam permission popup only appears if we ask from the main thread.
        cv2.VideoCapture(SOURCE).release()
    global calibration, ignore_zones, settings
    calibration = Calibration(os.path.join(DATA_DIR, "calibration.json"))
    ignore_zones = IgnoreZones(os.path.join(DATA_DIR, "ignore_zones.json"))
    settings = Settings(os.path.join(DATA_DIR, "settings.json"))
    start_voice()
    start_recorder()
    threading.Thread(target=detect_loop, daemon=True).start()
    app = web.Application()
    app.on_startup.append(start_background)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/calibrate", calibrate_page)
    app.router.add_get("/calws", cal_ws_handler)
    app.router.add_get("/debug", debug_page)
    app.router.add_get("/debug.mjpg", debug_stream)
    if voice:
        app.router.add_get("/voice/audio/{n}", voice.audio_handler)
        app.router.add_get("/voice/preview", voice.preview_handler)
        app.router.add_get("/voice/speaker", voice.speaker_handler)
        app.router.add_get("/voice/photo/{name}", voice.photo_handler)
    if recorder:
        app.router.add_get("/recordings/{day}/{name}", recorder.file_handler)
    app.router.add_static("/", "web")
    print(f"Web server started on port {PORT} (detecting on {DEVICE}, {DETECT_SIZE}px)")
    web.run_app(app, port=PORT, print=None)


def restart_when_code_changes():
    """When running as a background service, quit if a program file is updated;
    the service starts it again right away with the new code (no password needed)."""
    files = [os.path.join(HERE, f) for f in ("tracker.py", "calibration.py", "settings.py", "voice.py", "recorder.py")]
    stamp = lambda: [os.path.getmtime(f) for f in files]
    start = stamp()
    while True:
        time.sleep(5)
        try:
            if stamp() != start:
                print("Program files updated, restarting...")
                os._exit(0)
        except OSError:
            pass   # a file is mid-copy; check again next time


class RotatingLog:
    """Output file that never grows past max_bytes: the older half moves to .1"""

    def __init__(self, path, max_bytes=5_000_000):
        self.path, self.max_bytes = path, max_bytes
        self.f = open(path, "a")

    def write(self, text):
        self.f.write(text)
        self.f.flush()
        if self.f.tell() > self.max_bytes:
            self.f.close()
            os.replace(self.path, self.path + ".1")
            self.f = open(self.path, "a")

    def flush(self):
        self.f.flush()


if __name__ == "__main__":
    os.chdir(HERE)
    if os.environ.get("LOG_FILE"):   # set when running as a background service
        sys.stdout = sys.stderr = RotatingLog(os.environ["LOG_FILE"])
    if os.environ.get("KEEP_AWAKE"):
        # Keep the Mac from sleeping for as long as this program runs. (Started from
        # here, not from the service file, so macOS's local-network permission for
        # Python covers the camera connection.)
        subprocess.Popen(["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())])
    if os.environ.get("LOG_FILE"):
        threading.Thread(target=restart_when_code_changes, daemon=True).start()
    main()
