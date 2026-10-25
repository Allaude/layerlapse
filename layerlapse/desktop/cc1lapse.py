#!/usr/bin/env python3
"""
cc1lapse - automatic layer-synced timelapse for the Elegoo Centauri Carbon (CC1)

How it works
  1. Finds the printer on your LAN (UDP discovery) or uses --ip.
  2. Connects to the printer's local SDCP WebSocket (ws://<ip>:3030/websocket)
     and watches CurrentLayer / head position. No cloud, no firmware mods.
  3. On every layer change it grabs one frame from a camera:
       - "printer" : the CC1's built-in chamber camera (MJPEG on :3031)
       - any URL   : a phone running IP Webcam / DroidCam etc.
                     (http://PHONE_IP:8080/shot.jpg or an MJPEG /video URL)
  4. When the print finishes (or you press Ctrl+C) it stitches the frames
     into an MP4 with ffmpeg.

Optional "park" mode (--park X,Y): if you add the park G-code to your slicer
(see README), the app waits until the toolhead is actually parked before
snapping, so the head is out of the shot and every frame looks clean.

Usage
  python cc1lapse.py                         # auto-discover, printer camera
  python cc1lapse.py --ip 192.168.1.50
  python cc1lapse.py --camera http://192.168.1.77:8080/shot.jpg
  python cc1lapse.py --park 250,250          # wait for head to park
  python cc1lapse.py render runs/2026-10-24_1030_model --fps 30
"""

import argparse
import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import base64
import struct


# --------------------------------------------------------------------------
# Minimal WebSocket client (stdlib only, so there is nothing to pip install)
# --------------------------------------------------------------------------
class WSTimeout(Exception):
    pass


class MiniWebSocket:
    def __init__(self, host, port, path="/", timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake failed")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.split(b"\r\n")[0].decode(errors="ignore"))
        self.send_lock = threading.Lock()

    def settimeout(self, t):
        self.sock.settimeout(t)

    def _read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise WSTimeout()
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, data):
        hdr = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.send_lock:
            self.sock.sendall(hdr + mask + body)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv(self):
        message = b""
        while True:
            # Only commit to a frame once its 2-byte header is available,
            # so a timeout never leaves us mid-frame.
            b1, b2 = self._read(2)
            prev_timeout = self.sock.gettimeout()
            self.sock.settimeout(10)
            try:
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b2 & 0x80 else None
                data = self._read(n)
            finally:
                self.sock.settimeout(prev_timeout)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b1 & 0x0F
            if op == 0x9:      # ping
                self._send_frame(0xA, data)
                continue
            if op == 0xA:      # pong
                continue
            if op == 0x8:      # close
                raise ConnectionError("closed by printer")
            message += data
            if b1 & 0x80:      # FIN
                return message.decode("utf-8", "ignore")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        self.sock.close()

SDCP_DISCOVERY_PORT = 3000
SDCP_WS_PORT = 3030
CAM_PORT = 3031

# SDCP command numbers
CMD_STATUS = 0
CMD_ATTRIBUTES = 1
CMD_VIDEO_STREAM = 386
CMD_STATUS_PUSH = 512

