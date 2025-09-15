from flask import Flask, render_template, jsonify, Response, stream_with_context, request
from flask_socketio import SocketIO, emit
import xmlrpc.client
import threading
import time
from datetime import datetime
import os
import json
import sys
import subprocess
import re
import asyncio
import logging
from fractions import Fraction

import av
from av.audio.resampler import AudioResampler
import math
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack

# --- Logging / debug mode ---
DEBUG_MODE = ("--debug" in sys.argv)
logging.basicConfig(level=logging.DEBUG if DEBUG_MODE else logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)
logging.getLogger("engineio").setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)
logging.getLogger("socketio").setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)

# File logging only (do NOT redirect stdout/stderr, so interactive prompts remain visible)
try:
    _debug_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
    _fh = logging.FileHandler(_debug_log_path, encoding="utf-8")
    _fh.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception as _e:
    logging.error(f"failed to initialize file logging: {_e}")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'flrig-web-remote-secret'
# Silence Socket.IO internal logging unless debug
socketio = SocketIO(app, cors_allowed_origins="*", logger=DEBUG_MODE, engineio_logger=DEBUG_MODE)

# flrig connection settings
FLRIG_HOST = "localhost"
FLRIG_PORT = 12345
server_url = f"http://{FLRIG_HOST}:{FLRIG_PORT}/RPC2"

# --- Audio configuration (ALSA only) ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "flrigWebRemote.config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: failed to read config: {e}")
    return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"Saved configuration to {CONFIG_PATH}")
    except Exception as e:
        print(f"Error saving config: {e}")


