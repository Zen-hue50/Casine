#!/usr/bin/env python3
"""casine - audio-reactive sine waves for the Kitty terminal.

Taps your existing cava (raw output, private config; cava is never modified)
and draws one sine wave per frequency band, pixel-exact, through the Kitty
graphics protocol.   band = which wave moves,  loudness = how high it moves.

Keys: q or Esc quits.

Performance build - what changed under the hood (output is unchanged):

  * every terminal write moved to a dedicated thread.  The render loop can
    no longer block on a busy kitty - that block was the "App not
    responding" freeze.  If kitty falls behind, frames are conflated (only
    the newest survives) instead of piling up, and rendering pauses
    (backpressure) until the terminal catches up.  Keys stay responsive.
  * wave geometry for all bands is computed in one vectorised pass and all
    scratch buffers (glow planes, dither noise, phase/sin/cos tables) are
    reused across frames instead of re-allocated 60x per second.
  * cava banding switched from per-band array_split to a cached reduceat.
  * the cyclic garbage collector is switched off while running (every
    per-frame object is refcounted, so nothing leaks and no GC pause can
    land in the middle of a frame).
  * frame pacing gets drift correction, and skipped frames donate their
    elapsed time to the next rendered frame so wave speed stays exact.
  * [render] adaptive = true: if a frame overruns its budget casine first
    lowers render_scale a notch, then the frame rate, and reports on exit;
    it climbs back up automatically when there is headroom.
  * if kitty does not confirm shared-memory images (old kitty, unusual
    environment) the compressed fallback now runs capped (fallback_scale /
    fallback_fps) instead of flooding the pty and stuttering.
"""

import base64
import configparser
import fcntl
import gc
import math
import mmap
import os
import queue
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import zlib
from collections import deque
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.stderr.write("casine: missing numpy, run: sudo pacman -S python-numpy\n")
    raise SystemExit(1)

APP_DIR = Path.home() / ".config" / "casine"
CAVA_CONF = APP_DIR / "cava.conf"
VIS_CONF = APP_DIR / "casine.ini"
GLOW_ID, CORE_ID = 1, 2
PROBE_ID = 31
CHUNK = 4096
GS = 4                      # the soft glow is drawn at 1/GS resolution
TAU = 2.0 * math.pi
STOP = object()

DEFAULT_INI = """\
; -- casine settings ---------------------------------------------------
[waves]
count = 8                     ; number of waves (frequency bands, bass -> treble)
colors = #22d3ee, #38bdf8, #6366f1, #8b5cf6, #d946ef, #f472b6   ; blended across the waves
phase_spread = 1.7            ; phase offset between waves

[audio]
raw_max = 1000                ; must match ascii_max_range in cava.conf
sensitivity = 1.4             ; higher = more punch / reacts to quieter sound
auto_gain = true              ; normalise each band so treble moves as much as bass
gain_memory = 6.0             ; seconds the auto-gain remembers a loud peak
noise_floor = 0.03            ; ignore levels below this (0-1)
peak_mix = 0.5                ; 0 = band average, 1 = band peak

[shape]
center = 0.5                  ; vertical position of the waves (0 = top, 1 = bottom)
max_amplitude = 0.34          ; wave height at full volume (fraction of window height)
idle_amplitude = 0.010        ; sway when silent
bass_cycles = 1.0             ; wavelengths across the screen, bass wave
treble_cycles = 4.5           ; wavelengths across the screen, treble wave
bass_speed = 0.7              ; travel speed (rad/s), bass wave
treble_speed = 2.2            ; travel speed (rad/s), treble wave
speed_boost = 1.5             ; waves travel faster when loud
max_slope = 1.5               ; limits steepness (high-pitched waves stay shorter)
edge_power = 1.3              ; how softly the waves taper to the screen edges
edge_fade = 0.7               ; fade the lines out toward the edges (0 = off)

[look]
thickness = 2.0               ; line thickness in pixels
core_white = 0.22             ; soft light centre of each line (0-1)
glow_width = 11.0             ; glow radius in pixels
glow_strength = 0.30          ; 0 = no glow
min_visibility = 0.30         ; brightness of silent waves (0-1)

[motion]
attack_ms = 45                ; how quickly waves rise
release_ms = 320              ; how slowly they settle back
softness_ms = 55              ; rounds off sudden jumps (more = more liquid)

[render]
fps = 60
render_scale = 1.0            ; 1.0 = pixel-exact and sharp; lower only if your CPU struggles
adaptive = true               ; auto-lower scale (then fps) a notch if frames overrun; climbs back
fallback_scale = 0.6          ; render_scale cap when kitty can't use shared memory (auto-detected)
fallback_fps = 30             ; fps cap when kitty can't use shared memory (auto-detected)
"""


