"""
Echo - a minimal frosted-glass voice recorder.

  Space            record / pause
  P                play / pause what you have recorded (or opened)
  Left / Right     skip 5 seconds while reviewing
  Click / drag     seek on the waveform (when not recording)
  Shift+S, Ctrl+S  save        Ctrl+Shift+S  save as
"""

import sys
import os
import json
import time
import wave
import ctypes
import tempfile
from array import array
from collections import deque

from PySide6.QtCore import (
    Qt, QTimer, QElapsedTimer, QRectF, QPointF, QPoint, QSize, QObject, Signal,
    QBuffer, QByteArray, QIODevice, QVariantAnimation, QEasingCurve
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QIcon, QPixmap, QShortcut, QKeySequence,
    QAction, QLinearGradient, QPainterPath, QRegion, QBrush, QFont
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QMenu, QFileDialog, QDialog, QSizeGrip
)
from PySide6.QtMultimedia import (
    QAudio, QAudioFormat, QAudioSource, QAudioSink, QMediaDevices
)


APP_NAME = "Echo"

# ---- tweakables -----------------------------------------------------------
FROST_ALPHA = 0xA0     # 0x00-0xFF. Higher = more frosted / less see-through.
FROST_RGB = (0x3A, 0x40, 0x46)   # tint colour of the glass (R, G, B)

BAR_W = 3              # waveform bar width in px
BAR_GAP = 2            # gap between bars in px
TICK_MS = 80           # one new bar every TICK_MS  (higher = slower scroll)
FRAME_MS = 16          # ~60 fps while recording
HISTORY = 700          # bars kept in memory
PEAK_BINS = 1200       # resolution of the static (review) waveform
CLIP_LEVEL = 0.985     # a peak above this is drawn as clipping
PLAYHEAD = QColor(240, 90, 130)
REC_RED = QColor(237, 39, 50)

# Input device to use at startup (matched by name, case-insensitive).
# Leave "" to auto-pick a real microphone.
PREFERRED_INPUT = "Voicemeeter Out B1"

# "auto" uses the `sounddevice` package when it is installed (best for
# Voicemeeter / virtual devices), otherwise Qt. Set "qt" to force Qt.
BACKEND = "auto"
# ---------------------------------------------------------------------------

PITCH = BAR_W + BAR_GAP
IS_WIN = sys.platform == "win32"
WIN_BUILD = sys.getwindowsversion().build if IS_WIN else 0
ROUND_DWM = IS_WIN and WIN_BUILD >= 22000    # Windows 11 rounds the window itself
BACKDROP = "none"                            # becomes "accent" when blur is active


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def fmt_time(ms):
    t = int(ms) // 100
    return f"{t // 600:02d}:{(t // 10) % 60:02d}.{t % 10}"


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------

def make_fallback_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(255, 255, 255, 55), 2))
    p.setBrush(QColor("#ed2732"))
    p.drawEllipse(9, 9, 46, 46)
    p.end()
    return QIcon(pix)


def find_icon():
    # __file__ covers a Nuitka onefile build (data is unpacked beside it);
    # argv[0] covers running next to a loose recorder.ico.
    bases = (os.path.dirname(os.path.abspath(__file__)),
             os.path.dirname(os.path.abspath(sys.argv[0])))
    for base in bases:
        for name in ("recorder.ico", "icon.ico", "app.ico"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return QIcon(path)
    return make_fallback_icon()


def make_glyph(kind, size=40):
    colors = {
        "warning": QColor(255, 196, 61),
        "error": QColor(255, 99, 99),
        "info": QColor(140, 190, 255),
    }
    color = colors.get(kind, colors["info"])
    pix = QPixmap(size * 2, size * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(color)
    fill.setAlpha(40)
    p.setBrush(fill)
    p.setPen(QPen(color, 1.6))
    p.drawEllipse(QRectF(2, 2, size - 4, size - 4))
    p.setPen(color)
    font = QFont("Segoe UI", 15)
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter,
               "i" if kind == "info" else "!")
    p.end()
    return pix


# --------------------------------------------------------------------------
# Frosted glass backdrop (Windows acrylic)
# --------------------------------------------------------------------------

class _ACCENT(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCAD(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _dwm_set(hwnd, attr, value):
    v = ctypes.c_int(value)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd, ctypes.c_uint(attr), ctypes.byref(v), ctypes.sizeof(v)
    )


def apply_backdrop(widget):
    """Turn on acrylic blur behind the window (the method that works with
    translucent Qt windows on Windows 10 and 11)."""
    global BACKDROP
    if not IS_WIN:
        return
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))

        if ROUND_DWM:
            try:
                _dwm_set(hwnd, 33, 2)    # rounded corners (Windows 11)
            except Exception:
                pass
        try:
            _dwm_set(hwnd, 20, 1)        # dark mode
        except Exception:
            pass

        r, g, b = FROST_RGB
        color = (FROST_ALPHA << 24) | (b << 16) | (g << 8) | r   # AABBGGRR
        accent = _ACCENT(4, 2, color, 0)  # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND
        data = _WCAD(19, ctypes.addressof(accent), ctypes.sizeof(accent))

        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WCAD)]
        fn.restype = ctypes.c_int
        ok = fn(hwnd, ctypes.byref(data))
        BACKDROP = "accent" if ok else "none"
    except Exception:
        BACKDROP = "none"


def glass_radius():
    return 8 if ROUND_DWM else 14