def enumerate_input_devices_alsa():
    """
    Use 'arecord -l' to list input-capable ALSA devices.
    Returns list of dicts: { 'card': int, 'device': int, 'name': str, 'alsa_device': 'plughw:card,device' }
    """
    devices = []
    try:
        out = subprocess.check_output(["arecord", "-l"], stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        print(f"ALSA enumeration failed (arecord -l): {e}")
        return devices

    # Example:
    # card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
    pattern = re.compile(r"card\s+(\d+):\s+([^\[]+)\[([^\]]+)\],\s+device\s+(\d+):\s+([^\[]+)\[([^\]]+)\]")
    for line in out.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        card_num = int(m.group(1))
        card_name = m.group(3).strip()
        dev_num = int(m.group(4))
        dev_name = m.group(6).strip()
        display_name = f"{card_name} / {dev_name}".strip()
        alsa_dev = f"plughw:{card_num},{dev_num}"
        devices.append({
            "card": card_num,
            "device": dev_num,
            "name": display_name,
            "alsa_device": alsa_dev
        })

    # Prefer USB devices first
    devices.sort(key=lambda d: (0 if "usb" in d["name"].lower() else 1, d["card"], d["device"]))
    return devices


def enumerate_playback_devices_alsa():
    """
    Use 'aplay -l' to list output-capable ALSA devices.
    Returns list of dicts: { 'card': int, 'device': int, 'name': str, 'alsa_device': 'plughw:card,device' }
    """
    devices = []
    try:
        out = subprocess.check_output(["aplay", "-l"], stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        print(f"ALSA enumeration failed (aplay -l): {e}")
        return devices

    pattern = re.compile(r"card\s+(\d+):\s+([^\[]+)\[([^\]]+)\],\s+device\s+(\d+):\s+([^\[]+)\[([^\]]+)\]")
    for line in out.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        card_num = int(m.group(1))
        card_name = m.group(3).strip()
        dev_num = int(m.group(4))
        dev_name = m.group(6).strip()
        display_name = f"{card_name} / {dev_name}".strip()
        alsa_dev = f"plughw:{card_num},{dev_num}"
        devices.append({
            "card": card_num,
            "device": dev_num,
            "name": display_name,
            "alsa_device": alsa_dev
        })

    # Prefer USB devices first
    devices.sort(key=lambda d: (0 if "usb" in d["name"].lower() else 1, d["card"], d["device"]))
    return devices


def prompt_select_device(devices, title="input"):
    """Interactive prompt to select device; returns selected device dict or None."""
    if not devices:
        print(f"No {title}-capable ALSA devices found.")
        return None

    print(f"\nAvailable {title} audio devices (ALSA):")
    for i, d in enumerate(devices):
        usb_tag = " [USB]" if "usb" in d["name"].lower() else ""
        print(f"  {i}) {d['name']}{usb_tag}  -> {d['alsa_device']}")

    while True:
        sel = input(f"Select {title} device number (or press Enter to pick first listed): ").strip()
        if sel == "":
            return devices[0]
        if sel.isdigit():
            n = int(sel)
            if 0 <= n < len(devices):
                return devices[n]
        print("Invalid selection. Try again.")


def auto_pick_device(devices):
    """Non-interactive fallback: first USB if available, else first device."""
    if not devices:
        return None
    return devices[0]


def validate_stored_capture(cfg_audio_in):
    """
    Validate stored ALSA input device by a 1-second silent capture.
    """
    dev = cfg_audio_in.get("alsa_device") if cfg_audio_in else None
    if not dev:
        return False
    try:
        subprocess.run(
            ["arecord", "-D", dev, "-f", "S16_LE", "-d", "1", "-q", "/dev/null"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return True
    except Exception:
        return False


def validate_stored_playback(cfg_audio_out):
    """
    Validate stored ALSA output device by a 1-second silent playback from /dev/zero.
    """
    dev = cfg_audio_out.get("alsa_device") if cfg_audio_out else None
    if not dev:
        return False
    try:
        subprocess.run(
            ["aplay", "-D", dev, "-f", "S16_LE", "-c", "1", "-r", "48000", "-d", "1", "-q", "/dev/zero"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return True
    except Exception:
        return False


def ensure_audio_config(force_reconfigure: bool = False):
    """
    Ensure we have ALSA capture (audio_in) and playback (audio_out) devices configured.
    Config shape:
      {
        "audio_in":  { "device_name": "...", "alsa_device": "plughw:X,Y" },
        "audio_out": { "device_name": "...", "alsa_device": "plughw:X,Y" }
      }
    """
    cfg = load_config()
    changed = False

    # ---------- Ensure CAPTURE device (audio_in) ----------
    need_in = (
            force_reconfigure or
            ("audio_in" not in cfg) or
            ("alsa_device" not in cfg.get("audio_in", {})) or
            (not validate_stored_capture(cfg.get("audio_in")))
    )

    if need_in:
        in_list = enumerate_input_devices_alsa()
        if not in_list:
            print("No input-capable ALSA devices found. Proceeding without audio_in configuration.")
        else:
            picked_in = None
            # Only prompt if explicitly reconfiguring; otherwise auto-pick to avoid blocking
            if force_reconfigure and sys.stdin.isatty():
                picked_in = prompt_select_device(in_list, title="input")
            else:
                picked_in = auto_pick_device(in_list)
                if picked_in:
                    print(f"Auto-selected input {picked_in['name']} -> {picked_in['alsa_device']}")
            if picked_in:
                cfg["audio_in"] = {
                    "device_name": picked_in["name"],
                    "alsa_device": picked_in["alsa_device"]
                }
                changed = True
                print(f"Selected ALSA input: {picked_in['name']} -> {picked_in['alsa_device']}")
    else:
        print(f"Using configured ALSA input: {cfg['audio_in']['device_name']} -> {cfg['audio_in']['alsa_device']}")

    # ---------- Ensure PLAYBACK device (audio_out) ----------
    need_out = (
            force_reconfigure or
            ("audio_out" not in cfg) or
            ("alsa_device" not in cfg.get("audio_out", {})) or
            (not validate_stored_playback(cfg.get("audio_out")))
    )

    if need_out:
        out_list = enumerate_playback_devices_alsa()
        if not out_list:
            print("No output-capable ALSA devices found. Proceeding without audio_out configuration.")
        else:
            picked_out = None
            # Only prompt if explicitly reconfiguring; otherwise auto-pick to avoid blocking
            if force_reconfigure and sys.stdin.isatty():
                picked_out = prompt_select_device(out_list, title="output")
            else:
                picked_out = auto_pick_device(out_list)
                if picked_out:
                    print(f"Auto-selected output {picked_out['name']} -> {picked_out['alsa_device']}")
            if picked_out:
                cfg["audio_out"] = {
                    "device_name": picked_out["name"],
                    "alsa_device": picked_out["alsa_device"]
                }
                changed = True
                print(f"Selected ALSA output: {picked_out['name']} -> {picked_out['alsa_device']}")
    else:
        print(f"Using configured ALSA output: {cfg['audio_out']['device_name']} -> {cfg['audio_out']['alsa_device']}")

    if changed:
        save_config(cfg)

    return cfg


# Optional CLI flag to force reconfiguration: --reconfigure-audio
FORCE_RECONFIG = ("--reconfigure-audio" in sys.argv)

# Initialize audio configuration on startup (ALSA only)
AUDIO_CONFIG = ensure_audio_config(force_reconfigure=FORCE_RECONFIG)

# Optional: set USE_TONE=1 in the environment to send a 1 kHz test tone
USE_TONE = (os.environ.get("USE_TONE", "0") == "1")

# Audio sample rate for the whole media path (change to 48000 if needed)
SAMPLE_RATE = 24000
FRAME_SAMPLES = 480  # 20 ms at 24 kHz

class FlrigWebRemote:
    def __init__(self):
        self.client = None
        self.current_data = {
            'frequency_a': 'Unknown',
            'frequency_b': 'Unknown',
            'mode': 'Unknown',
            'power': 0,
            'swr': 0,
            'rf_gain': 0,
            'mic_gain': 0,
            'volume': 0,
            'connected': False,
            'last_update': 'Never'
        }
        self.initialize_connection()

    def initialize_connection(self):
        """Initialize connection to flrig."""
        try:
            self.client = xmlrpc.client.ServerProxy(server_url)
            # Test the connection
            self.client.rig.get_vfoA()
            self.current_data['connected'] = True
            print(f"Connected to flrig at {server_url}")
        except Exception as e:
            print(f"Error connecting to flrig: {e}")
            self.client = None
            self.current_data['connected'] = False

    def update_data(self):
        """Fetch current data from flrig."""
        if not self.client:
            self.initialize_connection()
            return

        try:
            # Get frequency data - show kHz with 2 decimals
            freq_a_hz = float(self.client.rig.get_vfoA())
            self.current_data['frequency_a'] = f"{freq_a_hz / 1e3:.2f}"

            try:
                freq_b_hz = float(self.client.rig.get_vfoB())
                self.current_data['frequency_b'] = f"{freq_b_hz / 1e3:.2f}"
            except:
                self.current_data['frequency_b'] = "N/A"

            # Get mode, power, swr
            try:
                self.current_data['mode'] = self.client.rig.get_mode()
            except:
                self.current_data['mode'] = "Unknown"

            try:
                self.current_data['power'] = int(float(self.client.rig.get_power()))
            except:
                self.current_data['power'] = 0

            try:
                self.current_data['swr'] = float(self.client.rig.get_swr())
            except:
                self.current_data['swr'] = 0.0

            try:
                self.current_data['volume'] = int(float(self.client.rig.get_volume()))
            except:
                self.current_data['volume'] = 0

            self.current_data['connected'] = True
            self.current_data['last_update'] = datetime.now().strftime("%H:%M:%S")

        except Exception as e:
            print(f"Error updating data from flrig: {e}")
            self.current_data['connected'] = False
            self.initialize_connection()

    def set_frequency(self, frequency_hz):
        """Set radio frequency."""
        if not self.client:
            return False, "Not connected to flrig"

        try:
            print(f"Attempting to set frequency to: {frequency_hz}Hz (type: {type(frequency_hz)})")
            freq_hz_float = float(frequency_hz)
            print(f"Converted to float: {freq_hz_float}")
            self.client.rig.set_vfoA(freq_hz_float)
            return True, "Frequency set successfully"
        except Exception as e:
            print(f"Error setting frequency: {e}")
            return False, str(e)

    def tune_control(self, action):
        """Control tuner."""
        if not self.client:
            return False, "Not connected to flrig"

        try:
            if action == 'start':
                self.client.rig.tune(1)
            else:
                self.client.rig.tune(0)
            return True, f"Tune {action} successful"
        except Exception as e:
            print(f"Error controlling tuner: {e}")
            return False, str(e)

    def ptt_control(self, action):
        """Control PTT."""
        if not self.client:
            return False, "Not connected to flrig"

        try:
            if action == 'on':
                self.client.rig.set_ptt(1)
            else:
                self.client.rig.set_ptt(0)
            return True, f"PTT {action} successful"
        except Exception as e:
            print(f"Error controlling PTT: {e}")
            return False, str(e)


# Global instance
flrig_remote = FlrigWebRemote()

# --- WebRTC (aiortc) state and helpers ---
pcs = set()
_pc_players = {}  # kept for compatibility; not used by custom track

# Single, long-lived asyncio loop for all aiortc work
_aiortc_loop = asyncio.new_event_loop()
def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
_loop_thread = threading.Thread(target=_run_loop, args=(_aiortc_loop,), daemon=True)
_loop_thread.start()

class FfmpegPcmTrack(MediaStreamTrack):
    """
    Capture from ALSA via ffmpeg, resample to SAMPLE_RATE mono s16, and emit exact
    20 ms (FRAME_SAMPLES) frames with correct timestamps.
    """
    kind = "audio"

    def __init__(self, alsa_device: str):
        super().__init__()
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self.samples_per_frame = FRAME_SAMPLES
        self._frame_bytes = self.samples_per_frame * self.channels * 2  # s16 mono
        self._closed = False
        self._buffer = bytearray()
        # Timestamp state
        self._time_base = Fraction(1, self.sample_rate)
        self._pts = 0

        self._cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            # Input: ALSA
            "-f", "alsa",
            "-ac", "1",
            # Let ALSA run its native rate; we will resample on output
            "-i", alsa_device,
            # Resample to SAMPLE_RATE first, then normalize to exact FRAME_SAMPLES blocks
            "-af", f"aresample={self.sample_rate}:resampler=soxr,asetnsamples=n={self.samples_per_frame}:p=0",
            # Output: raw PCM mono s16 at SAMPLE_RATE
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-"
        ]

        self._proc = subprocess.Popen(
            self._cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )

    async def recv(self) -> av.AudioFrame:
        # Fill buffer to one frame (1920 bytes)
        while len(self._buffer) < self._frame_bytes:
            if self._closed:
                return self._silence_frame()
            need = self._frame_bytes - len(self._buffer)
            chunk = await asyncio.get_event_loop().run_in_executor(None, self._read_exact, need)
            if not chunk:
                return self._silence_frame()
            self._buffer.extend(chunk)

        data = bytes(self._buffer[:self._frame_bytes])
        del self._buffer[:self._frame_bytes]

        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(data)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

    def _read_exact(self, n: int) -> bytes:
        try:
            return self._proc.stdout.read(n)
        except Exception:
            return b""

    def _silence_frame(self) -> av.AudioFrame:
        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(b"\x00" * self._frame_bytes)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

    # Synchronous stop, as aiortc calls stop() without awaiting
    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=1)
                except Exception:
                    pass
            if self._proc:
                try:
                    if self._proc.stdout:
                        self._proc.stdout.close()
                except Exception:
                    pass
                try:
                    if self._proc.stderr:
                        self._proc.stderr.close()
                except Exception:
                    pass
        finally:
            try:
                super().stop()
            except Exception:
                pass

class Tone1kTrack(MediaStreamTrack):
    """
    Generate a 1 kHz sine at 48 (or maybe 24) kHz mono, framed at exactly 20 ms (960 (or maybe 280) samples).
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.sample_rate = SAMPLE_RATE
        self.samples_per_frame = FRAME_SAMPLES
        self.phase = 0.0
        self._closed = False
        # add timestamp state to match FfmpegPcmTrack
        self._time_base = Fraction(1, self.sample_rate)
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        if self._closed:
            return self._silence_frame()

        buf = bytearray()
        freq = 1000.0
        two_pi_over_sr = 2.0 * math.pi / self.sample_rate
        for _ in range(self.samples_per_frame):
            sample = int(32767 * math.sin(self.phase))
            buf += int(sample).to_bytes(2, byteorder="little", signed=True)
            self.phase += two_pi_over_sr * freq
            if self.phase >= 2.0 * math.pi:
                self.phase -= 2.0 * math.pi

        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(bytes(buf))
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

    def _silence_frame(self) -> av.AudioFrame:
        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(b"\x00" * (self.samples_per_frame * 2))
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

class ArecordPcmTrack(MediaStreamTrack):
    """
    Capture mono PCM from ALSA using arecord at SAMPLE_RATE Hz, s16le,
    and emit exact 20 ms frames (FRAME_SAMPLES) with proper timestamps.
    """
    kind = "audio"

    def __init__(self, alsa_device: str):
        super().__init__()
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self.samples_per_frame = FRAME_SAMPLES
        self._frame_bytes = self.samples_per_frame * self.channels * 2  # s16
        self._closed = False
        self._time_base = Fraction(1, self.sample_rate)
        self._pts = 0
        self._buffer = bytearray()

        # arecord: force exact format/rate; buffer/period to avoid xruns
        self._cmd = [
            "arecord",
            "-D", alsa_device,
            "-f", "S16_LE",
            "-c", "1",
            "-r", str(self.sample_rate),
            "-t", "raw",
            "-q",
            "-B", "200000",   # 200 ms buffer
            "-F", "20000",    # 20 ms period
            "-"
        ]
        self._proc = subprocess.Popen(
            self._cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )

    async def recv(self) -> av.AudioFrame:
        while len(self._buffer) < self._frame_bytes:
            if self._closed:
                return self._silence_frame()
            chunk = await asyncio.get_event_loop().run_in_executor(
                None, self._read_exact, self._frame_bytes - len(self._buffer)
            )
            if not chunk:
                return self._silence_frame()
            self._buffer.extend(chunk)

        data = bytes(self._buffer[:self._frame_bytes])
        del self._buffer[:self._frame_bytes]

        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(data)
        frame.sample_rate = self.sample_rate
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

    def _read_exact(self, n: int) -> bytes:
        try:
            return self._proc.stdout.read(n)
        except Exception:
            return b""

    def _silence_frame(self) -> av.AudioFrame:
        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(b"\x00" * self._frame_bytes)
        frame.sample_rate = self.sample_rate
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=1)
                except Exception:
                    pass
            if self._proc and self._proc.stdout:
                try:
                    self._proc.stdout.close()
                except Exception:
                    pass
        finally:
            try:
                super().stop()
            except Exception:
                pass

# Helpers for PeerConnection lifecycle and ICE

def _alsa_input_device():
    """Return ALSA input device string like 'plughw:X,Y' or None."""
    return AUDIO_CONFIG.get("audio_in", {}).get("alsa_device")

async def _cleanup_pc(pc: RTCPeerConnection):
    try:
        for sender in pc.getSenders():
            try:
                track = sender.track
                if track and hasattr(track, "stop"):
                    res = track.stop()
                    # If some track returns an awaitable, await it; else ignore
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass
        # Backward compat: stop any old MediaPlayer if ever present
        player = _pc_players.pop(pc, None)
        if player:
            try:
                player.audio and player.audio.stop()
            except Exception:
                pass
            try:
                await player.stop()
            except Exception:
                pass
    except Exception:
        pass
    try:
        await pc.close()
    except Exception:
        pass

async def _close_all_pcs():
    to_close = list(pcs)
    for p in to_close:
        try:
            pcs.discard(p)
            await _cleanup_pc(p)
        except Exception:
            pass

async def _wait_for_ice_complete(pc: RTCPeerConnection, timeout: float = 5.0):
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()
    @pc.on("icegatheringstatechange")
    def _on_ice_state_change():
        if pc.iceGatheringState == "complete":
            try:
                done.set()
            except Exception:
                pass
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # proceed with whatever candidates we have (LAN usually ok)
        pass

async def _create_pc_with_rig_rx():
    """
    Create a PeerConnection that sends rig RX audio (from ALSA or synthetic tone) to the client.
    Only one active PC is allowed at a time to avoid ALSA 'busy' errors.
    """
    await _close_all_pcs()

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        print("WebRTC connection state:", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            pcs.discard(pc)
            await _cleanup_pc(pc)

    if USE_TONE:
        print("[webrtc] using Tone1kTrack source")
        track = Tone1kTrack()
        pc.addTrack(track)
        return pc

    alsa_in = _alsa_input_device()
    if not alsa_in:
        print("[webrtc] No ALSA input configured; cannot provide WebRTC audio.")
    else:
        print(f"[webrtc] creating ArecordPcmTrack on ALSA device: {alsa_in}")
        try:
            track = ArecordPcmTrack(alsa_in)
            pc.addTrack(track)
            print("[webrtc] ArecordPcmTrack added")
        except Exception as e:
            print(f"[webrtc] failed to start ArecordPcmTrack: {e}")
    return pc

# -------------------- Flask routes --------------------
@app.route('/')
def index():
    """Main page."""
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    """API endpoint to get current status."""
    return jsonify(flrig_remote.current_data)

def background_updater():
    """Background thread to update flrig data and emit to clients."""
    while True:
        flrig_remote.update_data()
        socketio.emit('status_update', flrig_remote.current_data)
        time.sleep(2)

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    emit('status_update', flrig_remote.current_data)

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('ptt_control')
def handle_ptt_control(data):
    success, message = flrig_remote.ptt_control(data['action'])
    emit('ptt_response', {'success': success, 'error': message if not success else None})

@socketio.on('tune_control')
def handle_tune_control(data):
    success, message = flrig_remote.tune_control(data['action'])
    emit('tune_response', {'success': success, 'error': message if not success else None})

@socketio.on('band_select')
def handle_band_select(data):
    """
    Switch to a General-class phone 'center' for the selected band:
    sets mode (LSB/USB/AM) and frequency (Hz).
    Skips 160m (1.8) and 30m (10) as requested.
    GEN goes to 830 kHz AM broadcast.
    """
    band = str(data.get('band', '')).strip()
    if not band:
        emit('band_selected', {'success': False, 'error': 'Missing band'})
        return

    # Requested mappings
    centers = {
        # "1.8":  {"freq": 1900000,  "mode": "LSB"},  # skipped per user
        "3.5":   {"freq": 3900000,   "mode": "LSB"},
        "7":     {"freq": 7237500,   "mode": "LSB"},
        # "10":  {"freq": 10136000,  "mode": "USB"},  # skipped (no phone)
        "14":    {"freq": 14300000,  "mode": "USB"},
        "18":    {"freq": 18139000,  "mode": "USB"},
        "21":    {"freq": 21362500,  "mode": "USB"},
        "24":    {"freq": 24960000,  "mode": "USB"},
        "28":    {"freq": 28400000,  "mode": "USB"},
        "50":    {"freq": 50200000,  "mode": "USB"},
        "GEN":   {"freq": 830000,    "mode": "AM"},   # 830 kHz AM broadcast
    }

    cfg = centers.get(band)
    if not cfg:
        emit('band_selected', {'success': False, 'error': f"No mapping defined for band '{band}'"})
        return

    try:
        # Set mode then tune frequency
        try:
            flrig_remote.client.rig.set_mode(cfg["mode"])
        except Exception as e:
            # Try uppercase fallback only
            try:
                flrig_remote.client.rig.set_mode(str(cfg["mode"]).upper())
            except Exception:
                emit('band_selected', {'success': False, 'error': f"set_mode failed: {e}", 'band': band})
                return

        ok, msg = flrig_remote.set_frequency(cfg["freq"])
        emit('band_selected', {
            'success': ok,
            'error': None if ok else msg,
            'band': band,
            'frequency_hz': cfg["freq"]
        })
    except Exception as e:
        emit('band_selected', {'success': False, 'error': str(e), 'band': band})

@socketio.on('user_button')
def handle_user_button(data):
    """
    Invoke flrig user 'cmd' button (integer 1..8).
    Example: 1=Ant1, 2=Ant2, 3=DNR On, 4=DNR Off, 5=5W, 6=15W, 7=30W, 8=60W.
    """
    try:
        cmd = int(data.get('cmd', 0))
    except Exception:
        emit('user_button_ack', {'success': False, 'error': 'Invalid cmd'})
        return

    if not (1 <= cmd <= 32):  # allow a broader range just in case
        emit('user_button_ack', {'success': False, 'error': f'cmd out of range: {cmd}'})
        return

    try:
        flrig_remote.client.rig.cmd(cmd)
        emit('user_button_ack', {'success': True, 'cmd': cmd})
    except Exception as e:
        emit('user_button_ack', {'success': False, 'error': str(e), 'cmd': cmd})

@socketio.on('frequency_change')
def handle_frequency_change(data):
    freq_hz = data.get('frequency')
    success, message = flrig_remote.set_frequency(freq_hz)
    emit('frequency_changed', {'success': success, 'error': message if not success else None})

# --- Add: set_mode handler (used by Mode dropdown) ---
@socketio.on('set_mode')
def handle_set_mode(data):
    try:
        mode = str(data.get('mode', '')).strip()
        if not mode:
            emit('mode_set', {'success': False, 'error': 'missing mode'})
            return
        # Try provided case, then uppercase fallback
        try:
            flrig_remote.client.rig.set_mode(mode)
        except Exception as e1:
            try:
                flrig_remote.client.rig.set_mode(mode.upper())
            except Exception as e2:
                emit('mode_set', {'success': False, 'error': f'{e1} | {e2}'})
                return
        emit('mode_set', {'success': True, 'mode': mode})
    except Exception as e:
        emit('mode_set', {'success': False, 'error': str(e)})

# --- Add: temporary debug to verify mode control path ---
@socketio.on('debug_probe_modes')
def handle_debug_probe_modes(_data=None):
    subset = ['LSB', 'USB', 'CW', 'AM', 'PKT-L', 'PKT-U']
    results = []
    try:
        # Read current mode first
        try:
            cur = flrig_remote.client.rig.get_mode()
        except Exception as e:
            cur = f'get_mode failed: {e}'
        results.append({'current': cur})

        for m in subset:
            ok = True
            err = None
            try:
                # Try set case, then uppercase fallback
                try:
                    flrig_remote.client.rig.set_mode(m)
                except Exception as e1:
                    flrig_remote.client.rig.set_mode(m.upper())
            except Exception as e2:
                ok = False
                err = str(e2)
            # Read back
            try:
                readback = flrig_remote.client.rig.get_mode()
            except Exception as e3:
                readback = f'get_mode failed: {e3}'
            results.append({'mode': m, 'set_ok': ok, 'readback': readback, 'error': err})
        emit('debug_probe_modes_result', {'success': True, 'results': results})
    except Exception as e:
        emit('debug_probe_modes_result', {'success': False, 'error': str(e)})

# ------------- Audio: WebRTC (Opus) -------------
@app.post('/api/webrtc/offer')
def api_webrtc_offer():
    """
    Signaling endpoint: accepts an SDP offer, returns an SDP answer.
    Uses a long-lived asyncio loop and waits for ICE completion.
    """
    try:
        payload = request.get_json(force=True, silent=False)
        sdp_len = len(payload.get("sdp", "")) if isinstance(payload, dict) else -1
        print(f"[webrtc] received offer, sdp bytes={sdp_len}")
        offer = RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])
    except Exception as e:
        return jsonify({"error": f"invalid offer: {e}"}), 400

    try:
        fut = asyncio.run_coroutine_threadsafe(_handle_webrtc_offer(offer), _aiortc_loop)
        result = fut.result(timeout=10)
        ans_len = len(result.get("sdp", "")) if isinstance(result, dict) else -1
        print(f"[webrtc] sending answer, sdp bytes={ans_len}")
        return jsonify(result)
    except Exception as e:
        print(f"[webrtc] offer handling failed: {e}")
        return jsonify({"error": f"webrtc failed: {e}"}), 500

async def _handle_webrtc_offer(offer: RTCSessionDescription):
    """
    Create PC, prime audio (warm-up), set remote, create answer, wait ICE.
    """
    pc = await _create_pc_with_rig_rx()
    # Warm up: pull a few frames so capture/resampler are primed before answering
    for _ in range(6):
        for sender in pc.getSenders():
            tr = sender.track
            if tr and hasattr(tr, "recv"):
                try:
                    await tr.recv()  # discard warm-up frames
                except Exception:
                    pass
        await asyncio.sleep(0)
    # Tiny settle delay helps first-connect on Safari/iOS
    await asyncio.sleep(0.03)
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await _wait_for_ice_complete(pc)
    return {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    }

@app.post('/api/webrtc/teardown')
def api_webrtc_teardown():
    """
    Explicit teardown endpoint invoked by the client on 'Disconnect'.
    Ensures ALSA is released immediately.
    """
    try:
        fut = asyncio.run_coroutine_threadsafe(_close_all_pcs(), _aiortc_loop)
        fut.result(timeout=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == '__main__':
    # Start background updater thread
    update_thread = threading.Thread(target=background_updater, daemon=True)
    update_thread.start()

    # HTTPS auto-detection (no env vars): look for certs/server/server.crt and server.key
    CERT_DEFAULT = os.path.join(SCRIPT_DIR, "certs", "server", "server.crt")
    KEY_DEFAULT  = os.path.join(SCRIPT_DIR, "certs", "server", "server.key")
    ssl_ctx = None
    if os.path.exists(CERT_DEFAULT) and os.path.exists(KEY_DEFAULT):
        ssl_ctx = (CERT_DEFAULT, KEY_DEFAULT)
        print(f"HTTPS enabled: cert={CERT_DEFAULT}, key={KEY_DEFAULT}")
    else:
        print("HTTPS disabled: cert/key not found (expected certs/server/server.crt and .key). Serving HTTP.")

    # Run the Flask-SocketIO app
    socketio.run(app, host='0.0.0.0', port=5000, debug=DEBUG_MODE, ssl_context=ssl_ctx)
