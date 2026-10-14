"""Giant Eye yard speaker: plays the eye's lines on a Bluetooth speaker, from a Raspberry Pi.

Runs on a Pi near a window (see speaker/install.sh). It connects to the tracker on the Mac,
and whenever the eye has a line to say, downloads it and plays it on the Bluetooth speaker.
It also:
  - reconnects to the speaker whenever it's turned back on,
  - plays a short, nearly silent sound every few minutes so the speaker doesn't switch
    itself off from inactivity,
  - tells the controls page whether the speaker is connected.

Settings come from the environment (set in eye-speaker.service):
  EYE_SERVER    the tracker, e.g. http://my-mac.local:8085
  SPEAKER_MAC   the Bluetooth speaker's address, e.g. AA:BB:CC:11:22:33
"""
import asyncio
import io
import json
import os
import random
import socket
import struct
import subprocess
import tempfile
import time
import wave

import aiohttp

SERVER = os.environ.get("EYE_SERVER", "http://localhost:8085").rstrip("/")
SPEAKER = os.environ["SPEAKER_MAC"]
DEVICE = f"bluealsa:DEV={SPEAKER},PROFILE=a2dp"
KEEP_AWAKE_EVERY = 240     # seconds; the speaker turns itself off after ~30 min of silence
CHECK_EVERY = 15           # seconds between Bluetooth checks

last_sound = 0.0
connected = False
playing = asyncio.Lock()


def log(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


def bluetooth(*args, timeout=20):
    r = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def speaker_connected():
    return "Connected: yes" in bluetooth("info", SPEAKER)


def device_name():
    for line in bluetooth("info", SPEAKER).splitlines():
        if line.strip().startswith("Name:"):
            return line.split(":", 1)[1].strip()
    return SPEAKER


def quiet_wav(seconds=1.0, level=0.003):
    """A second of very quiet noise: keeps the speaker awake without being heard."""
    rate = 22050
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        amp = int(32767 * level)
        w.writeframes(b"".join(struct.pack("<h", random.randint(-amp, amp)) for _ in range(int(rate * seconds))))
    return out.getvalue()


async def play(audio, kind, on_start=None):
    """Play mp3 or wav bytes on the speaker. One at a time. on_start() is awaited just before the
    sound starts (the eye uses it to move in time with the words)."""
    global last_sound
    async with playing:
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "line.wav")
            if "mpeg" in kind:
                # Decode first, so playback is a plain file read (no decoding while Bluetooth streams).
                mp3 = os.path.join(tmp, "line.mp3")
                with open(mp3, "wb") as f:
                    f.write(audio)
                await asyncio.to_thread(subprocess.run, ["mpg123", "-q", "-w", wav, mp3],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            else:
                with open(wav, "wb") as f:
                    f.write(audio)
            if on_start:
                await on_start()
            # A half-second buffer smooths over hiccups on the Pi's shared Wi-Fi/Bluetooth radio.
            proc = await asyncio.create_subprocess_exec("aplay", "-q", "-D", DEVICE, "--buffer-time=500000", wav,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
        last_sound = time.time()
        return proc.returncode == 0


async def fetch_and_play(session, path, on_start=None):
    async with session.get(SERVER + path, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            log(f"couldn't download {path}: HTTP {r.status}")
            return False
        audio = await r.read()
        kind = r.headers.get("Content-Type", "")
    ok = await play(audio, kind, on_start)
    log(f"played {path}" if ok else f"couldn't play {path} (is the speaker connected?)")
    return ok


async def keep_speaker(send_status):
    """Reconnect the speaker when it's back, keep it awake, and report its state."""
    global connected
    quiet = quiet_wav()
    last_report = None
    while True:
        try:
            now_connected = await asyncio.to_thread(speaker_connected)
            if not now_connected:
                await asyncio.to_thread(bluetooth, "connect", SPEAKER)
                now_connected = await asyncio.to_thread(speaker_connected)
            if now_connected != connected:
                log("speaker connected" if now_connected else "speaker not connected (off or out of range?)")
            connected = now_connected
            if connected and time.time() - last_sound > KEEP_AWAKE_EVERY:
                await play(quiet, "audio/wav")
            report = (connected,)
            if report != last_report:
                last_report = report
                await send_status()
        except Exception as e:
            log(f"speaker check failed: {e}")
        await asyncio.sleep(CHECK_EVERY)


async def main():
    name = socket.gethostname()
    ws_holder = {}

    async def send_status():
        ws = ws_holder.get("ws")
        if ws is not None and not ws.closed:
            await ws.send_str(json.dumps({"type": "status", "name": name, "connected": connected,
                                          "device": await asyncio.to_thread(device_name)}))

    await asyncio.to_thread(bluetooth, "power", "on")
    asyncio.create_task(keep_speaker(send_status))
    while True:
        try:
            # The system's resolver, so names like my-mac.local (mDNS) work.
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(f"{SERVER}/voice/speaker?name={name}", heartbeat=10) as ws:
                    ws_holder["ws"] = ws
                    log(f"connected to the eye at {SERVER}")
                    await send_status()
                    async for m in ws:
                        msg = json.loads(m.data)
                        def started(n):
                            return lambda: ws.send_str(json.dumps({"type": "started", "n": n}))
                        if msg.get("type") == "play":
                            asyncio.create_task(fetch_and_play(session, f"/voice/audio/{msg['n']}", started(msg["n"])))
                        elif msg.get("type") == "test":
                            asyncio.create_task(fetch_and_play(session, f"/voice/preview?t={time.time()}",
                                                               started("preview")))
        except Exception as e:
            log(f"can't reach the eye at {SERVER} ({e.__class__.__name__}), retrying in 5s")
        ws_holder.pop("ws", None)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
