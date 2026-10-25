# cc1lapse — automatic layer timelapse for the Elegoo Centauri Carbon

Snaps one photo per layer while your CC1 prints, then turns them into an MP4
automatically. Works over your local Wi‑Fi using the printer's own LAN
connection (no cloud, no firmware mods, no extra hardware).

```
PC running cc1lapse  ──Wi‑Fi──►  CC1 (reports "layer 37 of 210", head position)
        │
        └──Wi‑Fi──►  camera: CC1 built‑in cam  OR  your phone (IP Webcam app)
```

## 1. Install (once)

1. Install **Python 3.10+** from python.org (tick *"Add Python to PATH"*).
2. Install **ffmpeg** (Windows: `winget install ffmpeg`), or run
   `pip install imageio-ffmpeg` and the app will use that instead.
3. Put `cc1lapse.py` and `run.bat` in one folder.

That's it — no other packages needed.

## 2. Run it

Start it *before* (or during) the print. It waits for the print to start,
captures every layer, and renders the video when the print ends.

```bash
python cc1lapse.py                      # finds the printer automatically
python cc1lapse.py --ip 192.168.1.50    # if auto-find fails (IP is on the printer screen > Network)
```

Frames and the video are saved to `runs/<date>_<file name>/`.
Press **Ctrl+C** anytime to stop early — it still renders what it has.

Re-render with a different speed:
```bash
python cc1lapse.py render runs/2026-10-24_1030_Lemon_Clicker --fps 24
```

## 3. Camera choices

| Camera | Command | Quality |
|---|---|---|
| CC1 built-in camera (default) | `--camera printer` | OK, low‑res |
| Android phone with **IP Webcam** app | `--camera http://PHONE_IP:8080/shot.jpg` | Best (full photo res) |
| Any MJPEG stream (DroidCam, etc.) | `--camera http://PHONE_IP:4747/video` | Good |

Phone tips: mount it on a tripod, lock focus/exposure in the app, keep it
plugged in, and put the phone on the same Wi‑Fi as the PC.

Note: the CC1 only serves one camera viewer at a time; close the Elegoo app's
live view if frames come back empty.

## 4. Clean shots: the box trigger (recommended)

The CC1 reports where the head is *going*, not where it *is*, and only every
1–2 s, so its position can't time the photo. Instead the camera watches a box
you draw: when the toolhead is parked inside it and holding still, it snaps.

1. Add the park G-code (`park_gcode.txt`) to Elegoo Slicer → Printer settings →
   Machine G-code → **Layer change G-code**, then re-slice.
2. Once: `install_once.bat` (installs Pillow).
3. Run `setup_front.bat` (or `setup_top.bat`). A page opens in your browser:
   draw the box where the head sits when parked, click *Kunin ang background*
   while the head is NOT in the box, check it turns green when the head is
   there, then *I-save*.
4. Print with `run_front.bat` / `run_top.bat`.

Redo the setup whenever you move the phone.

## All options

| Option | Default | Meaning |
|---|---|---|
| `--ip` | auto | Printer IP |
| `--camera` | `printer` | `printer` or a camera URL |
| `--box NAME` | off | Use a saved box setup (front / top) |
| `--box-timeout` | 20 | Skip a layer if no parked head is seen in time |
| `--debug` | off | Show every layer/position update from the printer |
| `--delay` | 0 | Extra wait after each layer change (no-box mode) |
| `--settle` | 0.4 | Head must be still in the box this long before the photo |
| `--fps` | 30 | Video frame rate |
| `--hold` | 2 | Seconds to show the finished print at the end |
| `--out` | `runs` | Output folder |
