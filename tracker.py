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
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))  # saved calibration lives here

# ---- Shared state between the camera thread and the web server ------------
state = {"present": False, "id": None, "x": 0.5, "y": 0.5, "size": 0.0, "box": None}
debug_jpeg = None
lock = threading.Lock()


def hide_password(source):
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:****@", str(source))


class LatestFrame:
    """Reads the camera in the background and keeps only the newest frame,
    so detection never falls behind a live stream."""

    def __init__(self, source):
        self.source = source
        self.frame = None
        self.lock = threading.Lock()
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
            time.sleep(2)

    def _set(self, frame):
        with self.lock:
            self.frame = frame

    def _read_network(self):
        # Network cameras are read through the ffmpeg program, which handles the
        # Wyze's encrypted (rtsps) stream more reliably than OpenCV, and shrinks
        # the picture so detection is faster.
        w, h = FRAME_SIZE
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-rtsp_transport", "tcp", "-timeout", "5000000",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-i", self.source, "-an", "-vf", f"scale={w}:{h}",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        print(f"Connecting to camera {hide_password(self.source)} ...")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._log_errors, args=(proc.stderr,), daemon=True).start()
        size = w * h * 3
        first = True
        try:
            while True:
                data = proc.stdout.read(size)
                if len(data) < size:
                    print("Camera stream dropped, reconnecting...")
                    return
                if first:
                    print("Camera connected")
                    first = False
                self._set(np.frombuffer(data, np.uint8).reshape(h, w, 3).copy())
        finally:
            proc.kill()
            proc.wait()

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

    def get(self):
        with self.lock:
            frame, self.frame = self.frame, None
        return frame


def detect_loop():
    model = YOLO("yolo11n.pt")   # downloads a small (~5 MB) model the first time
    camera = LatestFrame(SOURCE)
    # Keep going even if one frame causes an error, so tracking never silently stops.
    while True:
        try:
            _detect_loop(model, camera)
        except Exception:
            traceback.print_exc()
            print("Detection error, restarting in 2s...")
            time.sleep(2)


def _detect_loop(model, camera):
    global debug_jpeg
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
        if box:
            x1, y1, x2, y2 = box
            tx, ty = (x1 + x2) / 2, y1 + (y2 - y1) * HEAD_POINT
            last_target = {"present": True, "id": following, "x": tx / w, "y": ty / h, "size": (y2 - y1) / h,
                           "box": [x1 / w, y1 / h, x2 / w, y2 / h]}
            if now - last_print > 2:
                last_print = now
                t = last_target
                print(f"person #{following}  x={t['x']:.2f}  y={t['y']:.2f}  size={t['size']:.2f}")
        # If they briefly vanish (e.g. behind a tree), keep looking where they were.
        new = last_target or {"present": False, "id": None, "x": 0.5, "y": 0.5, "size": 0.0, "box": None}
        with lock:
            state.update(new)

        # Debug picture: everyone in green, the followed person in yellow, aim point as a red dot,
        # ignored spots as gray outlines.
        for z in ignore_zones.zones:
            cv2.rectangle(frame, (int(z[0] * w), int(z[1] * h)), (int(z[2] * w), int(z[3] * h)), (128, 128, 128), 2)
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
    try:
        async for m in ws:
            try:
                msg = json.loads(m.data)
            except (TypeError, ValueError):
                continue
            kind = msg.get("type")
            if kind == "aim":
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
                "points": calibration.points, "ignored": len(ignore_zones.zones)}))


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


def main():
    if isinstance(SOURCE, int):
        # On a Mac, the webcam permission popup only appears if we ask from the main thread.
        cv2.VideoCapture(SOURCE).release()
    global calibration, ignore_zones, settings
    calibration = Calibration(os.path.join(DATA_DIR, "calibration.json"))
    ignore_zones = IgnoreZones(os.path.join(DATA_DIR, "ignore_zones.json"))
    settings = Settings(os.path.join(DATA_DIR, "settings.json"))
    threading.Thread(target=detect_loop, daemon=True).start()
    app = web.Application()
    app.on_startup.append(start_background)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/calibrate", calibrate_page)
    app.router.add_get("/calws", cal_ws_handler)
    app.router.add_get("/debug", debug_page)
    app.router.add_get("/debug.mjpg", debug_stream)
    app.router.add_static("/", "web")
    print(f"Web server started on port {PORT} (detecting on {DEVICE}, {DETECT_SIZE}px)")
    web.run_app(app, port=PORT, print=None)


def restart_when_code_changes():
    """When running as a background service, quit if a program file is updated;
    the service starts it again right away with the new code (no password needed)."""
    files = [os.path.join(HERE, f) for f in ("tracker.py", "calibration.py", "settings.py")]
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