# --------------------------------------------------------------------- config
def parse_color(s):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return np.array([int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32)


def build_palette(colors, n):
    if not colors:
        colors = [np.ones(3, np.float32)]
    if len(colors) == 1:
        return [np.asarray(colors[0], np.float32)] * n
    arr = np.stack(colors)
    xp = np.linspace(0, 1, len(arr))
    return [np.array([np.interp(x, xp, arr[:, c]) for c in range(3)], np.float32)
            for x in np.linspace(0, 1, n)]


def load_config():
    if not VIS_CONF.is_file():
        try:
            VIS_CONF.parent.mkdir(parents=True, exist_ok=True)
            VIS_CONF.write_text(DEFAULT_INI)
        except OSError:
            pass
    defaults = configparser.ConfigParser(inline_comment_prefixes=(";",))
    defaults.read_string(DEFAULT_INI)
    cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
    cp.read_string(DEFAULT_INI)
    try:
        cp.read(VIS_CONF)
    except configparser.Error as exc:
        sys.stderr.write(f"casine: ignoring unreadable {VIS_CONF.name}: {exc}\n")

    def f(s, k):
        try:
            return cp.getfloat(s, k)
        except (configparser.Error, ValueError):
            return defaults.getfloat(s, k)

    def b(s, k):
        try:
            return cp.getboolean(s, k)
        except (configparser.Error, ValueError):
            return defaults.getboolean(s, k)

    def clamp(v, lo, hi):
        return min(hi, max(lo, v))

    n = max(1, int(f("waves", "count")))
    try:
        raw_colors = cp.get("waves", "colors")
    except configparser.Error:
        raw_colors = defaults.get("waves", "colors")
    colors = [parse_color(t) for t in raw_colors.split(",")
              if re.fullmatch(r"\s*#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\s*", t)]
    return {
        "n": n, "palette": build_palette(colors, n),
        "phase_spread": f("waves", "phase_spread"),
        "raw_max": max(1.0, f("audio", "raw_max")),
        "sens": max(0.1, f("audio", "sensitivity")),
        "auto_gain": b("audio", "auto_gain"),
        "gain_memory": max(0.2, f("audio", "gain_memory")),
        "noise_floor": clamp(f("audio", "noise_floor"), 0.0, 0.5),
        "peak_mix": clamp(f("audio", "peak_mix"), 0.0, 1.0),
        "center": clamp(f("shape", "center"), 0.05, 0.95),
        "max_amp": clamp(f("shape", "max_amplitude"), 0.01, 0.5),
        "idle_amp": clamp(f("shape", "idle_amplitude"), 0.0, 0.2),
        "bass_cycles": max(0.1, f("shape", "bass_cycles")),
        "treble_cycles": max(0.1, f("shape", "treble_cycles")),
        "bass_speed": f("shape", "bass_speed"),
        "treble_speed": f("shape", "treble_speed"),
        "speed_boost": max(0.0, f("shape", "speed_boost")),
        "max_slope": max(0.1, f("shape", "max_slope")),
        "edge_power": max(1.0, f("shape", "edge_power")),
        "edge_fade": max(0.0, f("shape", "edge_fade")),
        "thickness": max(0.5, f("look", "thickness")),
        "core_white": clamp(f("look", "core_white"), 0.0, 1.0),
        "glow_width": max(1.0, f("look", "glow_width")),
        "glow_strength": max(0.0, f("look", "glow_strength")),
        "min_vis": clamp(f("look", "min_visibility"), 0.0, 1.0),
        "attack": max(1.0, f("motion", "attack_ms")) / 1000.0,
        "release": max(1.0, f("motion", "release_ms")) / 1000.0,
        "soft": max(0.0, f("motion", "softness_ms")) / 1000.0,
        "fps": max(10, int(f("render", "fps"))),
        "scale": clamp(f("render", "render_scale"), 0.25, 1.0),
        "adaptive": b("render", "adaptive"),
        "fb_scale": clamp(f("render", "fallback_scale"), 0.25, 1.0),
        "fb_fps": max(10, int(f("render", "fallback_fps"))),
    }


# ---------------------------------------------------------------- audio side
_BAND_SPLITS = {}            # (bars, waves) -> (starts, nonempty, counts)


def band_levels(vals, cfg):
    """Raw cava bars -> one 0..1 level per wave (mean/peak mix per band).
    Same band boundaries np.array_split produces, but computed once per
    (bar count, wave count) and evaluated with reduceat."""
    n = cfg["n"]
    arr = np.clip(np.asarray(vals, np.float32), 0, cfg["raw_max"]) / cfg["raw_max"]
    out = np.zeros(n, np.float32)
    B = arr.size
    if B == 0:
        return out
    split = _BAND_SPLITS.get((B, n))
    if split is None:
        if B >= n:
            base, extra = divmod(B, n)
            sizes = [base + (1 if i < extra else 0) for i in range(n)]
        else:
            sizes = [1] * B + [0] * (n - B)
        starts = np.cumsum([0] + sizes[:-1]).astype(np.intp)
        nonempty = np.array(sizes, dtype=bool)
        counts = np.diff(np.append(starts[nonempty], B)).astype(np.float32)
        split = (starts, nonempty, counts)
        _BAND_SPLITS[(B, n)] = split
    starts, nonempty, counts = split
    ks = starts[nonempty]
    sums = np.add.reduceat(arr, ks)
    maxs = np.maximum.reduceat(arr, ks)
    mix = cfg["peak_mix"]
    out[nonempty] = (1.0 - mix) * (sums / counts) + mix * maxs
    return out


class Dynamics:
    """Band levels -> smooth wave levels. Everything is time based, so the
    feel is identical at any frame rate."""

    def __init__(self, cfg):
        n = cfg["n"]
        self.cfg = cfg
        self.peak = np.full(n, 0.25, np.float32)
        self.raw = np.zeros(n, np.float32)
        self.s1 = np.zeros(n, np.float32)
        self.s2 = np.zeros(n, np.float32)
        self.stale = 0.0

    def step(self, frames, dt):
        cfg = self.cfg
        if frames:
            self.raw = frames[-1]
            top = np.maximum.reduce(frames)
            self.stale = 0.0
        else:
            top = self.raw
            self.stale += dt
            if self.stale > 0.5:            # cava went quiet: let waves settle
                self.raw = np.zeros_like(self.raw)
                top = self.raw

        x = self.raw
        if cfg["auto_gain"]:
            self.peak = np.maximum(self.peak * math.exp(-dt / cfg["gain_memory"]), top)
            self.peak = np.maximum(self.peak, 0.12)
            x = x / self.peak
        g = cfg["noise_floor"]
        x = np.maximum(x - g, 0.0) / (1.0 - g)
        s = cfg["sens"]
        target = np.minimum(np.tanh(x * s) / math.tanh(s), 1.0)

        ka = 1.0 - math.exp(-dt / cfg["attack"])
        kr = 1.0 - math.exp(-dt / cfg["release"])
        self.s1 += (target - self.s1) * np.where(target > self.s1, ka, kr)
        if cfg["soft"] > 0:
            self.s2 += (self.s1 - self.s2) * (1.0 - math.exp(-dt / cfg["soft"]))
        else:
            self.s2[:] = self.s1
        return self.s2


# --------------------------------------------------------------- drawing side
class Grid:
    """Per-width column data: wave envelope, its slope and the edge fade."""

    def __init__(self, W, edge_power, edge_fade):
        self.W = W
        x = (np.arange(W, dtype=np.float64) + 0.5) / W
        sn = np.clip(np.sin(math.pi * x), 1e-6, None)
        p = edge_power
        self.x = x.astype(np.float32)
        self.env = (sn ** p).astype(np.float32)
        self.denv = (p * sn ** (p - 1) * np.cos(math.pi * x) * math.pi / W).astype(np.float32)
        self.fade = (sn ** edge_fade).astype(np.float32)


def stencil(y, inv, reach, H):
    """Pixels within `reach` of the curve y(x): (rows, columns, distance).
    The ki aranges are cached (m repeats heavily across waves and frames),
    and the two boundary comparisons are skipped entirely whenever the whole
    curve sits safely inside the band - same result, less work."""
    m = int(math.ceil(reach / float(inv.min()))) + 1
    base = np.floor(y).astype(np.int32)
    frac = (base.astype(np.float32) + np.float32(0.5)) - y
    ki, kf = _ki_cache(m)                                # (-m..m), int32 + float32
    rows = base[None, :] + ki[:, None]
    d = np.abs(kf[:, None] + frac[None, :]) * inv[None, :]
    if int(base.min()) >= m and int(base.max()) < H - m:
        ok = d < reach                                  # fully inside the band
    else:
        ok = (d < reach) & (rows >= 0) & (rows < H)
    cols = np.nonzero(ok)[1]
    return rows[ok], cols, d[ok]


_KI_CACHE = {}


def _ki_cache(m):
    """(-m..m) as (int32, float32), cached - m repeats across waves/frames."""
    pair = _KI_CACHE.get(m)
    if pair is None:
        ki = np.arange(-m, m + 1, dtype=np.int32)
        pair = (ki, ki.astype(np.float32))
        _KI_CACHE[m] = pair
    return pair


def draw_core(flat, W, H, y, dy, fade, hw, color, vis, white):
    """Crisp anti-aliased line, composited (straight-alpha 'over') into the
    RGBA canvas. Only the pixels next to the line are ever touched.
    The blend runs per colour channel on contiguous (N,) arrays - the old
    (N,3)*(N,1) broadcast layout could not be vectorised and was the single
    biggest cost in the whole renderer."""
    inv = (1.0 / np.sqrt(1.0 + dy * dy)).astype(np.float32)
    reach = hw + 0.5
    r, c, d = stencil(y, inv, reach, H)
    if not r.size:
        return
    s = np.clip(np.float32(reach) - d, 0.0, 1.0)
    cov = s * s * (3.0 - 2.0 * s)                       # smooth edge
    a = (cov * np.float32(vis) * fade[c]).astype(np.float32)
    t = np.float32(white) * cov * cov
    omt = np.float32(1.0) - t
    idx = r.astype(np.intp) * W + c
    # old pixel as (4, N) float - channels contiguous for the loop below
    old = flat[idx].view(np.uint8).reshape(-1, 4).T.astype(np.float32) * np.float32(1 / 255)
    keep = old[3] * (1.0 - a)
    na = a + keep
    denom = np.maximum(na, 1e-6)
    out = np.empty((idx.size, 4), np.uint8)
    for k in range(3):
        src = color[k] * omt + t
        rgb = (src * a + old[k] * keep) / denom
        out[:, k] = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    out[:, 3] = (np.clip(na, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    flat[idx] = out.view(np.uint32).ravel()


def draw_glow(acc_flat, Wg, Hg, y, dy, fade, sig, color, weight):
    """Soft gaussian glow, accumulated additively at low resolution into the
    three reused colour planes (flat views of a persistent (3, H, W) buffer).
    `acc_flat` is (3, Hmax*Wg); only the first Hg rows are ever touched."""
    inv = (1.0 / np.sqrt(1.0 + dy * dy)).astype(np.float32)
    r, c, d = stencil(y, inv, sig * 3.2, Hg)
    if not r.size:
        return
    inten = np.exp(d * d * np.float32(-0.5 / (sig * sig))) * (np.float32(weight) * fade[c])
    idx = r.astype(np.intp) * Wg + c
    for k in range(3):
        acc_flat[k, idx] += inten * np.float32(color[k])


# ------------------------------------------------------------------ transport
class Buf:
    __slots__ = ("arr", "shm_name", "size")

    def __init__(self, arr, shm_name, size):
        self.arr, self.shm_name, self.size = arr, shm_name, size


class Transport:
    """Hands frames to Kitty. Shared memory when Kitty is on this machine
    (no pixel data crosses the terminal at all), compressed stream otherwise.
    Only the Sender thread calls send(); alloc() is called by the renderer."""

    def __init__(self, use_shm):
        self.shm = use_shm
        self.seq = 0
        self.pending = deque()

    def alloc(self, h, w):
        size = h * w * 4
        if self.shm:
            self.seq += 1
            name = f"casine-{os.getpid()}-{self.seq}"
            path = "/dev/shm/" + name
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.ftruncate(fd, size)
                    mm = mmap.mmap(fd, size)
                finally:
                    os.close(fd)
            except OSError:
                self.shm = False
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                self.pending.append(path)
                return Buf(np.frombuffer(mm, dtype=np.uint32).reshape(h, w), "/" + name, size)
        return Buf(np.zeros((h, w), np.uint32), None, size)

    def _gc(self, keep):
        # Kitty unlinks each segment once it has read it; sweep up any it did not.
        while len(self.pending) > keep:
            try:
                os.unlink(self.pending.popleft())
            except OSError:
                pass

    def send(self, images, top_row, nrows, cols):
        out = bytearray(b"\x1b[?2026h")
        out += f"\x1b[{top_row + 1};1H".encode()
        for img_id, z, buf, w, h in images:
            ctrl = f"a=T,f=32,s={w},v={h},i={img_id},p=1,c={cols},r={nrows},z={z},C=1,q=2"
            if buf.shm_name:
                out += (b"\x1b_G" + (ctrl + f",t=s,S={buf.size}").encode() + b";"
                        + base64.b64encode(buf.shm_name.encode()) + b"\x1b\\")
                continue
            data = base64.b64encode(zlib.compress(memoryview(buf.arr).cast("B"), 1))
            first = True
            for off in range(0, len(data), CHUNK):
                more = 1 if off + CHUNK < len(data) else 0
                head = (ctrl + f",o=z,m={more}") if first else f"m={more}"
                first = False
                out += b"\x1b_G" + head.encode() + b";" + data[off:off + CHUNK] + b"\x1b\\"
        out += b"\x1b[?2026l"
        write_all(out)
        self._gc(6)

    def close(self):
        self._gc(0)


def write_all(data):
    """Write every byte to the terminal, tolerating partial writes and signals.
    Only ever called from the Sender thread (or after it has been joined),
    so a slow terminal can never stall the render loop."""
    fd = sys.stdout.fileno()
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 0.5)
            continue
        view = view[n:]


def kitty_delete():
    for i in (GLOW_ID, CORE_ID):
        sys.stdout.buffer.write(f"\x1b_Ga=d,d=I,i={i},q=2\x1b\\".encode())
    sys.stdout.buffer.flush()


def read_reply(fd, timeout):
    """Read one terminal graphics reply (ESC _ G ... ESC \\)."""
    end = time.monotonic() + timeout
    data = b""
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
        if not ready:
            break
        try:
            data += os.read(fd, 256)
        except OSError:
            break
        if b"\x1b\\" in data:
            break
    return data


def probe_shm(in_fd, timeout=0.35):
    """Ask Kitty whether it can read shared-memory images from this machine."""
    if in_fd is None or not os.path.isdir("/dev/shm"):
        return False
    name = f"casine-{os.getpid()}-probe"
    path = "/dev/shm/" + name
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"\0\0\0\0")
        os.close(fd)
    except OSError:
        return False
    try:
        sys.stdout.buffer.write(
            f"\x1b_Ga=q,i={PROBE_ID},s=1,v=1,f=32,t=s;".encode()
            + base64.b64encode(("/" + name).encode()) + b"\x1b\\")
        sys.stdout.buffer.flush()
        reply = read_reply(in_fd, timeout)
        return f"i={PROBE_ID};OK".encode() in reply
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class Sender:
    """Owns the terminal fd on its own thread.

    The render loop hands finished frames over and immediately moves on.
    While a frame is still queued the renderer pauses (backpressure), and if
    kitty falls further behind only the newest frame is kept (conflation).
    A blocked os.write can therefore never freeze the app - this is the fix
    for the "App not responding" behaviour."""

    def __init__(self, transport):
        self.tr = transport
        self.q = queue.Queue(maxsize=1)
        self.dead = None
        self.send_ema = 0.002                     # smoothed send time (seconds)
        self._t = threading.Thread(target=self._run, name="casine-writer", daemon=True)
        self._t.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            try:
                t0 = time.monotonic()
                self.tr.send(*item)
                dt = time.monotonic() - t0
                self.send_ema = dt if self.send_ema == 0.0 else self.send_ema * 0.85 + dt * 0.15
            except Exception as exc:
                self.dead = exc
                return

    def busy(self):
        return self.q.qsize() > 0

    def submit(self, frame):
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            try:
                self.q.get_nowait()               # drop the stale frame
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(frame)
            except queue.Full:
                pass

    def close(self, timeout=1.0):
        # a blocking put parks the sentinel *behind* any queued frame, so the
        # last frame still reaches the terminal instead of being conflated
        try:
            self.q.put(None, timeout=timeout)
        except queue.Full:
            pass
        self._t.join(timeout)


# ------------------------------------------------------------------- renderer
class Renderer:
    """Same picture as before, drawn with recycled buffers and one vectorised
    geometry pass per frame instead of per-wave passes."""

    def __init__(self, cfg, transport):
        self.cfg = cfg
        self.tr = transport
        n = cfg["n"]
        self.n = n
        self.ph = np.zeros(n)                     # travelling-wave phases
        rank = np.arange(n, dtype=np.float64) / max(1, n - 1)
        self.cyc = cfg["bass_cycles"] + (cfg["treble_cycles"] - cfg["bass_cycles"]) * rank
        self.spd = cfg["bass_speed"] + (cfg["treble_speed"] - cfg["bass_speed"]) * rank
        self.K = (TAU * self.cyc).astype(np.float32)
        self.woff = np.arange(n) * cfg["phase_spread"]

    def resize(self, cols, rows, cw, ch):
        cfg, n = self.cfg, self.n
        self.cols, self.rows = cols, rows
        self.ps = cfg["scale"]
        self.cw = max(1, int(round(cw * self.ps)))      # render pixels per cell
        self.ch = max(1, int(round(ch * self.ps)))
        self.W = max(16, cols * self.cw)
        self.H = max(8, rows * self.ch)
        self.Wg = max(8, int(round(self.W / GS)))
        self.big = Grid(self.W, cfg["edge_power"], cfg["edge_fade"])
        self.small = Grid(self.Wg, cfg["edge_power"], cfg["edge_fade"])
        self.kd = (TAU * self.cyc / self.W).astype(np.float32)
        self.kds = (TAU * self.cyc / self.Wg).astype(np.float32)
        # reused per-frame scratch: phase/sin/cos tables and geometry,
        # at full resolution and at glow resolution
        self.PH = np.empty((n, self.W), np.float32)
        self.S = np.empty_like(self.PH)
        self.C = np.empty_like(self.PH)
        self.T = np.empty_like(self.PH)
        self.Y = np.empty_like(self.PH)
        self.D = np.empty_like(self.PH)
        self.PHs = np.empty((n, self.Wg), np.float32)
        self.Ss = np.empty_like(self.PHs)
        self.Cs = np.empty_like(self.PHs)
        self.Ts = np.empty_like(self.PHs)
        self.Ys = np.empty_like(self.PHs)
        self.Ds = np.empty_like(self.PHs)
        Hg_max = self.H // GS + 4
        self.acc = np.zeros((3, Hg_max, self.Wg), np.float32)   # glow colour planes
        self.accf = self.acc.reshape(3, -1)
        self.noise = np.random.default_rng(7).random((Hg_max, self.Wg), np.float32)

    def _geom(self, grid, P, kd, ph, s, c, t, y, d):
        """y(x) and dy/dx for every wave in one pass (same math as the old
        per-wave wave_unit, just batched over the wave axis).  P is float32
        so the additions round exactly like the original np.float32(phase)."""
        np.multiply(grid.x[None, :], self.K[:, None], out=ph)
        ph += P[:, None]
        np.sin(ph, out=s)
        np.cos(ph, out=c)
        np.multiply(grid.env, s, out=y)
        np.multiply(grid.denv, s, out=d)
        np.multiply(grid.env, c, out=t)
        t *= kd[:, None]
        d += t

    def frame(self, lv, dt):
        cfg, big, small, ps = self.cfg, self.big, self.small, self.ps
        n, W, H = self.n, self.W, self.H
        cy = cfg["center"] * H
        lv = np.asarray(lv, np.float64)

        # ---- geometry of every wave, and the vertical band they occupy
        self.ph = (self.ph + dt * self.spd * (1.0 + cfg["speed_boost"] * lv)) % TAU
        P = (self.ph + self.woff).astype(np.float32)     # rounds phase like np.float32(phase)
        self._geom(big, P, self.kd, self.PH, self.S, self.C, self.T, self.Y, self.D)
        glow_on = cfg["glow_strength"] > 0
        if glow_on:
            self._geom(small, P, self.kds, self.PHs, self.Ss, self.Cs, self.Ts, self.Ys, self.Ds)

        smax = np.abs(self.D).max(axis=1).astype(np.float64)
        cap = np.minimum(cfg["max_amp"] * H, cfg["max_slope"] / (smax + 1e-9))
        idle = cfg["idle_amp"] * H
        amp = np.where(cap > idle, idle + (cap - idle) * lv, cap)
        hw = cfg["thickness"] * ps * 0.5 * (0.85 + 0.3 * lv)
        sig = cfg["glow_width"] * ps * (0.8 + 0.6 * lv)
        reach = hw + 2.0
        if glow_on:
            reach = np.maximum(reach, sig * 3.2 + 1.0)
        extent = float(max(1.0, (amp + reach).max()))

        r0 = max(0, int(math.floor((cy - extent) / self.ch)))
        r1 = min(self.rows, int(math.ceil((cy + extent) / self.ch)))
        if r1 <= r0:
            r0, r1 = 0, min(self.rows, 1)
        nrows = r1 - r0
        Hb = nrows * self.ch
        Hg = max(2, int(round(Hb / GS)))
        sy = Hg / Hb
        cy_local = cy - r0 * self.ch

        core = self.tr.alloc(Hb, W)
        flat = core.arr.reshape(-1)
        if glow_on:
            self.accf[:, :Hg * self.Wg] = 0.0

        for w in range(n):
            lvl = float(lv[w])
            # python floats keep the original scalar arithmetic (NEP-50 would
            # otherwise promote np.float64 scalars and change the rounding)
            amp_w, hw_w, sig_w = float(amp[w]), float(hw[w]), float(sig[w])
            color = cfg["palette"][w]
            vis = cfg["min_vis"] + (1.0 - cfg["min_vis"]) * lvl ** 0.7
            aw = np.float32(amp_w)
            if glow_on:
                draw_glow(self.accf, self.Wg, Hg,
                          cy_local * sy + (amp_w * sy) * self.Ys[w],
                          (amp_w * sy) * self.Ds[w], small.fade,
                          sig_w * sy, color,
                          cfg["glow_strength"] * (0.25 + 0.75 * lvl))
            draw_core(flat, W, Hb, np.float32(cy_local) + aw * self.Y[w], aw * self.D[w],
                      big.fade, hw_w, color, vis,
                      cfg["core_white"] * (0.5 + 0.5 * lvl))

        images = [(CORE_ID, 2, core, W, Hb)]
        if glow_on:
            # glow: hue at full brightness, alpha carries the intensity (soft knee)
            glow = self.tr.alloc(Hg, self.Wg)
            img = glow.arr.view(np.uint8).reshape(Hg, self.Wg, 4)
            acc = self.acc[:, :Hg, :]
            peak = np.maximum(np.maximum(acc[0], acc[1]), acc[2])
            scale = np.float32(255.0) / np.maximum(peak, np.float32(1e-6))
            for k in range(3):
                img[..., k] = acc[k] * scale + np.float32(0.5)
            alpha = (1.0 - np.exp(-peak)) * np.float32(255.0) + self.noise[:Hg]
            img[..., 3] = np.minimum(alpha, 255.0)
            images.insert(0, (GLOW_ID, 1, glow, self.Wg, Hg))

        return images, r0, nrows


# ------------------------------------------------------------------- terminal
def term_geometry():
    fd = sys.stdout.fileno()
    rows, cols, px_w, px_h = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    cols, rows = max(1, cols), max(1, rows)
    cw = px_w // cols if px_w else 0
    ch = px_h // rows if px_h else 0
    return cols, rows, (cw if cw > 0 else 10), (ch if ch > 0 else 20)


def cava_reader(proc, q, cfg):
    try:
        for line in proc.stdout:
            vals = [int(t) for t in line.split(b";") if t.strip().isdigit()]
            if vals and q.qsize() < 32:
                q.put(band_levels(vals, cfg))
    finally:
        q.put(STOP)


def drain(stream, sink):
    try:
        for line in stream:
            sink.append(line.decode("utf-8", "replace").rstrip())
    except (OSError, ValueError):
        pass


def key_pressed_quit(fd):
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return False
        data = os.read(fd, 64)
    except OSError:
        return False
    return data in (b"\x1b",) or b"q" in data or b"Q" in data


def main():
    if not sys.stdout.isatty():
        sys.stderr.write("casine: must be run in a terminal.\n")
        return 2
    if "KITTY_WINDOW_ID" not in os.environ and "kitty" not in os.environ.get("TERM", ""):
        sys.stderr.write("casine: requires the Kitty terminal (graphics protocol).\n")
        return 2
    cava = shutil.which("cava")
    if cava is None:
        sys.stderr.write("casine: could not find cava in PATH.\n")
        return 2
    if not CAVA_CONF.is_file():
        sys.stderr.write(f"casine: missing {CAVA_CONF} (the raw-output cava config).\n")
        return 2

    cfg = load_config()
    state = {"resized": True, "quit": False}
    signal.signal(signal.SIGWINCH, lambda *_: state.__setitem__("resized", True))
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: state.__setitem__("quit", True))

    in_fd = None
    old_tty = None
    if sys.stdin.isatty():
        in_fd = sys.stdin.fileno()
        old_tty = termios.tcgetattr(in_fd)
        tty.setcbreak(in_fd)

    gc.disable()          # no reference cycles are made per frame -> no GC pauses

    proc = None
    transport = None
    sender = None
    err_tail = deque(maxlen=12)
    failure = None
    notes = []

    try:
        use_shm = probe_shm(in_fd, 0.35) or probe_shm(in_fd, 0.6)
        capped = not use_shm
        if capped:
            cfg["scale"] = min(cfg["scale"], cfg["fb_scale"])
            notes.append("kitty did not confirm shared-memory images; running the "
                         "compressed fallback with capped load (fallback_scale/fallback_fps)")
        transport = Transport(use_shm)

        sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.flush()

        proc = subprocess.Popen([cava, "-p", str(CAVA_CONF)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        q = queue.Queue()
        threading.Thread(target=cava_reader, args=(proc, q, cfg), daemon=True).start()
        threading.Thread(target=drain, args=(proc.stderr, err_tail), daemon=True).start()

        sender = Sender(transport)
        renderer = Renderer(cfg, transport)
        dyn = Dynamics(cfg)
        cols = rows = cw = ch = 0
        cur_fps = min(cfg["fps"], cfg["fb_fps"]) if capped else cfg["fps"]
        scale0 = cfg["scale"]
        render_ema = 0.0
        counter = 0
        alive = True
        pend_dt = 0.0          # elapsed time of skipped frames (keeps wave speed exact)
        last = time.monotonic()
        next_t = last

        while alive and not state["quit"] and sender.dead is None:
            t0 = time.monotonic()
            dt = min(0.1, max(1e-4, t0 - last))
            last = t0

            if in_fd is not None and key_pressed_quit(in_fd):
                break
            if state["resized"]:
                state["resized"] = False
                cols, rows, cw, ch = term_geometry()
                renderer.resize(cols, rows, cw, ch)

            if not capped and not transport.shm:     # shared memory died mid-run
                capped = True
                cfg["scale"] = min(cfg["scale"], cfg["fb_scale"])
                cur_fps = min(cur_fps, cfg["fb_fps"])
                state["resized"] = True
                notes.append("shared-memory images became unavailable; switched to the "
                             "compressed fallback with capped load")

            frames = []
            while len(frames) < 64:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is STOP:
                    alive = False
                    break
                frames.append(item)
            lv = dyn.step(frames, dt)

            if sender.busy():
                # terminal still digesting the previous frame: hold rendering,
                # but bank this frame's time so motion stays at true speed
                pend_dt = min(0.1, pend_dt + dt)
            else:
                tr0 = time.perf_counter()
                images, top, nrows = renderer.frame(lv, dt + pend_dt)
                pend_dt = 0.0
                sender.submit((images, top, nrows, cols))
                del images
                rc = time.perf_counter() - tr0
                render_ema = rc if render_ema == 0.0 else render_ema * 0.85 + rc * 0.15

            # keep the pace: prefer sharpness, trade scale before frame rate
            counter += 1
            if counter % 30 == 0 and cols:
                budget = 1.0 / cur_fps
                cost = max(render_ema, sender.send_ema)
                if cfg["adaptive"]:
                    if cost > 0.8 * budget:
                        if cfg["scale"] > 0.51:
                            cfg["scale"] = max(0.5, cfg["scale"] * 0.85)
                            renderer.resize(cols, rows, cw, ch)
                        elif cur_fps > 24:
                            cur_fps = max(24, int(cur_fps * 0.85))
                    elif cost < 0.45 * budget:
                        if cur_fps < cfg["fps"]:
                            cur_fps = min(cfg["fps"], int(cur_fps * 1.2) + 1)
                        elif cfg["scale"] < 0.99:
                            cfg["scale"] = min(1.0, cfg["scale"] / 0.85)
                            renderer.resize(cols, rows, cw, ch)
            next_t += 1.0 / cur_fps
            wait = next_t - time.monotonic()
            if wait > 2e-4:
                time.sleep(wait)
            elif wait < -2.0 / cur_fps:
                next_t = time.monotonic()

        if sender.dead is not None:
            failure = f"terminal writer failed: {sender.dead}"
        if not alive and proc.poll() not in (None, 0):
            failure = f"cava exited with status {proc.returncode}"
        if counter > 60 and (cfg["scale"] < scale0 - 0.01 or cur_fps < cfg["fps"] - 0.5):
            notes.append(f"adaptive quality settled at scale {cfg['scale']:.2f}, {cur_fps} fps "
                         f"(frame cost ~{max(render_ema, sender.send_ema) * 1000:.1f} ms; lower "
                         f"render_scale or waves count in casine.ini to lock it in)")
    except KeyboardInterrupt:
        pass
    except Exception as exc:                         # report after the screen is restored
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if sender is not None:
            sender.close(1.0)
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if transport is not None:
            transport.close()
        kitty_delete()
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if old_tty is not None:
            termios.tcsetattr(in_fd, termios.TCSAFLUSH, old_tty)
        gc.enable()

    for note in notes:
        sys.stderr.write(f"casine: {note}\n")
    if failure:
        sys.stderr.write(f"casine: {failure}\n")
        for line in err_tail:
            sys.stderr.write(f"  cava: {line}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