# PrintInfo.Status values that mean the job is over
JOB_DONE_STATES = {8, 9}  # stopped, completed


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def discover(timeout=3.0):
    """Broadcast M99999 on UDP 3000; return list of (ip, mainboard_id, name)."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    try:
        sock.sendto(b"M99999", ("255.255.255.255", SDCP_DISCOVERY_PORT))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "ignore"))
            except ValueError:
                continue
            d = msg.get("Data", {})
            d = d.get("Attributes", d)  # some firmwares nest it
            ip = d.get("MainboardIP") or addr[0]
            found[ip] = (ip, d.get("MainboardID", ""), d.get("Name") or d.get("MachineName", "printer"))
    finally:
        sock.close()
    return list(found.values())


# --------------------------------------------------------------------------
# Printer connection (SDCP over WebSocket)
# --------------------------------------------------------------------------
class Printer:
    def __init__(self, ip, mainboard_id=""):
        self.ip = ip
        self.mainboard_id = mainboard_id
        self.ws = None
        self.lock = threading.Lock()
        self.state = {}             # merged latest Status dict
        self.video_url = None
        self.on_status = None       # callback(state)
        self._stop = threading.Event()

    # -- low level ---------------------------------------------------------
    def connect(self):
        self.ws = MiniWebSocket(self.ip, SDCP_WS_PORT, "/websocket")
        self.ws.settimeout(2)
        threading.Thread(target=self._reader, daemon=True).start()
        # ask for attributes (gives us MainboardID if discovery didn't)
        self.send(CMD_ATTRIBUTES)
        t0 = time.time()
        while not self.mainboard_id and time.time() - t0 < 5:
            time.sleep(0.1)
        self.send(CMD_STATUS_PUSH, {"TimePeriod": 500})
        self.send(CMD_STATUS)

    def send(self, cmd, data=None):
        payload = {
            "Id": "",
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": uuid.uuid4().hex,
                "MainboardID": self.mainboard_id,
                "TimeStamp": int(time.time() * 1000),
                "From": 1,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }
        self.ws.send(json.dumps(payload))

    def _reader(self):
        while not self._stop.is_set():
            try:
                raw = self.ws.recv()
            except WSTimeout:
                continue
            except Exception as e:
                if not self._stop.is_set():
                    log(f"Printer connection lost: {e}")
                    self._stop.set()
                return
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            self._handle(msg)

    def _handle(self, msg):
        if not self.mainboard_id:
            self.mainboard_id = msg.get("MainboardID") or msg.get("Data", {}).get("MainboardID", "")
        attrs = msg.get("Attributes")
        if attrs and not self.mainboard_id:
            self.mainboard_id = attrs.get("MainboardID", "")
        status = msg.get("Status")
        if status:
            _deep_merge(self.state, status)
            if self.on_status:
                self.on_status(self.state)
        # command responses: look for the video URL
        inner = msg.get("Data", {}).get("Data", {})
        if isinstance(inner, dict) and inner.get("VideoUrl"):
            v = inner["VideoUrl"]
            self.video_url = v if v.startswith("http") else f"http://{v}"

    def poll_forever(self, every=1.0):
        while not self._stop.is_set():
            try:
                self.send(CMD_STATUS)
            except Exception:
                return
            time.sleep(every)

    def enable_video(self):
        self.send(CMD_VIDEO_STREAM, {"Enable": 1})

    def close(self):
        self._stop.set()
        try:
            self.send(CMD_VIDEO_STREAM, {"Enable": 0})
        except Exception:
            pass
        try:
            self.ws.close()
        except Exception:
            pass

    @property
    def alive(self):
        return not self._stop.is_set()


def _deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def parse_coord(state):
    c = state.get("CurrenCoord") or state.get("CurrentCoord") or ""
    try:
        x, y, z = (float(p) for p in str(c).split(",")[:3])
        return x, y, z
    except ValueError:
        return None


def is_printing(state):
    cs = state.get("CurrentStatus")
    if isinstance(cs, list):
        return 1 in cs
    return cs == 1


# --------------------------------------------------------------------------
# Cameras
# --------------------------------------------------------------------------
def _fetch_jpeg(url, timeout=10):
    with urlopen(Request(url, headers={"User-Agent": "cc1lapse"}), timeout=timeout) as r:
        data = r.read()
    return data if data[:2] == b"\xff\xd8" else None


class Camera:
    """One camera, two jobs:
      - a live stream of frames (for box detection), fed to on_frame callbacks
      - grab(): one photo for the timelapse (full-res snapshot when available)

    stream_url : MJPEG stream (e.g. IP Webcam /video, CC1 :3031/video)
    poll_url   : single-JPEG URL polled a few times per second (fallback stream)
    still_url  : single-JPEG URL fetched fresh for each timelapse photo
    """

    def __init__(self, label, stream_url=None, poll_url=None, still_url=None, live=False):
        self.label = label
        self.stream_url = stream_url      # str or callable
        self.poll_url = poll_url
        self.still_url = still_url
        self.latest = None
        self.latest_t = 0
        self.callbacks = []
        self._stop = threading.Event()
        if live or not still_url:
            target = self._run_stream if stream_url else self._run_poll
            threading.Thread(target=target, daemon=True).start()

    def _push(self, jpg):
        self.latest = jpg
        self.latest_t = time.time()
        for cb in self.callbacks:
            try:
                cb(jpg, self.latest_t)
            except Exception as ex:
                log(f"frame analysis error: {ex}")

    def _run_stream(self):
        while not self._stop.is_set():
            url = self.stream_url() if callable(self.stream_url) else self.stream_url
            if not url:
                time.sleep(0.5)
                continue
            try:
                with urlopen(Request(url, headers={"User-Agent": "cc1lapse"}), timeout=10) as r:
                    buf = b""
                    while not self._stop.is_set():
                        chunk = r.read(16384)
                        if not chunk:
                            break
                        buf += chunk
                        while True:
                            s = buf.find(b"\xff\xd8")
                            e = buf.find(b"\xff\xd9", s + 2)
                            if s < 0 or e < 0:
                                break
                            self._push(buf[s:e + 2])
                            buf = buf[e + 2:]
                        if len(buf) > 8_000_000:
                            buf = b""
            except Exception as ex:
                log(f"{self.label} camera: {ex} (retrying)")
                time.sleep(2)

    def _run_poll(self):
        url = self.poll_url or self.still_url
        while not self._stop.is_set():
            try:
                jpg = _fetch_jpeg(url, timeout=5)
                if jpg:
                    self._push(jpg)
            except Exception as ex:
                log(f"{self.label} camera: {ex} (retrying)")
                time.sleep(2)
            time.sleep(0.2)

    def grab(self, newer_than=0.0, max_age=3.0):
        """Photo for the timelapse."""
        if self.still_url:
            wait = newer_than - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                return _fetch_jpeg(self.still_url)
            except Exception as ex:
                log(f"camera: {ex}")
                return None
        if self.latest and time.time() - self.latest_t <= max_age and self.latest_t >= newer_than:
            return self.latest
        return None

    def stop(self):
        self._stop.set()


def open_camera(spec, printer=None, live=False):
    """Turn the --camera value into a Camera.
    live=True also starts the frame stream (needed for box detection)."""
    if spec == "printer":
        if printer is None:
            raise SystemExit("--camera printer needs --ip")
        printer.enable_video()
        fallback = f"http://{printer.ip}:{CAM_PORT}/video"
        return Camera("printer", stream_url=lambda: printer.video_url or fallback, live=True)
    low = spec.lower().split("?")[0].rstrip("/")
    if low.endswith("/shot.jpg") or low.endswith("/video") or re.search(r":\d+$", low):
        # IP Webcam style: /video for the live stream, /shot.jpg for full-res photos
        base = re.sub(r"/(shot\.jpg|video)$", "", spec.split("?")[0].rstrip("/"), flags=re.I)
        return Camera("phone", stream_url=base + "/video", still_url=base + "/shot.jpg", live=live)
    if low.endswith((".jpg", ".jpeg")) or "snapshot" in low:
        return Camera("snapshot", poll_url=spec, still_url=spec, live=live)
    return Camera("stream", stream_url=spec, live=True)


# --------------------------------------------------------------------------
# Box detection: "is the toolhead parked inside the box I drew?"
# --------------------------------------------------------------------------
PROFILE_FILE = Path(__file__).with_name("boxes.json")
CROP = 48  # analysis size (pixels) of the box crop


def _pil():
    try:
        from PIL import Image, ImageChops, ImageStat
        return Image, ImageChops, ImageStat
    except ImportError:
        sys.exit("Box mode needs Pillow. Run:  pip install pillow")


class BoxDetector:
    """Watches one rectangle of the camera image.
    presence = how different the box looks from the empty background
    motion   = how much the box changed since the previous frame"""

    def __init__(self, box=None, ref=None, presence_thr=18.0, still_thr=4.0):
        self.Image, self.Chops, self.Stat = _pil()
        self.box = box                     # normalized (x0,y0,x1,y1) in RAW camera frame
        self.ref = ref                     # PIL "L" CROPxCROP image of the empty box
        self.presence_thr = presence_thr
        self.still_thr = still_thr
        self.prev = None
        self.crop = None
        self.presence = None
        self.motion = None
        self.still_since = 0.0
        self.last_t = 0.0
        self.frames = 0

    def set_box(self, box):
        self.box = box
        self.prev = self.crop = self.ref = None
        self.presence = self.motion = None
        self.still_since = 0.0

    def _diff(self, a, b):
        return self.Stat.Stat(self.Chops.difference(a, b)).mean[0]

    def feed(self, jpg, t):
        if not self.box or t - self.last_t < 0.07:   # analyse at most ~14 fps
            return
        self.last_t = t
        Image = self.Image
        im = Image.open(io.BytesIO(jpg))
        im.draft("L", (im.width // 4, im.height // 4))   # fast low-res JPEG decode
        im = im.convert("L")
        w, h = im.size
        x0, y0, x1, y1 = self.box
        crop = im.crop((int(x0 * w), int(y0 * h), max(int(x1 * w), int(x0 * w) + 2),
                        max(int(y1 * h), int(y0 * h) + 2))).resize((CROP, CROP))
        self.motion = self._diff(crop, self.prev) if self.prev is not None else None
        if self.motion is not None and self.motion <= self.still_thr:
            if not self.still_since:
                self.still_since = t
        else:
            self.still_since = 0.0
        if self.ref is not None:
            self.presence = self._diff(crop, self.ref)
            # follow slow lighting changes, only while the box is clearly empty and still
            if (self.presence < self.presence_thr * 0.5 and self.still_since
                    and t - self.still_since > 2.0):
                self.ref = Image.blend(self.ref, crop, 0.2)
        self.prev = crop
        self.crop = crop
        self.frames += 1

    def set_ref(self):
        if self.crop is not None:
            self.ref = self.crop.copy()
            return True
        return False

    def present(self):
        return self.presence is not None and self.presence >= self.presence_thr

    def still_for(self, now=None):
        return (now or time.time()) - self.still_since if self.still_since else 0.0

    def to_profile(self, rotate):
        return {"box": [round(v, 4) for v in self.box],
                "ref": base64.b64encode(self.ref.tobytes()).decode() if self.ref is not None else None,
                "presence_thr": self.presence_thr, "still_thr": self.still_thr, "rotate": rotate}

    @classmethod
    def from_profile(cls, prof):
        Image, _, _ = _pil()
        ref = None
        if prof.get("ref"):
            ref = Image.frombytes("L", (CROP, CROP), base64.b64decode(prof["ref"]))
        return cls(tuple(prof["box"]), ref, prof.get("presence_thr", 18.0), prof.get("still_thr", 4.0))


def load_profiles():
    try:
        return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_profile(name, data):
    profs = load_profiles()
    profs[name] = data
    PROFILE_FILE.write_text(json.dumps(profs, indent=1), encoding="utf-8")


# rotation helpers: the setup page shows the picture upright (rotated clockwise
# by `rot`), but boxes are stored in raw camera coordinates.
def disp_to_raw(u, v, rot):
    return {0: (u, v), 90: (v, 1 - u), 180: (1 - u, 1 - v), 270: (1 - v, u)}[rot % 360]


def raw_to_disp(x, y, rot):
    return {0: (x, y), 90: (1 - y, x), 180: (1 - x, 1 - y), 270: (y, 1 - x)}[rot % 360]


def rect_convert(r, rot, fn):
    (a, b), (c, d) = fn(r[0], r[1], rot), fn(r[2], r[3], rot)
    return (min(a, c), min(b, d), max(a, c), max(b, d))


# --------------------------------------------------------------------------
# Video rendering
# --------------------------------------------------------------------------
# clockwise rotation applied when making the video (frames stay untouched)
ROTATE_FILTERS = {0: "", 90: "transpose=1,", 180: "hflip,vflip,", 270: "transpose=2,"}


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def render(folder, fps=30, hold_last=2.0, out=None, rotate=0):
    folder = Path(folder)
    frames = sorted(folder.glob("frame_*.jpg"))
    if not frames:
        log("No frames to render.")
        return None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("ffmpeg not found. Install it or run: pip install imageio-ffmpeg")
        return None
    out = Path(out) if out else folder / f"{folder.name}.mp4"
    # Concat list lets us hold the final frame (the finished print) a bit longer
    lst = folder / "_frames.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in frames:
            f.write(f"file '{p.name}'\nduration {1/fps:.6f}\n")
        f.write(f"file '{frames[-1].name}'\nduration {hold_last:.3f}\n")
        f.write(f"file '{frames[-1].name}'\n")
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", lst.name,
        "-vf", ROTATE_FILTERS.get(rotate % 360, "") + f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-movflags", "+faststart", out.resolve().as_posix(),
    ]
    log(f"Rendering {len(frames)} frames at {fps} fps ...")
    r = subprocess.run(cmd, cwd=folder)
    lst.unlink(missing_ok=True)
    if r.returncode == 0:
        secs = len(frames) / fps + hold_last
        log(f"Done: {out}  (~{secs:.1f}s)")
        return out
    log("ffmpeg failed.")
    return None


# --------------------------------------------------------------------------
# Setup page: draw the box in your browser
# --------------------------------------------------------------------------
SETUP_HTML = r"""<!doctype html>
<html lang="tl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>cc1lapse setup</title>
<style>
:root{--bg:#111316;--panel:#1b1e23;--line:#2c3138;--text:#e9ecef;--dim:#9aa3ad;--ok:#3ddc84;--warn:#ffb020;--accent:#ff6a2b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline}
header b{font-size:17px}header span{color:var(--dim)}
main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px;padding:20px;max-width:1500px;margin:auto}
@media(max-width:900px){main{grid-template-columns:1fr}}
#wrap{position:relative;background:#000;border-radius:10px;overflow:hidden;touch-action:none;user-select:none;width:fit-content;max-width:100%;margin:0 auto;cursor:crosshair}
#wrap img{display:block;max-width:100%;max-height:calc(100vh - 140px);width:auto;height:auto;pointer-events:none;-webkit-user-drag:none}
#box{position:absolute;border:3px solid var(--warn);background:rgba(255,176,32,.12);display:none;pointer-events:none;border-radius:4px}
#box.on{border-color:var(--ok);background:rgba(61,220,132,.18)}
#tag{position:absolute;left:6px;top:6px;font:600 12px system-ui;background:rgba(0,0,0,.6);padding:3px 7px;border-radius:5px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:14px}
.step{display:flex;gap:10px}.n{flex:none;width:24px;height:24px;border-radius:50%;background:var(--line);display:grid;place-items:center;font-weight:700;font-size:13px}
.step.done .n{background:var(--ok);color:#000}
.step p{margin:0;color:var(--dim)}.step b{color:var(--text)}
button{width:100%;padding:11px;border:0;border-radius:8px;font:600 15px system-ui;cursor:pointer;background:var(--line);color:var(--text)}
button.primary{background:var(--accent);color:#fff}button:disabled{opacity:.4;cursor:default}
.meter{height:12px;background:#000;border-radius:6px;position:relative;overflow:hidden}
.meter i{position:absolute;inset:0 auto 0 0;background:var(--warn);width:0}
.meter.on i{background:var(--ok)}
.meter u{position:absolute;top:-2px;bottom:-2px;width:2px;background:#fff}
.status{font-size:20px;font-weight:700}
.row{display:flex;justify-content:space-between;color:var(--dim);font-size:13px}
input[type=range]{width:100%}
#msg{min-height:20px;color:var(--ok);font-weight:600}
</style></head><body>
<header><b>cc1lapse setup</b><span id="hdr"></span></header>
<main>
<div><div id="wrap"><img id="img" alt="camera" draggable="false"><div id="box"><span id="tag"></span></div></div>
<p style="color:var(--dim);margin:8px 2px;text-align:center">I-drag sa larawan para iguhit ang box kung saan nagpa-park ang toolhead.</p></div>
<div class="panel">
 <div class="step" id="s1"><div class="n">1</div><p><b>Iguhit ang box</b> sa lugar ng toolhead kapag naka-park (likod-kanan). Medyo maliit lang, sakto sa katawan ng head.</p></div>
 <div class="step" id="s2"><div class="n">2</div><p><b>Walang head sa box?</b> Saka i-click: </p></div>
 <button id="ref" disabled>Kunin ang background (walang head)</button>
 <div class="step" id="s3"><div class="n">3</div><p><b>Subukan:</b> sa printer screen, igalaw ang head papunta sa park spot. Dapat maging <span style="color:var(--ok)">berde</span>.</p></div>
 <div><div class="status" id="st">—</div>
  <div class="meter" id="meter"><i id="bar"></i><u id="thrmark"></u></div>
  <div class="row"><span>Presence: <b id="pv">—</b></span><span id="mv">—</span></div></div>
 <label class="row" for="thr"><span>Sensitivity (mas mababa = mas sensitive)</span><b id="tv"></b></label>
 <input type="range" id="thr" min="4" max="60" step="1">
 <div class="step" id="s4"><div class="n">4</div><p><b>I-save</b>, tapos isara ang page na ito at ang cmd window.</p></div>
 <button class="primary" id="save" disabled>I-save ang setup</button>
 <div id="msg"></div>
</div></main>
<script>
const $=id=>document.getElementById(id);let box=null,drag=null,hasRef=false;
const wrap=$('wrap'),img=$('img'),bx=$('box');
function pos(e){const r=img.getBoundingClientRect();return[Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))]}
function draw(){if(!box){bx.style.display='none';return}const[a,b,c,d]=box;Object.assign(bx.style,{display:'block',left:a*100+'%',top:b*100+'%',width:(c-a)*100+'%',height:(d-b)*100+'%'})}
wrap.addEventListener('dragstart',e=>e.preventDefault());
wrap.addEventListener('pointerdown',e=>{e.preventDefault();drag=pos(e);box=[...drag,...drag];draw();wrap.setPointerCapture(e.pointerId)});
wrap.addEventListener('pointermove',e=>{if(!drag)return;const p=pos(e);box=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.max(drag[0],p[0]),Math.max(drag[1],p[1])];draw()});
wrap.addEventListener('pointerup',async()=>{if(!drag)return;drag=null;if(box[2]-box[0]<0.02||box[3]-box[1]<0.02){box=null;draw();return}
 hasRef=false;await post('/box',{box});refresh()});
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json()}
$('ref').onclick=async()=>{const r=await post('/ref');hasRef=r.ok;$('msg').textContent=r.ok?'Nakuha ang background.':'Wala pang frame, subukan ulit.';refresh()};
$('thr').oninput=e=>{$('tv').textContent=e.target.value;post('/thr',{presence:+e.target.value})};
$('save').onclick=async()=>{const r=await post('/save');$('msg').textContent=r.ok?('Na-save bilang "'+r.name+'". Pwede mo nang isara ito.'):'Hindi na-save: '+r.error};
function refresh(){$('s1').classList.toggle('done',!!box);$('ref').disabled=!box;$('s2').classList.toggle('done',hasRef);$('save').disabled=!(box&&hasRef)}
function frame(){const n=new Image();n.onload=()=>{img.src=n.src;setTimeout(frame,200)};n.onerror=()=>setTimeout(frame,1000);n.src='/frame.jpg?'+Date.now()}
async function poll(){try{const s=await (await fetch('/state')).json();
 $('hdr').textContent=s.name+' · rotate '+s.rotate+'° · '+s.fps+' fps analysis';
 if(box===null&&s.box&&!drag){box=s.box;hasRef=s.has_ref;draw();refresh()}
 const t=s.thr;if(!$('thr').matches(':active')){$('thr').value=t;$('tv').textContent=t}
 $('thrmark').style.left=Math.min(100,t/80*100)+'%';
 if(s.presence==null){$('st').textContent=box?(hasRef?'Naghihintay…':'Kunin ang background'):'Iguhit ang box';$('st').style.color='var(--dim)';$('bar').style.width='0';$('pv').textContent='—'}
 else{$('pv').textContent=s.presence.toFixed(1);$('bar').style.width=Math.min(100,s.presence/80*100)+'%';
  $('meter').classList.toggle('on',s.present);bx.classList.toggle('on',s.present);
  $('st').textContent=s.present?(s.still?'Head nasa box ✓ (kukunan)':'Head nasa box, gumagalaw pa'):'Walang head sa box';
  $('st').style.color=s.present?'var(--ok)':'var(--text)';$('tag').textContent=s.present?'HEAD':''}
 $('mv').textContent=s.motion==null?'':(s.still?'Still':'Gumagalaw')+' ('+s.motion.toFixed(1)+')';
}catch(e){}setTimeout(poll,250)}
frame();poll();
</script></body></html>"""


def setup(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import webbrowser
    Image, _, _ = _pil()
    rot = args.rotate or 0
    prof = load_profiles().get(args.name)
    det = BoxDetector.from_profile(prof) if prof else BoxDetector()
    if prof and args.rotate is None:
        rot = prof.get("rotate", 0)
    cam = open_camera(args.camera, None, live=True)
    cam.callbacks.append(det.feed)
    fps_count = [0, time.time(), 0.0]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.startswith("/frame.jpg"):
                jpg = cam.latest
                if not jpg:
                    self.send_response(503)
                    self.end_headers()
                    return
                im = Image.open(io.BytesIO(jpg))
                im.draft("RGB", (1280, 1280))
                im = im.convert("RGB")
                if rot:
                    im = im.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180,
                                       270: Image.ROTATE_90}[rot])
                im.thumbnail((1280, 1280))
                out = io.BytesIO()
                im.save(out, "JPEG", quality=80)
                b = out.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif self.path.startswith("/state"):
                now = time.time()
                if now - fps_count[1] >= 2:
                    fps_count[2] = (det.frames - fps_count[0]) / (now - fps_count[1])
                    fps_count[0], fps_count[1] = det.frames, now
                self._json({
                    "name": args.name, "rotate": rot, "fps": round(fps_count[2], 1),
                    "box": rect_convert(det.box, rot, raw_to_disp) if det.box else None,
                    "has_ref": det.ref is not None, "thr": det.presence_thr,
                    "presence": det.presence, "present": det.present(),
                    "motion": det.motion, "still": det.still_for() >= args.settle,
                })
            else:
                b = SETUP_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/box":
                det.set_box(rect_convert(body["box"], rot, disp_to_raw))
                self._json({"ok": True})
            elif self.path == "/ref":
                self._json({"ok": det.set_ref()})
            elif self.path == "/thr":
                det.presence_thr = float(body.get("presence", det.presence_thr))
                self._json({"ok": True})
            elif self.path == "/save":
                if not det.box or det.ref is None:
                    self._json({"ok": False, "error": "kulang pa ang box o background"})
                    return
                save_profile(args.name, det.to_profile(rot))
                log(f'Saved setup "{args.name}" to {PROFILE_FILE.name}')
                self._json({"ok": True, "name": args.name})
            else:
                self._json({"ok": False}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    url = f"http://127.0.0.1:{args.port}/"
    log(f'Setup "{args.name}" running. Open {url} in your browser (Ctrl+C here to quit).')
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()


# --------------------------------------------------------------------------
# Main capture loop
# --------------------------------------------------------------------------
class Capture:
    def __init__(self, args, printer, camera, detector=None):
        self.a = args
        self.p = printer
        self.cam = camera
        self.det = detector
        self.folder = None
        self.count = 0
        self.last_layer = None
        self.pending_layer = None
        self.pending_since = 0
        self.seen_printing = False
        self.end_since = 0
        self.missed = 0
        self.dbg_last = None
        self.dbg_t = 0
        self.done = threading.Event()

    def start_job(self, state):
        name = state.get("PrintInfo", {}).get("Filename") or "print"
        name = Path(name).stem[:40].replace(" ", "_")
        self.folder = Path(self.a.out) / f"{datetime.now():%Y-%m-%d_%H%M}_{name}"
        self.folder.mkdir(parents=True, exist_ok=True)
        log(f"Print started: {name} -> {self.folder}")
        self.seen_printing = True

    def save(self, layer, jpg):
        self.count += 1
        p = self.folder / f"frame_{self.count:05d}.jpg"
        p.write_bytes(jpg)
        total = self.p.state.get("PrintInfo", {}).get("TotalLayer", "?")
        log(f"Layer {layer}/{total}  ->  {p.name}")

    def _new_layer(self, layer):
        if isinstance(layer, int) and layer > 0 and layer != self.last_layer:
            if self.pending_layer is not None and self.det:
                self.missed += 1
                log(f"Layer {self.pending_layer}: missed - head never seen parked in the box")
            self.last_layer = layer
            self.pending_layer = layer
            self.pending_since = time.time()

    # ---- box mode: camera decides when the head is parked ------------------
    def _tick_box(self):
        if self.pending_layer is None:
            return
        now = time.time()
        if self.det.present() and self.det.still_for(now) >= self.a.settle:
            jpg = self.cam.grab()
            if jpg:
                self.save(self.pending_layer, jpg)
                self.pending_layer = None
        elif now - self.pending_since > self.a.box_timeout:
            self.missed += 1
            log(f"Layer {self.pending_layer}: missed - no parked head in the box after "
                f"{self.a.box_timeout:.0f}s (check the box / park G-code)")
            self.pending_layer = None

    # ---- plain mode: snap on every layer change ----------------------------
    def _tick_layer(self):
        if self.pending_layer is None:
            return
        waited = time.time() - self.pending_since
        if waited < self.a.delay:
            return
        jpg = self.cam.grab(newer_than=self.pending_since + self.a.delay)
        if jpg:
            self.save(self.pending_layer, jpg)
            self.pending_layer = None
        elif waited > self.a.delay + 5:
            log(f"Layer {self.pending_layer}: no camera frame, skipped")
            self.pending_layer = None

    def tick(self):
        st = self.p.state
        info = st.get("PrintInfo", {})
        printing = is_printing(st)
        layer = info.get("CurrentLayer")

        if printing and not self.seen_printing:
            self.start_job(st)
        if not self.seen_printing:
            return

        if self.a.debug:
            now = time.time()
            key = (layer, st.get("CurrenCoord"))
            if key != self.dbg_last or now - self.dbg_t > 1:
                self.dbg_last, self.dbg_t = key, now
                extra = ""
                if self.det and self.det.presence is not None:
                    extra = (f" box: presence={self.det.presence:.1f}/{self.det.presence_thr:.0f}"
                             f" still={self.det.still_for(now):.1f}s")
                log(f"[debug] layer={layer} coord={key[1]}{extra}")

        self._new_layer(layer)
        if self.det:
            self._tick_box()
        else:
            self._tick_layer()

        # Job finished? (debounced so a stale status can't end the run early)
        status = info.get("Status")
        if (not printing) or status in JOB_DONE_STATES:
            if not self.end_since:
                self.end_since = time.time()
                return
            if time.time() - self.end_since < 3:
                return
        else:
            self.end_since = 0
            return

        # one extra "finished" frame after the head moves away
        time.sleep(self.a.final_delay)
        jpg = self.cam.grab()
        if jpg and self.folder:
            self.save("final", jpg)
        log(f"Print finished. {self.count} frames saved, {self.missed} layers missed.")
        self.done.set()


def run(args):
    det = None
    rotate = args.rotate
    if args.box:
        prof = load_profiles().get(args.box)
        if not prof or not prof.get("ref"):
            sys.exit(f'No saved box called "{args.box}". Run setup first, e.g.:\n'
                     f'  python cc1lapse.py setup --camera {args.camera} --name {args.box}')
        det = BoxDetector.from_profile(prof)
        if rotate is None:
            rotate = prof.get("rotate", 0)
    rotate = rotate or 0

    if args.ip:
        ip, mbid = args.ip, ""
    else:
        log("Searching for printer on the network ...")
        printers = discover()
        if not printers:
            sys.exit("No printer found. Use --ip <printer ip> (see printer screen > Settings > Network).")
        ip, mbid, name = printers[0]
        log(f"Found {name} at {ip}")

    p = Printer(ip, mbid)
    p.connect()
    threading.Thread(target=p.poll_forever, daemon=True).start()
    cam = open_camera(args.camera, p, live=det is not None)
    if det:
        cam.callbacks.append(det.feed)
    cap = Capture(args, p, cam, det)
    mode = f'box "{args.box}"' if det else "every layer change"
    log(f"Connected. Camera: {args.camera}. Trigger: {mode}. "
        f"Waiting for a print to start ... (Ctrl+C to stop)")

    try:
        while not cap.done.is_set() and p.alive:
            cap.tick()
            time.sleep(0.05)
    except KeyboardInterrupt:
        log("Stopped by user.")
    finally:
        cam.stop()
        p.close()

    if cap.folder and cap.count:
        render(cap.folder, args.fps, args.hold, rotate=rotate)


def main():
    ap = argparse.ArgumentParser(description="Layer-synced timelapse for Elegoo Centauri Carbon")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("render", help="re-render an existing frames folder")
    r.add_argument("folder")
    r.add_argument("--fps", type=int, default=30)
    r.add_argument("--hold", type=float, default=2.0, help="seconds to hold the last frame")
    r.add_argument("-o", "--output")
    r.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270], help="rotate video clockwise")

    s = sub.add_parser("setup", help="draw the park box in your browser")
    s.add_argument("--camera", required=True, help="phone camera URL, e.g. http://192.168.1.77:8080")
    s.add_argument("--name", default="front", help="name for this setup, e.g. front / top")
    s.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270],
                   help="rotate the picture clockwise (also used for the video)")
    s.add_argument("--settle", type=float, default=0.4)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    ap.add_argument("--ip", help="printer IP (skip auto-discovery)")
    ap.add_argument("--camera", default="printer",
                    help='"printer" (built-in cam) or a phone camera URL, e.g. http://192.168.1.77:8080/shot.jpg')
    ap.add_argument("--box", help="name of a saved box setup (see: setup). Snaps when the head is parked in it")
    ap.add_argument("--box-timeout", type=float, default=20.0,
                    help="give up on a layer if no parked head is seen within this many seconds")
    ap.add_argument("--settle", type=float, default=0.4,
                    help="head must be still in the box this long before the photo")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="(no-box mode) seconds to wait after a layer change before snapping")
    ap.add_argument("--out", default="runs", help="folder for captures")
    ap.add_argument("--debug", action="store_true", help="log printer + box detection values")
    ap.add_argument("--final-delay", type=float, default=5.0, help="wait before the 'finished' frame")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--hold", type=float, default=2.0, help="seconds to hold the last frame")
    ap.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270],
                    help="rotate video clockwise (default: from the box setup, else 0)")

    args = ap.parse_args()
    if args.cmd == "render":
        render(args.folder, args.fps, args.hold, args.output, args.rotate)
    elif args.cmd == "setup":
        setup(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