def paint_glass(widget, extra_alpha=0, glow=0.0):
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    rect = QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
    r = glass_radius()
    path = QPainterPath()
    path.addRoundedRect(rect, r, r)

    # With real blur the OS already supplies most of the tint; without it we
    # need a mostly opaque panel so the UI stays readable.
    base = 30 if BACKDROP == "accent" else 228
    p.fillPath(path, QColor(26, 30, 34, min(250, base + extra_alpha)))

    sheen = QLinearGradient(0, 0, 0, min(widget.height(), 300))
    sheen.setColorAt(0.0, QColor(255, 255, 255, 30))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 4))
    p.fillPath(path, QBrush(sheen))

    # soft red wash rising from the bottom while recording
    if glow > 0.01:
        wash = QLinearGradient(0, widget.height(), 0, widget.height() * 0.2)
        wash.setColorAt(0.0, QColor(REC_RED.red(), REC_RED.green(), REC_RED.blue(),
                                    int(38 * glow)))
        wash.setColorAt(1.0, QColor(REC_RED.red(), REC_RED.green(), REC_RED.blue(), 0))
        p.fillPath(path, QBrush(wash))

    p.setPen(QPen(QColor(255, 255, 255, 55), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)

    # 1px highlight just inside the top edge
    hi = QLinearGradient(rect.left(), 0, rect.right(), 0)
    hi.setColorAt(0.0, QColor(255, 255, 255, 0))
    hi.setColorAt(0.5, QColor(255, 255, 255, 46))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(QPen(QBrush(hi), 1))
    p.drawLine(QPointF(rect.left() + r, rect.top() + 1.5),
               QPointF(rect.right() - r, rect.top() + 1.5))
    p.end()


def update_mask(widget):
    """Windows 10 / other: clip the blur to rounded corners."""
    if ROUND_DWM:
        widget.clearMask()
        return
    path = QPainterPath()
    path.addRoundedRect(QRectF(widget.rect()), glass_radius(), glass_radius())
    widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


# --------------------------------------------------------------------------
# Audio helpers
# --------------------------------------------------------------------------

def peak_level(data):
    """Exact peak of a chunk of 16-bit PCM (0.0 - 1.0)."""
    n = len(data) & ~1
    if n <= 0:
        return 0.0
    vals = memoryview(data)[:n].cast("h")
    try:
        return max(max(vals), -min(vals)) / 32768.0
    finally:
        vals.release()


def compute_peaks(frames, n_bins):
    """Peak per bin across the whole recording (16-bit PCM, any channels)."""
    out = [0.0] * n_bins
    if n_bins <= 0:
        return out
    mv = memoryview(frames)
    try:
        total = len(mv) // 2
        if total == 0:
            return out
        vals = mv[:total * 2].cast("h")
        try:
            size = total / n_bins
            for i in range(n_bins):
                a = int(i * size)
                b = max(a + 1, int((i + 1) * size))
                seg = vals[a:b:max(1, (b - a) // 64)]
                if len(seg):
                    out[i] = max(max(seg), -min(seg)) / 32768.0
                del seg
        finally:
            vals.release()
    finally:
        mv.release()
    return out


def to_int16(data, sample_format):
    """Convert whatever the device delivers into 16-bit PCM bytes."""
    SF = QAudioFormat.SampleFormat
    if sample_format == SF.Int16:
        return bytes(data)
    if sample_format == SF.Float:
        a = array("f")
        a.frombytes(data[: len(data) & ~3])
        return array("h", (max(-32768, min(32767, int(x * 32767))) for x in a)).tobytes()
    if sample_format == SF.Int32:
        a = array("i")
        a.frombytes(data[: len(data) & ~3])
        return array("h", (x >> 16 for x in a)).tobytes()
    if sample_format == SF.UInt8:
        return array("h", ((x - 128) << 8 for x in data)).tobytes()
    return bytes(data)


_U8_TO_S8 = bytes(i ^ 0x80 for i in range(256))


def pcm_to_int16(raw, width):
    """8 / 16 / 24 / 32-bit little-endian PCM (from a WAV file) -> 16-bit."""
    if width == 2:
        return bytes(raw)
    if width == 1:
        out = bytearray(len(raw) * 2)
        out[1::2] = bytes(raw).translate(_U8_TO_S8)
        return bytes(out)
    if width == 3:
        n = len(raw) // 3
        raw = bytes(raw[:n * 3])
        out = bytearray(n * 2)
        out[0::2] = raw[1::3]
        out[1::2] = raw[2::3]
        return bytes(out)
    if width == 4:
        n = len(raw) // 4
        raw = bytes(raw[:n * 4])
        out = bytearray(n * 2)
        out[0::2] = raw[2::4]
        out[1::2] = raw[3::4]
        return bytes(out)
    raise ValueError(f"unsupported sample width: {width}")


try:
    import sounddevice as sd
    HAVE_SD = True
except Exception:
    sd = None
    HAVE_SD = False


class Dev:
    def __init__(self, name, key, virtual=False):
        self.name = name
        self.key = key
        self.virtual = virtual


VIRTUAL_HINTS = ("voicemeeter", "vb-audio", "virtual", "stereo mix", "cable",
                 "loopback", "obs", "steam streaming")


def name_is_virtual(name):
    n = name.lower()
    return any(h in n for h in VIRTUAL_HINTS)


def pick_default(devs, system_default=""):
    if not devs:
        return None
    pref = PREFERRED_INPUT.strip().lower()
    if pref:
        for d in devs:
            if pref in d.name.lower():
                return d
    if system_default:
        for d in devs:
            if d.name == system_default and not d.virtual:
                return d
    for d in devs:
        if not d.virtual:
            return d
    return devs[0]


def reduce_channels(data, ch, typecode):
    """Keep only the first two channels of multi-channel audio (Voicemeeter
    buses are 8-channel; channels 1-2 are the stereo mix)."""
    if ch <= 2:
        return data
    a = array(typecode)
    a.frombytes(data)
    n = len(a) // ch
    out = array(typecode, bytes(n * 2 * a.itemsize))
    out[0::2] = a[0:n * ch:ch]
    out[1::2] = a[1:n * ch:ch]
    return out.tobytes()


class QtCapture:
    name = "qt"

    def __init__(self):
        self.source = None
        self.io = None
        self.rate = 48000
        self.out_channels = 1
        self.channels = 1
        self.sf = QAudioFormat.SampleFormat.Int16
        self.bps = 2
        self.log = []
        self._rem = b""

    def default_name(self):
        d = QMediaDevices.defaultAudioInput()
        return "" if d.isNull() else d.description()

    def devices(self):
        return [Dev(d.description(), d, name_is_virtual(d.description()))
                for d in QMediaDevices.audioInputs()]

    def start(self, dev):
        self.stop()
        self.log = []
        SF = QAudioFormat.SampleFormat
        device = dev.key
        pref = device.preferredFormat()

        cands = []

        def add(rate, ch, sf):
            if rate and ch and sf != SF.Unknown and (rate, ch, sf) not in cands:
                cands.append((rate, ch, sf))

        add(pref.sampleRate(), pref.channelCount(), pref.sampleFormat())
        add(pref.sampleRate(), pref.channelCount(), SF.Int16)
        for rate in (pref.sampleRate(), 48000, 44100):
            for ch in (2, 1):
                add(rate, ch, SF.Int16)

        for rate, ch, sf in cands:
            fmt = QAudioFormat()
            fmt.setSampleRate(rate)
            fmt.setChannelCount(ch)
            fmt.setSampleFormat(sf)
            src = QAudioSource(device, fmt)
            io = src.start()
            err = src.error()
            if io is not None and err == QAudio.Error.NoError:
                self.source, self.io = src, io
                self.rate, self.channels, self.sf = rate, ch, sf
                self.bps = fmt.bytesPerSample()
                self.out_channels = min(2, ch)
                self._rem = b""
                return True
            self.log.append(f"{rate} Hz {ch}ch {sf.name}: {err.name}")
            src.stop()
            src.deleteLater()
        return False

    def read(self):
        if self.io is None:
            return b""
        data = bytes(self.io.readAll().data())
        if not data:
            return b""
        data = self._rem + data
        fb = max(1, self.bps * self.channels)
        usable = len(data) - (len(data) % fb)
        self._rem = data[usable:]
        data = data[:usable]
        if not data:
            return b""
        SF = QAudioFormat.SampleFormat
        tc = {SF.Int16: "h", SF.Float: "f", SF.Int32: "i", SF.UInt8: "B"}.get(self.sf, "h")
        data = reduce_channels(data, self.channels, tc)
        return to_int16(data, self.sf)

    def pause(self):
        if self.source:
            self.source.suspend()

    def resume(self):
        if self.source:
            self.source.resume()

    def stop(self):
        if self.source:
            self.source.stop()
            self.source.deleteLater()
        self.source = None
        self.io = None


class SdCapture:
    """PortAudio (sounddevice) capture. Opens Voicemeeter's virtual devices
    through MME, which resamples/remixes for us."""
    name = "sd"

    def __init__(self):
        self.stream = None
        self.q = deque()
        self.paused = True
        self.rate = 48000
        self.out_channels = 1
        self.log = []

    def default_name(self):
        return ""

    def devices(self):
        apis = sd.query_hostapis()
        devs = sd.query_devices()
        inputs = [i for i, d in enumerate(devs) if d["max_input_channels"] > 0]
        mme = [i for i in inputs if apis[devs[i]["hostapi"]]["name"] == "MME"]
        out, seen = [], set()
        for i in (mme or inputs):
            nm = devs[i]["name"]
            if nm in seen or nm.lower().startswith("microsoft sound mapper"):
                continue
            seen.add(nm)
            out.append(Dev(nm, i, name_is_virtual(nm)))
        return out

    def _cb(self, indata, frames, t, status):
        if not self.paused:
            self.q.append(bytes(indata))

    def start(self, dev):
        self.stop()
        self.log = []
        info = sd.query_devices(dev.key)
        max_in = max(1, int(info["max_input_channels"]))
        rates = []
        for r in (int(info["default_samplerate"]), 48000, 44100):
            if r not in rates:
                rates.append(r)
        for ch in sorted({min(2, max_in), 1}, reverse=True):
            for rate in rates:
                try:
                    st = sd.RawInputStream(device=dev.key, channels=ch, samplerate=rate,
                                           dtype="int16", callback=self._cb)
                    st.start()
                except Exception as e:
                    self.log.append(f"{rate} Hz {ch}ch: {e}")
                    continue
                self.stream = st
                self.rate = rate
                self.out_channels = ch
                self.q.clear()
                self.paused = False
                return True
        return False

    def read(self):
        chunks = []
        while self.q:
            chunks.append(self.q.popleft())
        return b"".join(chunks)

    def pause(self):
        self.paused = True

    def resume(self):
        self.q.clear()
        self.paused = False

    def stop(self):
        self.paused = True
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.q.clear()


def shape_level(v):
    """Make quiet speech visible without clipping loud peaks."""
    return min(1.0, (v ** 0.55) * 1.15)


# --------------------------------------------------------------------------
# Crash recovery: the raw audio is also streamed to disk while recording, so
# an unsaved recording survives a crash or power cut.
# --------------------------------------------------------------------------

class Recovery:
    def __init__(self):
        root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        self.dir = os.path.join(root, "Echo")
        self.pcm = os.path.join(self.dir, "recovery.pcm")
        self.meta = os.path.join(self.dir, "recovery.json")
        self.f = None
        self.dead = False           # disk problem: stop trying this session
        self._last_flush = 0.0

    def available(self):
        try:
            return os.path.getsize(self.pcm) > 0 and os.path.isfile(self.meta)
        except OSError:
            return False

    def read(self):
        """-> (pcm bytes, rate, channels) or None."""
        try:
            with open(self.meta, "r", encoding="utf-8") as fh:
                m = json.load(fh)
            with open(self.pcm, "rb") as fh:
                data = fh.read()
            rate, ch = int(m["rate"]), int(m["channels"])
            if rate <= 0 or ch <= 0:
                return None
            data = data[: len(data) - len(data) % (2 * ch)]
            return data, rate, ch
        except Exception:
            return None

    def begin(self, rate, channels, existing):
        self.close()
        if self.dead:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.meta, "w", encoding="utf-8") as fh:
                json.dump({"rate": rate, "channels": channels}, fh)
            self.f = open(self.pcm, "wb")
            if existing:
                self.f.write(existing)
            self.f.flush()
            self._last_flush = time.monotonic()
        except Exception:
            self.f = None
            self.dead = True

    def write(self, data):
        if self.f is None:
            return
        try:
            self.f.write(data)
            now = time.monotonic()
            if now - self._last_flush > 1.0:
                self.f.flush()
                self._last_flush = now
        except Exception:
            self.close()
            self.dead = True

    def flush(self):
        if self.f is not None:
            try:
                self.f.flush()
            except Exception:
                pass

    def close(self):
        if self.f is not None:
            try:
                self.f.close()
            except Exception:
                pass
        self.f = None

    def clear(self):
        self.close()
        for path in (self.pcm, self.meta):
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------

class Playback(QObject):
    """Plays the 16-bit PCM held in memory through the default output."""
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sink = None
        self.buf = None
        self.data = None
        self.device = None
        self.fmt = None
        self.rate = 48000
        self.frame_bytes = 2
        self.start_byte = 0
        self.total = 0
        self.paused = False
        self.active = False

    def start(self, frames, rate, channels, start_byte):
        """Returns None on success, or an error message."""
        self.stop()
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return "No audio output device was found."

        fmt = QAudioFormat()
        fmt.setSampleRate(rate)
        fmt.setChannelCount(channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            return (f"Your output device can't play {rate} Hz, "
                    f"{channels}-channel audio.")

        self.device, self.fmt = device, fmt
        self.rate = rate
        self.frame_bytes = 2 * channels
        self.total = len(frames) // self.frame_bytes * self.frame_bytes
        with memoryview(frames) as mv:
            raw = bytes(mv[:self.total])
        self.data = QByteArray(raw)
        del raw

        self.buf = QBuffer(self)
        self.buf.setData(self.data)
        self.buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self.paused = False
        self.active = True
        return self._begin(start_byte)

    def _begin(self, start_byte):
        self._drop_sink()
        pos = max(0, min(start_byte, self.total))
        pos = pos // self.frame_bytes * self.frame_bytes
        self.start_byte = pos
        self.buf.seek(pos)
        self.sink = QAudioSink(self.device, self.fmt, self)
        self.sink.stateChanged.connect(self._on_state)
        self.sink.start(self.buf)
        err = self.sink.error()
        if err != QAudio.Error.NoError:
            self.stop()
            return f"The output device could not be opened ({err.name})."
        if self.paused:
            self.sink.suspend()
        return None

    def _drop_sink(self):
        if self.sink is not None:
            try:
                self.sink.stateChanged.disconnect(self._on_state)
            except Exception:
                pass
            self.sink.stop()
            self.sink.deleteLater()
            self.sink = None

    def _on_state(self, state):
        if not self.active or self.paused or self.buf is None:
            return
        if state == QAudio.State.IdleState and self.buf.atEnd():
            QTimer.singleShot(0, self._emit_finished)

    def _emit_finished(self):
        if self.active:
            self.finished.emit()

    def position_bytes(self):
        if not self.active or self.sink is None:
            return self.start_byte
        frames_done = int(self.sink.processedUSecs() * self.rate / 1_000_000)
        return min(self.start_byte + frames_done * self.frame_bytes, self.total)

    def pause(self):
        if self.sink is not None and self.active and not self.paused:
            self.paused = True
            self.sink.suspend()

    def resume(self):
        if self.sink is not None and self.active and self.paused:
            self.paused = False
            self.sink.resume()

    def seek(self, byte):
        if not self.active:
            return None
        return self._begin(byte)

    def stop(self):
        self.active = False
        self.paused = False
        self._drop_sink()
        if self.buf is not None:
            self.buf.close()
            self.buf.deleteLater()
            self.buf = None
        self.data = None


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class TitleBar(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255, 10))
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()


class Grip(QSizeGrip):
    """Resize handle for the frameless window (three faint dots)."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 70))
        s = float(self.width())
        for i in range(3):
            for j in range(3 - i):
                p.drawEllipse(QPointF(s - 3 - j * 4, s - 3 - i * 4), 1.0, 1.0)
        p.end()


class RecDot(QWidget):
    """Small blinking dot beside the timer while recording."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on = False
        self.setFixedSize(12, 12)

    def set_on(self, value):
        if value != self.on:
            self.on = value
            self.update()

    def paintEvent(self, event):
        if not self.on:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        halo = QColor(REC_RED)
        halo.setAlpha(60)
        p.setBrush(halo)
        p.drawEllipse(QRectF(0.5, 0.5, 11, 11))
        p.setBrush(REC_RED)
        p.drawEllipse(QRectF(2.5, 2.5, 7, 7))
        p.end()


class RecordButton(QPushButton):
    SIZE = 46.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.morph = 0.0        # 0 = round record dot, 1 = rounded stop square
        self.pulse = 0.0        # 0..1 phase of the halo while recording
        self.setFixedSize(54, 54)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

        self.anim = QVariantAnimation(self)
        self.anim.setDuration(190)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.valueChanged.connect(self._on_morph)

        self.pulse_anim = QVariantAnimation(self)
        self.pulse_anim.setDuration(1400)
        self.pulse_anim.setStartValue(0.0)
        self.pulse_anim.setEndValue(1.0)
        self.pulse_anim.setLoopCount(-1)
        self.pulse_anim.valueChanged.connect(self._on_pulse)

    def _on_morph(self, v):
        self.morph = float(v)
        self.update()

    def _on_pulse(self, v):
        self.pulse = float(v)
        self.update()

    def set_active(self, value):
        self.active = value
        self.anim.stop()
        self.anim.setStartValue(self.morph)
        self.anim.setEndValue(1.0 if value else 0.0)
        self.anim.start()
        if value:
            self.pulse_anim.start()
        else:
            self.pulse_anim.stop()
            self.pulse = 0.0
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = self.underMouse()

        s = self.SIZE
        off = (self.width() - s) / 2.0
        p.translate(off, off)

        if self.active:
            e = 4.0 * self.pulse
            halo = QColor(REC_RED)
            halo.setAlpha(int(120 * (1.0 - self.pulse)))
            p.setPen(QPen(halo, 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(1.5 - e, 1.5 - e, s - 3 + 2 * e, s - 3 + 2 * e))

        p.setPen(QPen(QColor(255, 255, 255, 120 if hover else 80), 1.6))
        p.setBrush(QColor(255, 255, 255, 22 if hover else 10))
        p.drawEllipse(QRectF(1.5, 1.5, s - 3, s - 3))

        p.setPen(Qt.PenStyle.NoPen)
        red = QColor(REC_RED)
        red.setAlpha(255 if hover else 235)
        if self.isDown():
            red = QColor(200, 30, 40)
        p.setBrush(red)

        t = self.morph
        d = s * 0.58 + (s * 0.30 - s * 0.58) * t
        rad = d / 2.0 + (4.0 - d / 2.0) * t
        p.drawRoundedRect(QRectF((s - d) / 2, (s - d) / 2, d, d), rad, rad)
        p.end()


class PlayButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.playing = False
        self.setFixedSize(40, 40)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_playing(self, value):
        self.playing = value
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        hover = self.underMouse() and enabled
        k = 1.0 if enabled else 0.38
        s = float(self.width())

        p.setPen(QPen(QColor(255, 255, 255, int((120 if hover else 80) * k)), 1.4))
        p.setBrush(QColor(255, 255, 255, 22 if hover else 10))
        p.drawEllipse(QRectF(1, 1, s - 2, s - 2))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(245, 246, 247, int(235 * k)))
        if self.playing:
            bw, bh, gap = s * 0.09, s * 0.30, s * 0.09
            y = (s - bh) / 2
            p.drawRoundedRect(QRectF(s / 2 - gap / 2 - bw, y, bw, bh), 1.5, 1.5)
            p.drawRoundedRect(QRectF(s / 2 + gap / 2, y, bw, bh), 1.5, 1.5)
        else:
            w, h = s * 0.28, s * 0.32
            x0 = s / 2 - w / 3
            y0 = (s - h) / 2
            path = QPainterPath()
            path.moveTo(x0, y0)
            path.lineTo(x0 + w, s / 2)
            path.lineTo(x0, y0 + h)
            path.closeSubpath()
            p.drawPath(path)
        p.end()


class Waveform(QWidget):
    """Voice-memo style bars. While recording they scroll left into a fixed
    playhead; when reviewing, the whole take is shown with a moving playhead
    that can be clicked or dragged to seek."""

    seeked = Signal(float, bool)      # fraction 0..1, final (mouse released)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.levels = deque([(0.0, False)] * HISTORY, maxlen=HISTORY)
        self.recording = False
        self.review_peaks = None    # list of peaks when showing a finished take
        self.play_frac = 0.0
        self.pending = 0.0
        self.current = 0.0
        self.frac = 0.0
        self.last_tick = 0
        self.clock = QElapsedTimer()
        self.seekable = False
        self.dragging = False
        self.hover_x = None
        self.setMinimumHeight(84)
        self.setMouseTracking(True)

        self.frame = QTimer(self)
        self.frame.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame.timeout.connect(self.on_frame)

    @property
    def reviewing(self):
        return self.review_peaks is not None

    # -- control --
    def start(self):
        self.recording = True
        self.review_peaks = None
        self.clock.start()
        self.last_tick = 0
        self.frame.start(FRAME_MS)

    def stop(self):
        self.recording = False
        self.frame.stop()
        self.update()

    def clear(self):
        self.stop()
        self.review_peaks = None
        self.play_frac = 0.0
        self.levels = deque([(0.0, False)] * HISTORY, maxlen=HISTORY)
        self.pending = self.current = self.frac = 0.0
        self.update()

    def push(self, level):
        if level > self.pending:
            self.pending = level

    def show_review(self, peaks):
        self.stop()
        self.review_peaks = peaks
        self.update()

    def set_position(self, frac):
        self.play_frac = min(1.0, max(0.0, frac))
        self.update()

    def set_seekable(self, value):
        if value != self.seekable:
            self.seekable = value
            if not value:
                self.dragging = False
                self.hover_x = None
            self.setCursor(Qt.CursorShape.PointingHandCursor if value
                           else Qt.CursorShape.ArrowCursor)
            self.update()

    # -- mouse (seeking) --
    def _frac_at(self, x):
        left = 14.5
        right = self.width() - 14.5
        return min(1.0, max(0.0, (x - left) / max(1.0, right - left)))

    def mousePressEvent(self, event):
        if self.seekable and event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.seeked.emit(self._frac_at(event.position().x()), False)

    def mouseMoveEvent(self, event):
        self.hover_x = event.position().x()
        if self.dragging:
            self.seeked.emit(self._frac_at(self.hover_x), False)
        if self.seekable or self.dragging:
            self.update()

    def mouseReleaseEvent(self, event):
        if self.dragging and event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            self.seeked.emit(self._frac_at(event.position().x()), True)

    def leaveEvent(self, event):
        self.hover_x = None
        self.update()

    # -- animation --
    def on_frame(self):
        now = self.clock.elapsed()
        while now - self.last_tick >= TICK_MS:
            self.last_tick += TICK_MS
            target = self.pending
            self.pending = 0.0
            self.current = target * 0.75 + self.current * 0.25
            self.levels.append((self.current, target >= CLIP_LEVEL))
        self.frac = (now - self.last_tick) / TICK_MS
        self.update()

    # -- painting --
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        panel = QLinearGradient(0, r.top(), 0, r.bottom())
        panel.setColorAt(0.0, QColor(255, 255, 255, 22))
        panel.setColorAt(1.0, QColor(255, 255, 255, 8))
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(QBrush(panel))
        p.drawRoundedRect(r, 14, 14)

        left = r.left() + 14
        right = r.right() - 14
        mid = r.center().y()
        half = r.height() * 0.38

        # faint centre line
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(QRectF(left, mid - 0.5, right - left, 1), QColor(255, 255, 255, 14))

        def vgrad(edge, centre, rgb):
            g = QLinearGradient(0, mid - half, 0, mid + half)
            g.setColorAt(0.0, QColor(rgb[0], rgb[1], rgb[2], edge))
            g.setColorAt(0.5, QColor(rgb[0], rgb[1], rgb[2], centre))
            g.setColorAt(1.0, QColor(rgb[0], rgb[1], rgb[2], edge))
            return QBrush(g)

        bright = vgrad(130, 232, (240, 243, 245))
        dim = vgrad(60, 140, (240, 243, 245))
        clipb = vgrad(150, 240, (255, 118, 150))

        def bar(x, v, brush, opacity):
            h = max(2.0, half * v)
            p.setOpacity(opacity)
            p.setBrush(brush)
            p.drawRoundedRect(QRectF(x, mid - h, BAR_W, h * 2), BAR_W / 2.0, BAR_W / 2.0)

        if self.review_peaks is not None:
            head = left + (right - left) * self.play_frac
            src = self.review_peaks
            count = len(src)
            n = max(1, int((right - left) // PITCH))
            for i in range(n):
                a = i * count // n
                b = max(a + 1, (i + 1) * count // n)
                raw = max(src[a:b])
                v = shape_level(raw)
                x = left + i * PITCH + 2
                played = (x + BAR_W / 2.0) <= head
                if raw >= CLIP_LEVEL:
                    brush = clipb
                else:
                    brush = bright if played else dim
                bar(x, v, brush, 1.0 if v > 0.02 else 0.5)
        else:
            head = left + (right - left) * 0.62

            # dotted baseline in front of the playhead (nothing recorded yet)
            p.setOpacity(1.0)
            p.setBrush(QColor(238, 241, 243, 70))
            x = head + PITCH
            while x < right:
                p.drawRect(QRectF(x, mid - 1, BAR_W, 2))
                x += PITCH

            # history bars, newest next to the playhead, fading out on the left
            n_vis = int((head - left) // PITCH) + 2
            hist = list(self.levels)[-n_vis:]
            hist.reverse()                       # index 0 = newest
            shift = self.frac * PITCH

            p.setClipRect(QRectF(left, r.top() + 3, head - left, r.height() - 6))
            for k, (raw, clipped) in enumerate(hist):
                v = shape_level(raw)
                x = head - (k + 1) * PITCH + BAR_GAP - shift
                fade = min(1.0, max(0.0, (x - left) / 70.0))
                if fade <= 0.0:
                    continue
                brush = clipb if clipped else bright
                bar(x, v, brush, fade * (1.0 if v > 0.02 else 0.45))
            p.setClipping(False)

        p.setOpacity(1.0)
        p.setPen(Qt.PenStyle.NoPen)

        # hover guide while seekable
        if self.seekable and self.hover_x is not None and not self.dragging:
            hx = min(right, max(left, self.hover_x))
            p.setPen(QPen(QColor(255, 255, 255, 52), 1))
            p.drawLine(QPointF(hx, r.top() + 10), QPointF(hx, r.bottom() - 10))
            p.setPen(Qt.PenStyle.NoPen)

        # playhead with a soft glow and a knob on top
        glow = QColor(PLAYHEAD)
        glow.setAlpha(40)
        p.setBrush(glow)
        p.drawRoundedRect(QRectF(head - 3, r.top() + 5, 6, r.height() - 10), 3, 3)
        p.setBrush(PLAYHEAD)
        p.drawRoundedRect(QRectF(head - 1, r.top() + 7, 2, r.height() - 14), 1, 1)
        p.drawEllipse(QPointF(head, r.top() + 7), 3.5, 3.5)
        p.end()


class GlassMenu(QMenu):
    def __init__(self, title="", parent=None):
        super().__init__(title, parent)
        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("""
            QMenu {
                background-color: rgba(34, 38, 42, 245);
                color: #F2F4F5;
                border: 1px solid rgba(255,255,255,55);
                border-radius: 10px;
                padding: 6px;
                font-family: "Segoe UI";
                font-size: 13px;
            }
            QMenu::item {
                background: transparent;
                padding: 8px 26px 8px 14px;
                margin: 1px 0px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background-color: rgba(255,255,255,34);
            }
            QMenu::item:disabled {
                color: rgba(242,244,245,110);
            }
            QMenu::separator {
                height: 1px;
                background: rgba(255,255,255,30);
                margin: 5px 8px;
            }
        """)


DIALOG_QSS = """
QLabel { background: transparent; color: #EEF0F1; font-family: "Segoe UI"; }
#dlgTitle { font-size: 13px; font-weight: 600; }
#dlgText { font-size: 14px; font-weight: 600; }
#dlgInfo { font-size: 13px; color: rgba(238,240,241,170); }
#closeButton {
    background: transparent; border: none; border-radius: 0px;
    color: rgba(245,246,247,190); font-size: 18px;
}
#closeButton:hover { background: rgba(232,17,35,210); color: white; }
QPushButton[role] {
    font-family: "Segoe UI"; font-size: 13px;
    min-width: 76px; padding: 7px 16px;
    border-radius: 8px; color: #F4F5F6;
    background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,34);
}
QPushButton[role]:hover { background: rgba(255,255,255,38); }
QPushButton[role]:pressed { background: rgba(255,255,255,56); }
QPushButton[role="primary"] {
    background: rgba(237,39,50,215); border: 1px solid rgba(255,255,255,50);
}
QPushButton[role="primary"]:hover { background: rgba(247,59,70,235); }
QPushButton[role="primary"]:pressed { background: rgba(200,30,40,235); }
QPushButton[role]:focus { border: 1px solid rgba(255,255,255,130); }
"""


class GlassDialog(QDialog):
    """Message box that matches the app's frosted glass look."""

    def __init__(self, parent, title, text, info="", kind="info",
                 buttons=(("OK", "primary"),)):
        super().__init__(parent)
        self.choice = None
        self._ready = False

        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(430)
        self.setStyleSheet(DIALOG_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = TitleBar()
        bar.setFixedHeight(40)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 0, 0, 0)
        bl.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(find_icon().pixmap(16, 16))
        t = QLabel(title)
        t.setObjectName("dlgTitle")
        close = QPushButton("×")
        close.setObjectName("closeButton")
        close.setFixedSize(44, 40)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(self.reject)
        bl.addWidget(icon)
        bl.addWidget(t)
        bl.addStretch()
        bl.addWidget(close)
        outer.addWidget(bar)

        body = QHBoxLayout()
        body.setContentsMargins(24, 22, 24, 6)
        body.setSpacing(16)
        glyph = QLabel()
        glyph.setPixmap(make_glyph(kind))
        glyph.setFixedSize(40, 40)
        texts = QVBoxLayout()
        texts.setSpacing(5)
        main = QLabel(text)
        main.setObjectName("dlgText")
        main.setWordWrap(True)
        texts.addWidget(main)
        if info:
            sub = QLabel(info)
            sub.setObjectName("dlgInfo")
            sub.setWordWrap(True)
            texts.addWidget(sub)
        texts.addStretch()
        body.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(texts, 1)
        outer.addLayout(body)

        row = QHBoxLayout()
        row.setContentsMargins(24, 14, 24, 20)
        row.setSpacing(8)
        row.addStretch()
        default_set = False
        for label, role in buttons:
            b = QPushButton(label)
            b.setProperty("role", role)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, l=label: self.finish(l))
            if role == "primary" and not default_set:
                b.setDefault(True)
                b.setAutoDefault(True)
                default_set = True
            row.addWidget(b)
        outer.addLayout(row)

    def finish(self, label):
        self.choice = label
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        if self._ready:
            return
        self._ready = True
        apply_backdrop(self)
        self.adjustSize()
        update_mask(self)
        parent = self.parentWidget()
        if parent:
            fg = self.frameGeometry()
            fg.moveCenter(parent.window().frameGeometry().center())
            self.move(fg.topLeft())
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ready:
            update_mask(self)

    def paintEvent(self, event):
        paint_glass(self, extra_alpha=45)


def ask(parent, title, text, info="", kind="info", buttons=(("OK", "primary"),)):
    dlg = GlassDialog(parent, title, text, info, kind, buttons)
    dlg.exec()
    return dlg.choice


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

MAIN_QSS = """
QWidget { color: #EEF0F1; font-family: "Segoe UI"; }
QLabel { background: transparent; }
QToolTip {
    background-color: rgba(34, 38, 42, 245);
    color: #EEF0F1;
    border: 1px solid rgba(255,255,255,55);
    padding: 4px 8px;
}

#title {
    color: rgba(247,248,249,235);
    font-size: 13px;
    font-weight: 600;
}
#timer {
    color: rgba(246,247,248,240);
    font-size: 26px;
    font-weight: 300;
}
#hint {
    color: rgba(238,240,241,125);
    font-size: 12px;
}
#menuButton, #windowButton, #closeButton {
    background: transparent;
    border: none;
    border-radius: 8px;
}
#menuButton:hover { background: rgba(255,255,255,28); }
#windowButton, #closeButton {
    border-radius: 0px;
    font-size: 18px;
    color: rgba(245,246,247,190);
}
#windowButton:hover { background: rgba(255,255,255,26); }
#closeButton:hover { background: rgba(232,17,35,210); color: white; }
"""


class Recorder(QWidget):
    def __init__(self):
        super().__init__()

        self.app_icon = find_icon()
        self.setWindowIcon(self.app_icon)
        self.setWindowTitle(APP_NAME)
        self.resize(760, 240)
        self.setMinimumSize(520, 212)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.file_path = None
        self.saved = True
        self.recording = False

        self.backend = None
        self.devices_list = []
        self.input = None
        self.capturing = False
        self.cap_rate = 48000
        self.cap_channels = 1

        self.pcm_rate = None        # format of the data currently in self.frames
        self.pcm_channels = None
        self.frames = bytearray()

        self.bytes_got = 0
        self.max_peak = 0.0
        self.session = 0
        self.warn_active = False

        # review / playback state
        self.playing = False
        self.play_paused = False
        self.play_pos = 0           # byte offset into self.frames
        self.scrubbing = False
        self.peaks_dirty = True

        self.glow = 0.0
        self.recovery = Recovery()

        self._styled = False
        self._last_text = ""
        self.elapsed = QElapsedTimer()
        self.elapsed_before_pause = 0

        self.build_ui()
        self.setup_audio()
        self.setup_shortcuts()

        if self.backend.name == "qt":
            self.media_devices = QMediaDevices(self)
            self.media_devices.audioInputsChanged.connect(self.on_inputs_changed)

        self.clock = QTimer(self)
        self.clock.setInterval(100)
        self.clock.timeout.connect(self.update_timer)

        # Poll the microphone instead of relying on readyRead, which some
        # Windows drivers never emit.
        self.poll = QTimer(self)
        self.poll.setInterval(15)
        self.poll.timeout.connect(self.read_audio)

        self.playback = Playback(self)
        self.playback.finished.connect(self.on_play_finished)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(33)
        self.play_timer.timeout.connect(self.tick_play)

        self.glow_anim = QVariantAnimation(self)
        self.glow_anim.setDuration(380)
        self.glow_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.glow_anim.valueChanged.connect(self._on_glow)

        self.update_controls()

    # ---------- UI ----------

    def build_ui(self):
        self.setStyleSheet(MAIN_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = TitleBar()
        bar.setFixedHeight(38)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 0, 0, 0)
        bl.setSpacing(6)

        self.menu_button = QPushButton()
        self.menu_button.setObjectName("menuButton")
        self.menu_button.setFixedSize(30, 30)
        self.menu_button.setIcon(self.app_icon)
        self.menu_button.setIconSize(QSize(18, 18))
        self.menu_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.menu_button.clicked.connect(self.show_menu)

        self.title = QLabel(APP_NAME)
        self.title.setObjectName("title")

        minimize = QPushButton("—")
        minimize.setObjectName("windowButton")
        minimize.setFixedSize(42, 38)
        minimize.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        minimize.clicked.connect(self.showMinimized)

        close = QPushButton("×")
        close.setObjectName("closeButton")
        close.setFixedSize(42, 38)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(self.close)

        bl.addWidget(self.menu_button)
        bl.addWidget(self.title)
        bl.addStretch()
        bl.addWidget(minimize)
        bl.addWidget(close)
        layout.addWidget(bar)

        content = QVBoxLayout()
        content.setContentsMargins(18, 8, 18, 16)
        content.setSpacing(8)
        layout.addLayout(content, 1)

        top = QHBoxLayout()
        top.setSpacing(12)

        self.timer_label = QLabel("00:00.0")
        self.timer_label.setObjectName("timer")
        top.addWidget(self.timer_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.rec_dot = RecDot()
        top.addWidget(self.rec_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self.hint = QLabel("")
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.hint, 1)

        self.play_btn = PlayButton()
        self.play_btn.setToolTip("Play / Pause (P)")
        self.play_btn.clicked.connect(self.toggle_playback)
        top.addWidget(self.play_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.record_button = RecordButton()
        self.record_button.setToolTip("Record / Pause (Space)")
        self.record_button.clicked.connect(self.toggle_recording)
        top.addWidget(self.record_button, 0, Qt.AlignmentFlag.AlignVCenter)

        content.addLayout(top)

        self.waveform = Waveform()
        self.waveform.seeked.connect(self.on_seek)
        content.addWidget(self.waveform, 1)

        self.grip = Grip(self)
        self.grip.setFixedSize(12, 12)
        self.grip.raise_()

        self.set_hint(self.idle_hint())

    def idle_hint(self):
        if self.input is None:
            return "No microphone found"
        if self.frames:
            return f"Space to record  ·  P to play  ·  {self.input.name}"
        return f"Press Space to record  ·  {self.input.name}"

    def set_hint(self, text, warn=False):
        self.warn_active = warn
        self.hint.setText(text)
        self.hint.setStyleSheet("color: rgba(255,176,120,235);" if warn else "")

    def refresh_title(self):
        if self.file_path:
            name = os.path.basename(self.file_path)
        elif self.frames:
            name = "Untitled recording"
        else:
            name = APP_NAME
        if self.frames and not self.saved:
            name += "  •"
        self.title.setText(name)
        self.setWindowTitle(name)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._styled:
            self._styled = True
            apply_backdrop(self)
            update_mask(self)
            self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grip = getattr(self, "grip", None)
        if grip is not None:
            grip.move(self.width() - 15, self.height() - 15)
            grip.raise_()
        if self._styled:
            update_mask(self)

    def paintEvent(self, event):
        paint_glass(self, glow=self.glow)

    def _on_glow(self, v):
        self.glow = float(v)
        self.update()

    def animate_glow(self, on):
        self.glow_anim.stop()
        self.glow_anim.setStartValue(self.glow)
        self.glow_anim.setEndValue(1.0 if on else 0.0)
        self.glow_anim.start()

    def update_controls(self):
        can = bool(self.frames) and not self.recording
        self.play_btn.setEnabled(can)
        self.waveform.set_seekable(can)

    # ---------- audio setup ----------

    def setup_audio(self):
        use_sd = HAVE_SD and BACKEND != "qt"
        self.backend = SdCapture() if use_sd else QtCapture()
        try:
            self.devices_list = self.backend.devices()
        except Exception:
            self.devices_list = []
        self.input = pick_default(self.devices_list, self.backend.default_name())
        self.set_hint(self.idle_hint())

    def on_inputs_changed(self):
        self.devices_list = self.backend.devices()
        names = [d.name for d in self.devices_list]
        if self.input is None or self.input.name not in names:
            self.stop_capture()
            self.input = pick_default(self.devices_list, self.backend.default_name())
        else:
            self.input = next(d for d in self.devices_list if d.name == self.input.name)
        if not self.recording:
            self.set_hint(self.idle_hint())

    def refresh_devices(self):
        """Re-scan inputs (PortAudio only sees devices present at startup)."""
        if self.recording:
            self.pause_recording()
        self.stop_capture()
        if sd is not None:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        try:
            self.devices_list = self.backend.devices()
        except Exception:
            self.devices_list = []
        names = [d.name for d in self.devices_list]
        if self.input is None or self.input.name not in names:
            self.input = pick_default(self.devices_list, self.backend.default_name())
        else:
            self.input = next(d for d in self.devices_list if d.name == self.input.name)
        self.set_hint(self.idle_hint())

    def select_device(self, dev):
        if self.recording:
            self.pause_recording()
        self.stop_capture()
        self.input = dev
        self.set_hint(self.idle_hint())

    def setup_shortcuts(self):
        for keys, slot in (
            ("Space", self.toggle_recording),
            ("P", self.toggle_playback),
            ("Left", lambda: self.skip(-5)),
            ("Right", lambda: self.skip(5)),
            ("Shift+S", self.save_file),
            ("Ctrl+S", self.save_file),
            ("Ctrl+Shift+S", lambda: self.save_file(True)),
        ):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

    # ---------- menu ----------

    def show_menu(self):
        menu = GlassMenu(parent=self)

        def add(target, label, slot):
            a = QAction(label, target)
            a.triggered.connect(lambda _=False, s=slot: s())
            target.addAction(a)
            return a

        has_audio = bool(self.frames)
        playing_now = self.playing and not self.play_paused

        add(menu, "New recording", self.new_recording)
        add(menu, "Open WAV…", self.open_wav)
        add(menu, "Save\tShift+S", self.save_file).setEnabled(has_audio)
        add(menu, "Save as…\tCtrl+Shift+S", lambda: self.save_file(True)).setEnabled(has_audio)
        menu.addSeparator()
        add(menu, ("Pause" if self.recording else "Record") + "\tSpace",
            self.toggle_recording)
        add(menu, ("Pause playback" if playing_now else "Play") + "\tP",
            self.toggle_playback).setEnabled(has_audio and not self.recording)

        mic = GlassMenu("Microphone", menu)
        if self.devices_list:
            for dev in self.devices_list:
                mark = "●   " if dev is self.input else "      "
                a = QAction(mark + dev.name, mic)
                a.triggered.connect(lambda _=False, d=dev: self.select_device(d))
                mic.addAction(a)
        else:
            a = QAction("No microphones found", mic)
            a.setEnabled(False)
            mic.addAction(a)
        if self.backend.name == "sd":
            mic.addSeparator()
            add(mic, "Refresh list", self.refresh_devices)
        menu.addMenu(mic)

        menu.addSeparator()
        add(menu, "About", self.show_about)
        add(menu, "Quit", self.close)

        menu.exec(self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height() + 4)))

    def show_about(self):
        ask(self, APP_NAME, "Echo",
            "A minimal microphone recorder.\n\n"
            "Space — Record / Pause\n"
            "P — Play / Pause\n"
            "← → — Skip 5 seconds\n"
            "Shift+S — Save    Ctrl+Shift+S — Save as",
            "info")

    # ---------- recording ----------

    def ensure_capture(self):
        if self.capturing:
            return True
        if not self.input:
            ask(self, APP_NAME, "No microphone was found.",
                "Plug one in, or pick one from the menu > Microphone.", "warning")
            return False

        order = [self.input] + sorted(
            [d for d in self.devices_list if d is not self.input],
            key=lambda d: d.virtual)
        notes = []
        for dev in order:
            if self.backend.start(dev):
                if self.frames and self.pcm_rate is not None and (
                    self.pcm_rate != self.backend.rate
                    or self.pcm_channels != self.backend.out_channels
                ):
                    self.backend.stop()
                    notes.append(f"{dev.name}: different format than the open file")
                    continue
                self.input = dev
                self.cap_rate = self.backend.rate
                self.cap_channels = self.backend.out_channels
                self.capturing = True
                return True
            notes.append(f"{dev.name}: " + ("; ".join(self.backend.log[-2:]) or "could not open"))

        if notes and all("different format" in n for n in notes):
            ask(self, APP_NAME, "This file can't be extended.",
                "It uses a different audio format than your microphone. "
                "Start a new recording instead.", "info")
        else:
            info = "\n".join(n[:120] for n in notes[:3])
            if self.backend.name == "qt":
                info += ("\n\nTip: run  pip install sounddevice  and restart the app. "
                         "It opens Voicemeeter devices much more reliably.")
            ask(self, APP_NAME, "The microphone could not be started.", info, "error")
        self.set_hint(self.idle_hint())
        return False

    def read_audio(self):
        if not self.capturing:
            return
        data = self.backend.read()
        if not data:
            return
        self.bytes_got += len(data)

        # mirror the audio to disk so a crash doesn't lose the take
        if self.recovery.f is None and not self.recovery.dead:
            self.recovery.begin(self.pcm_rate, self.pcm_channels, self.frames)
        self.recovery.write(data)

        self.frames.extend(data)
        self.peaks_dirty = True

        level = peak_level(data)
        self.waveform.push(level)
        if level > self.max_peak:
            self.max_peak = level
        if self.warn_active and level > 0.004:
            self.set_hint("Press Space to pause")
        if self.saved:
            self.saved = False
            self.refresh_title()

    def toggle_recording(self):
        if self.recording:
            self.pause_recording()
        else:
            self.resume_recording()

    def resume_recording(self):
        self.stop_playback()
        if self.capturing:
            self.backend.resume()
        elif not self.ensure_capture():
            return

        self.pcm_rate = self.cap_rate
        self.pcm_channels = self.cap_channels

        self.bytes_got = 0
        self.max_peak = 0.0
        self.session += 1
        session = self.session
        self.play_pos = 0

        self.recording = True
        self.elapsed.start()
        self.waveform.start()
        self.clock.start()
        self.poll.start()
        self.record_button.set_active(True)
        self.animate_glow(True)
        self.update_controls()
        self.update_timer()
        self.set_hint(f"Press Space to pause  ·  {self.input.name}")

        QTimer.singleShot(2000, lambda: self.check_signal(session))

    def check_signal(self, session):
        if not self.recording or session != self.session:
            return
        name = self.input.name if self.input else "microphone"
        if self.bytes_got == 0:
            self.set_hint(
                f"No audio from “{name}”. Pick another input in the menu, or allow "
                "mic access in Windows privacy settings.", warn=True)
        elif self.max_peak < 0.002:
            self.set_hint(
                f"“{name}” is silent. Is anything playing into that bus? "
                "Or pick another input in the menu.", warn=True)

    def pause_recording(self):
        self.read_audio()                    # flush whatever is buffered
        self.recovery.flush()
        self.poll.stop()
        self.backend.pause()

        self.elapsed_before_pause += self.elapsed.elapsed()
        self.recording = False
        self.clock.stop()
        self.waveform.stop()
        self.record_button.set_active(False)
        self.animate_glow(False)
        self.update_controls()
        self.set_hint(self.idle_hint())
        self.update_timer()

    def stop_capture(self):
        if self.recording:
            self.pause_recording()
        self.poll.stop()
        if self.backend:
            self.backend.stop()
        self.capturing = False

    def update_timer(self):
        if self.waveform.reviewing:
            fb = self.frame_bytes()
            rate = max(1, self.pcm_rate or 48000)
            ms = (self.play_pos // fb) * 1000 // rate
        else:
            ms = self.elapsed_before_pause
            if self.recording:
                ms += self.elapsed.elapsed()

        self.rec_dot.set_on(self.recording and (ms // 500) % 2 == 0)

        text = fmt_time(ms)
        if text != self._last_text:
            self._last_text = text
            self.timer_label.setText(text)

    # ---------- review / playback ----------

    def frame_bytes(self):
        return 2 * (self.pcm_channels or 1)

    def total_ms(self):
        rate = max(1, self.pcm_rate or 48000)
        return len(self.frames) // self.frame_bytes() * 1000 // rate

    def ensure_review(self):
        """Switch the waveform to the full-take view (computed on demand)."""
        if self.waveform.reviewing and not self.peaks_dirty:
            return
        self.waveform.show_review(compute_peaks(self.frames, PEAK_BINS))
        self.peaks_dirty = False
        self.waveform.set_position(self.play_pos / max(1, len(self.frames)))

    def toggle_playback(self):
        if self.recording or not self.frames:
            return
        if not self.playing:
            self.start_playback()
        elif self.play_paused:
            self.playback.resume()
            self.play_paused = False
            self.play_btn.set_playing(True)
            self.play_timer.start()
            self.set_hint("Playing  ·  click the waveform to seek")
        else:
            self.playback.pause()
            self.play_paused = True
            self.play_btn.set_playing(False)
            self.play_timer.stop()
            self.set_hint(f"Paused  ·  {fmt_time(self.total_ms())} long  ·  P to resume")

    def start_playback(self):
        if self.recording or not self.frames:
            return
        self.ensure_review()
        fb = self.frame_bytes()
        total = len(self.frames) // fb * fb
        pos = self.play_pos if self.play_pos < total - fb else 0
        err = self.playback.start(self.frames, self.pcm_rate, self.pcm_channels, pos)
        if err:
            ask(self, APP_NAME, "Playback could not be started.", err, "error")
            return
        self.play_pos = pos
        self.playing = True
        self.play_paused = False
        self.scrubbing = False
        self.play_btn.set_playing(True)
        self.play_timer.start()
        self.set_hint("Playing  ·  click the waveform to seek")

    def stop_playback(self):
        if not self.playing:
            return
        self.playback.stop()
        self.playing = False
        self.play_paused = False
        self.scrubbing = False
        self.play_timer.stop()
        self.play_btn.set_playing(False)

    def tick_play(self):
        if not self.playing:
            return
        if not self.scrubbing:
            self.play_pos = self.playback.position_bytes()
            self.waveform.set_position(self.play_pos / max(1, len(self.frames)))
        self.update_timer()

    def on_play_finished(self):
        if not self.playing:
            return
        self.stop_playback()
        self.play_pos = 0
        self.waveform.set_position(0.0)
        self.update_timer()
        self.set_hint(self.idle_hint())

    def on_seek(self, frac, done):
        if self.recording or not self.frames:
            return
        self.ensure_review()
        fb = self.frame_bytes()
        total = len(self.frames) // fb * fb
        pos = int(frac * total) // fb * fb
        pos = min(pos, max(0, total - fb))
        self.apply_seek(pos, done)

    def skip(self, seconds):
        if self.recording or not self.frames:
            return
        self.ensure_review()
        fb = self.frame_bytes()
        rate = max(1, self.pcm_rate or 48000)
        total = len(self.frames) // fb * fb
        if self.playing:
            self.play_pos = self.playback.position_bytes()
        pos = self.play_pos + int(seconds * rate) * fb
        pos = max(0, min(pos, max(0, total - fb)))
        self.apply_seek(pos, True)

    def apply_seek(self, pos, done):
        self.scrubbing = not done
        self.play_pos = pos
        self.waveform.set_position(pos / max(1, len(self.frames)))
        self.update_timer()
        if done and self.playing:
            err = self.playback.seek(pos)
            if err:
                self.stop_playback()
                ask(self, APP_NAME, "Playback could not be started.", err, "error")

    # ---------- files ----------

    def new_recording(self):
        if not self.confirm_unsaved():
            return
        self.stop_playback()
        self.stop_capture()
        self.frames = bytearray()
        self.file_path = None
        self.saved = True
        self.pcm_rate = self.pcm_channels = None
        self.elapsed_before_pause = 0
        self.play_pos = 0
        self.peaks_dirty = True
        self._last_text = ""
        self.recovery.clear()
        self.waveform.clear()
        self.refresh_title()
        self.update_controls()
        self.update_timer()
        self.set_hint(self.idle_hint())

    def open_wav(self):
        if not self.confirm_unsaved():
            return

        path, _ = QFileDialog.getOpenFileName(self, "Open WAV", "", "WAV audio (*.wav)")
        if not path:
            return

        try:
            with wave.open(path, "rb") as f:
                channels = f.getnchannels()
                width = f.getsampwidth()
                rate = f.getframerate()
                raw = f.readframes(f.getnframes())
        except Exception as e:
            ask(self, APP_NAME, "Could not open the WAV file.",
                f"{e}\n\nOnly PCM WAV files can be opened.", "error")
            return

        if width not in (1, 2, 3, 4):
            ask(self, APP_NAME, "Unsupported WAV format.",
                "Only 8, 16, 24 and 32-bit PCM WAV files can be opened.", "warning")
            return

        data = pcm_to_int16(raw, width)
        del raw

        self.stop_playback()
        self.stop_capture()
        self.recovery.clear()
        self.frames = bytearray(data)
        self.pcm_rate = rate
        self.pcm_channels = channels
        self.file_path = path
        self.saved = True

        duration = len(data) / max(1, channels * 2 * rate)
        self.elapsed_before_pause = int(duration * 1000)
        self.play_pos = 0
        self.peaks_dirty = True
        self.ensure_review()
        self.refresh_title()
        self.update_controls()
        self.update_timer()
        self.set_hint(self.idle_hint())

    def save_file(self, as_new=False):
        if not self.frames:
            return False

        path = None if as_new else self.file_path
        if not path:
            start = self.file_path or time.strftime("Recording %Y-%m-%d %H.%M.%S.wav")
            path, _ = QFileDialog.getSaveFileName(
                self, "Save recording", start, "WAV audio (*.wav)"
            )
            if not path:
                return False
            if not path.lower().endswith(".wav"):
                path += ".wav"

        # write beside the target first so a failed save can't damage an
        # existing file
        tmp = path + ".tmp"
        try:
            with wave.open(tmp, "wb") as f:
                f.setnchannels(self.pcm_channels or self.cap_channels)
                f.setsampwidth(2)
                f.setframerate(self.pcm_rate or self.cap_rate)
                f.writeframes(self.frames)
            os.replace(tmp, path)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            ask(self, APP_NAME, "Could not save the recording.", str(e), "error")
            return False

        self.file_path = path
        self.saved = True
        self.recovery.clear()
        self.refresh_title()
        return True

    def confirm_unsaved(self):
        if self.saved or not self.frames:
            return True

        if self.recording:
            self.pause_recording()
        self.stop_playback()

        choice = ask(
            self, APP_NAME,
            "This recording has not been saved.",
            "Save it before continuing?",
            "warning",
            (("Save", "primary"), ("Discard", "normal"), ("Cancel", "normal")),
        )

        if choice == "Save":
            return self.save_file()
        return choice == "Discard"

    def offer_recovery(self):
        """At startup: offer to bring back a recording that was never saved."""
        if self.frames or not self.recovery.available():
            return
        got = self.recovery.read()
        if not got or not got[0]:
            self.recovery.clear()
            return
        data, rate, channels = got
        secs = len(data) / (2 * channels * rate)

        choice = ask(
            self, APP_NAME, "Recover your last recording?",
            f"Echo closed before a {fmt_time(secs * 1000)} recording was saved.",
            "info",
            (("Recover", "primary"), ("Discard", "normal")),
        )
        if choice == "Discard":
            self.recovery.clear()
            return
        if choice != "Recover":
            return                          # dialog closed: ask again next time

        self.frames = bytearray(data)
        self.pcm_rate = rate
        self.pcm_channels = channels
        self.file_path = None
        self.saved = False
        self.elapsed_before_pause = int(secs * 1000)
        self.play_pos = 0
        self.peaks_dirty = True
        self.ensure_review()
        self.refresh_title()
        self.update_controls()
        self.update_timer()
        self.set_hint(self.idle_hint())

    def closeEvent(self, event):
        if not self.confirm_unsaved():
            event.ignore()
            return
        self.stop_playback()
        self.stop_capture()
        self.recovery.clear()               # saved, empty or deliberately discarded
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(find_icon())
    app.setStyle("Fusion")

    window = Recorder()
    window.show()
    QTimer.singleShot(300, window.offer_recovery)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
