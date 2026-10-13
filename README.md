# Giant Eye

A giant eyeball in your window that watches people walk by.

A camera outside spots people, and a TV in the window shows a big glowing eye whose
gaze follows them. It blinks, glances around when nobody's there, rolls its eyes now
and then, does double-takes, and slowly shifts through the colors of the rainbow.
Everything runs on your own network; nothing goes to the cloud.

<p align="center">
  <img src="docs/house-at-dusk.jpg" alt="The eye glowing green in a round upstairs window at dusk" width="520">
</p>
<p align="center">
  <img src="docs/demo.gif" alt="The eye watching someone walk up to the house" width="480">
</p>

**[Watch the full video](docs/demo.mp4)**: walking up to the house while the eye follows.

Up close, the eye looks like this:

<p align="center"><img src="docs/eye.jpg" alt="The eye, drawn in the browser" width="480"></p>

## How it works

```
Security camera (RTSP) ──► tracker.py on a Mac
                              • finds people (YOLO), locks onto one person
                              • turns their position into "where the eye looks"
                              • serves the web pages
                                   │
          ┌────────────────────────┼─────────────────────────┐
          ▼                        ▼                         ▼
   Eye page on the TV       Phone control page        Camera debug view
   (Raspberry Pi kiosk)     /calibrate                /debug
```

The eye is drawn live in a web browser, so you can try it on any laptop or phone first.

## What you need

- **A camera with an RTSP stream.** Tested with a Wyze Cam v3 using Wyze's built-in RTSP
  (`rtsps://user:pass@<camera-ip>:322/stream0`). Mount it **outside**: behind window glass
  its night-vision light reflects and it sees the TV.
- **An Apple Silicon Mac to run the tracker.** Detection runs on its GPU at ~14 ms per
  frame, fast enough to check every frame the camera sends.
- **A screen in the window**, driven by a **Raspberry Pi 4** running Chromium in kiosk mode
  (or any device with a browser, like an old iPad, for testing).
- **A phone** for aiming the eye and changing settings while standing outside.

## Setup

### 1. Camera
Get the camera's RTSP address working in VLC first. Then:
```bash
cp camera_url.example.txt camera_url.txt   # put in your camera's address, username and password
chmod 600 camera_url.txt
```
`camera_url.txt` is in `.gitignore`. Never commit it.

### 2. Tracker on a Mac
macOS blocks background programs from `~/Documents`, so run it from your home folder:
```bash
git clone <this repo> ~/giant-eye && cd ~/giant-eye
cp camera_url.example.txt camera_url.txt     # edit it (see above)
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tracker.py                  # try it: open http://localhost:8080/
```
To run it all the time as a background service (starts at boot, restarts if it stops,
keeps the Mac awake, serves on port 8085):
```bash
./mac/install.sh                             # asks for your password
```
The service runs as **root** on purpose. macOS "Local Network" privacy blocks other
background programs from reaching the camera; you'd see `No route to host` in the log.
See Apple's TN3179. Log: `~/Library/Logs/giant-eye.log`.

**Updating:** replace the code files; the service notices and restarts itself.

### 3. The display (Raspberry Pi 4)
Raspberry Pi OS (64-bit, desktop) with auto-login. Copy the `pi/` folder to the Pi, then:
```bash
./install.sh http://<tracker-computer-ip>:8085/
```
Restart, and the Pi boots straight into the eye, fullscreen, with no mouse pointer.
It waits for the tracker if it boots first, and restarts the browser if it crashes.
Plug the TV into the micro-HDMI port next to the power port.

The Pi runs the screen at **720p** on purpose: a Pi 4 draws this eye at ~40 frames per
second at 720p but only ~16 at 1080p. The TV scales it up.

### 4. Aim and tune it from your phone
Open `http://<tracker-computer-ip>:8085/calibrate`:

<p align="center"><img src="docs/phone-controls.jpg" alt="The phone control page: camera view with the followed person in a yellow box, and the Aim / Settings / Test tabs" width="300"></p>

- **Aim:** stand in the yard, drag the eye until it looks right at you, and tap
  *Save this spot*. Do 4–6 spots (left, right, near, far). The camera usually isn't where
  the eye is, and this corrects for it. Also: *Ignore what's in the yellow box* for things
  the camera mistakes for a person (a mailbox, a statue).
- **Settings:** position and size (to line the eye up with your window, with an
  alignment guide), iris and pupil size, brightness, color drift, movement, blinking,
  moods and detection sensitivity. Changes show on the TV instantly and are saved.
- **Test:** make the eye sleep, startle, roll its eyes or do a double-take right now.

`/debug` shows what the camera sees: yellow box = person being followed.

## Tips from building it

- **Mounting the TV.** Ours hangs inside a closet behind the window. Two wood strips
  bolted to the TV's mounting holes and cord to hooks in the wall hold it against the
  window; *Move left / right* on the phone page lines the eye up with the glass.

  <img src="docs/tv-mount.jpg" alt="Back of the TV hung in a closet with two wood strips and cord" width="300">

- **Far-away people are the hard part.** At night, small figures far from the camera are
  grainy, and detection struggles. Bigger models and higher resolution *didn't* help on
  real night footage (the extra detail is mostly noise). What helped: lowering *Detection
  sensitivity* to ~0.25, aiming the camera so the sidewalk isn't at the very edge of the
  picture, and more light.
- **It sticks with one person** until they've been gone 1.5 s, and keeps tracking someone
  it's already following even at low confidence, so it doesn't lose them in the dark.
- **Wyze cameras sometimes ignore the first connection** while their Wi-Fi wakes up; the
  tracker just retries.

## Files

| File | What it does |
|---|---|
| `tracker.py` | Reads the camera, detects and follows people, serves the pages |
| `calibration.py` | Turns "where the camera sees you" into "where the eye looks" (from saved spots) |
| `settings.py` | The settings on the phone page, with allowed ranges |
| `web/index.html` | The eye (canvas, drawn live) |
| `web/config.js` | Starting values for the settings |
| `web/calibrate.html` | Phone page: aim, settings, test reactions |
| `mac/` | Background-service setup for a Mac |
| `pi/` | Kiosk setup for a Raspberry Pi |

Saved calibration, ignored spots and settings live in `data/` (not in git).

To try the eye without a camera, open the eye page with `?mouse` at the end of the
address: it follows your mouse or finger, and has buttons for the reactions.

## Credits

Person detection and tracking: [Ultralytics YOLO](https://github.com/ultralytics/ultralytics)
(YOLO11n) with ByteTrack. Built with a lot of help from Claude.

## License

[AGPL-3.0](LICENSE), the same license as the Ultralytics YOLO detector this project
depends on. You're free to use, change and share it; if you share or host a modified
version, share your source too.
