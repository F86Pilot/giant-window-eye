"""Measure how far the camera picture lags behind real life.

Run it on the Mac while the eye is running:
    ~/giant-eye/.venv/bin/python camera_delay.py
then open http://<this Mac>:8086 on a phone, tap Start, and hold the phone's screen toward the camera.

The phone flashes black/white in a pseudo-random pattern timed by the Mac's clock (the page syncs to
it first, so the phone's own clock doesn't matter). This script watches the eye's debug picture
(/debug.mjpg, the same frames the eye tracks people in), notes when each frame arrives, finds the
patch of picture that flashes in that pattern, and slides the pattern in time until it lines up.
The slide is the delay: camera + network + decoding + person detection, i.e. how late the eye sees things.
"""
import argparse
import asyncio
import time

import cv2
import numpy as np
from aiohttp import web, ClientSession, ClientTimeout

SLOT = 0.25          # seconds per black/white square of the pattern
GRID = (80, 45)      # the debug picture is averaged into this many patches (8x8 pixels each at 640x360)
MAX_DELAY = 15.0     # longest delay searched for, seconds
FLASH_SECONDS = 40   # how long the phone flashes


def bit(slot):
    """Pattern square number `slot`: 1 = white. Same integer mix as the page's JavaScript."""
    x = slot & 0x7FFFFFFF
    x ^= x >> 16
    x = (x * 0x45D9F3B) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x45D9F3B) & 0xFFFFFFFF
    x ^= x >> 16
    return x & 1


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera delay test</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#888;font:18px -apple-system,system-ui,sans-serif}
  #ui{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
      gap:16px;padding:16px;text-align:center}
  button{font:inherit;font-size:24px;padding:16px 40px;border-radius:12px;border:0;background:#2a6;color:#fff}
  #msg{max-width:30em;line-height:1.4}
  #tag{position:fixed;left:8px;top:8px;font-size:14px;color:#777;mix-blend-mode:difference}
</style></head><body>
<div id="ui"><div id="msg">Turn screen brightness all the way up. Tap Start, then hold the screen toward
the camera for FLASH_SECONDS seconds (you can stand where it can see you; closer is better).</div>
<button id="go">Start</button></div>
<div id="tag"></div>
<script>
const SLOT = SLOT_S, FLASH = FLASH_SECONDS;
function bit(slot) {
  let x = slot & 0x7FFFFFFF;
  x ^= x >>> 16; x = Math.imul(x, 0x45D9F3B) >>> 0;
  x ^= x >>> 16; x = Math.imul(x, 0x45D9F3B) >>> 0;
  x ^= x >>> 16; return x & 1;
}
let base = 0;   // Mac time (s) = performance.now()/1000 + base
const mac = () => performance.now() / 1000 + base;
async function sync() {
  let best = null;
  for (let i = 0; i < 15; i++) {
    const p0 = performance.now();
    const t = await (await fetch("/time", {cache: "no-store"})).json();
    const p1 = performance.now();
    if (!best || p1 - p0 < best.rtt) best = {rtt: p1 - p0, base: t.now - (p0 + p1) / 2000};
  }
  base = best.base;
  return best.rtt;
}
const ui = document.getElementById("ui"), msg = document.getElementById("msg"), tag = document.getElementById("tag");
document.getElementById("go").onclick = async () => {
  msg.textContent = "Syncing with the Mac's clock..."; document.getElementById("go").remove();
  const rtt = await sync();
  try { await navigator.wakeLock.request("screen"); } catch (e) {}
  const start = Math.ceil(mac() + 1), end = start + FLASH;
  ui.style.display = "none";
  function frame() {
    const t = mac();
    if (t < end) {
      document.body.style.background = t < start ? "#000" : (bit(Math.floor(t / SLOT)) ? "#fff" : "#000");
      tag.textContent = t < start ? "" : Math.ceil(end - t) + " s";
      requestAnimationFrame(frame);
    } else {
      document.body.style.background = "#000"; tag.textContent = "";
      ui.style.display = "flex";
      msg.textContent = "Done flashing. Working it out (about 15 s)...";
      fetch(`/result?start=${start}&end=${end}&rtt=${rtt.toFixed(1)}`).then(r => r.text())
        .then(s => msg.innerHTML = s).catch(e => msg.textContent = "Couldn't get the result: " + e);
    }
  }
  requestAnimationFrame(frame);
};
</script></body></html>
""".replace("FLASH_SECONDS", str(FLASH_SECONDS)).replace("SLOT_S", str(SLOT))


class Watcher:
    """Reads the eye's debug picture and keeps (arrival time, patch brightnesses) for each frame."""

    def __init__(self, url):
        self.url = url
        self.times, self.cells = [], []
        self.status = "not connected yet"

    async def run(self):
        while True:
            try:
                async with ClientSession(timeout=ClientTimeout(total=None, sock_read=10)) as session:
                    async with session.get(self.url) as resp:
                        self.status = "watching"
                        buf = b""
                        async for chunk in resp.content.iter_any():
                            buf += chunk
                            while True:
                                a = buf.find(b"\xff\xd8")
                                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                                if b < 0:
                                    break
                                jpg, buf = buf[a:b + 2], buf[b + 2:]
                                self.add(time.time(), jpg)
            except Exception as e:
                self.status = f"can't read {self.url}: {e!r}"
                print(self.status)
                await asyncio.sleep(2)

    def add(self, t, jpg):
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return
        self.times.append(t)
        self.cells.append(cv2.resize(img, GRID, interpolation=cv2.INTER_AREA).astype(np.float32).ravel())
        cutoff = t - (FLASH_SECONDS + MAX_DELAY + 30)
        while self.times and self.times[0] < cutoff:
            self.times.pop(0)
            self.cells.pop(0)


def correlate(times, cells, start, end, lags):
    """For each lag: how well each patch's brightness matches the pattern shifted by that lag (Pearson r).
    Returns an array (lags x patches)."""
    out = np.full((len(lags), cells.shape[1]), np.nan, np.float32)
    for i, lag in enumerate(lags):
        shown = times - lag                       # when (Mac time) the picture in each frame happened
        ok = (shown >= start) & (shown < end)
        if ok.sum() < 40:
            continue
        e = np.array([bit(int(np.floor(s / SLOT))) for s in shown[ok]], np.float32)
        e -= e.mean()
        c = cells[ok] - cells[ok].mean(axis=0)
        denom = np.sqrt((c ** 2).sum(axis=0) * (e ** 2).sum()) + 1e-6
        out[i] = (e @ c) / denom
    return out


def analyse(watcher, start, end):
    times = np.array(watcher.times)
    cells = np.array(watcher.cells)
    keep = (times >= start) & (times < end + MAX_DELAY)
    times, cells = times[keep], cells[keep]
    if len(times) < 60:
        return None, f"Only {len(times)} camera frames arrived while flashing ({watcher.status})."
    fps = (len(times) - 1) / (times[-1] - times[0])
    coarse = np.arange(0, MAX_DELAY, 0.05)
    r = correlate(times, cells, start, end, coarse)
    li, ci = np.unravel_index(np.nanargmax(r), r.shape)
    # Use every patch that matches nearly as well (the phone covers several), refine the lag finely.
    best = r[li]
    patches = np.where(best >= 0.7 * best[ci])[0]
    series = cells[:, patches].mean(axis=1, keepdims=True)
    fine = np.arange(max(0, coarse[li] - 0.3), coarse[li] + 0.3, 0.005)
    rf = correlate(times, series, start, end, fine)[:, 0]
    lag = fine[np.nanargmax(rf)]
    # How clear-cut is it: best match vs the best match at any lag at least 0.5 s away.
    rs = correlate(times, series, start, end, coarse)[:, 0]
    other = np.nanmax(np.where(np.abs(coarse - lag) >= 0.5, rs, np.nan))
    y, x = divmod(ci, GRID[0])
    info = {"delay": round(float(lag), 2), "match": round(float(np.nanmax(rf)), 2),
            "next_best": round(float(other), 2), "fps": round(fps, 1), "frames": len(times),
            "patches": len(patches), "where": (round((x + 0.5) / GRID[0], 2), round((y + 0.5) / GRID[1], 2))}
    return info, None


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--eye", default="http://localhost:8085", help="the eye's web address")
    ap.add_argument("--port", type=int, default=8086)
    args = ap.parse_args()
    watcher = Watcher(args.eye.rstrip("/") + "/debug.mjpg")

    async def page(request):
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def now(request):
        return web.json_response({"now": time.time()})

    async def result(request):
        start, end = float(request.query["start"]), float(request.query["end"])
        wait = end + MAX_DELAY - time.time()
        if wait > 0:
            await asyncio.sleep(wait)   # let the late frames arrive
        info, problem = await asyncio.to_thread(analyse, watcher, start, end)
        if problem:
            print(problem)
            return web.Response(text=problem)
        print(f"Camera delay: {info}")
        sure = info["match"] >= 0.5 and info["match"] - info["next_best"] >= 0.25
        text = (f"<b style='font-size:32px;color:#fff'>{info['delay']:.2f} s</b><br>"
                f"{'Clear result.' if sure else 'Weak match: the camera may not have seen the screen well. Try closer or darker.'}"
                f"<br><small>match {info['match']} (next best {info['next_best']}), {info['frames']} frames at "
                f"{info['fps']} fps, phone seen at x={info['where'][0]} y={info['where'][1]}, "
                f"clock sync round trip {request.query.get('rtt')} ms</small>")
        return web.Response(text=text)

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/time", now)
    app.router.add_get("/result", result)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.port).start()
    print(f"Open http://<this Mac>:{args.port} on the phone. Watching {watcher.url}")
    await watcher.run()


if __name__ == "__main__":
    asyncio.run(main())
