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
import sounddevice as sd
import av
from av.audio.resampler import AudioResampler
import math
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack

# Import WinKeyer module
try:
    import winkeyer
    WINKEYER_AVAILABLE = True
except ImportError:
    WINKEYER_AVAILABLE = False
    print("Warning: winkeyer module not found. CW functionality will be disabled.")

# --- Logging / debug mode ---
DEBUG_MODE = ("--debug" in sys.argv)

# Show help if requested
if "--help" in sys.argv or "-h" in sys.argv:
    print("""
flrigWebRemote Server
=====================

A web-based remote control for amateur radio rigs via flrig, with WebRTC audio
streaming and optional WinKeyer CW support.

Usage:
    python3 flrigWebRemote-server.py [OPTIONS]

Options:
    -h, --help              Show this help message and exit
    --debug                 Enable verbose debug logging to console and debug.log
    --reconfigure-audio     Interactively select audio input/output devices
    --configure-winkeyer    Interactively select USB serial port for WinKeyer

Environment Variables:
    USE_TONE=1              Generate 1 kHz test tone instead of capturing radio audio
                            (useful for testing WebRTC without a radio connected)

Configuration:
    Settings are stored in: flrigWebRemote.config.json
    - Audio device selections (input/output indices)
    - WinKeyer serial port configuration

Examples:
    # Initial setup (first run or new hardware):
    python3 flrigWebRemote-server.py --reconfigure-audio --configure-winkeyer

    # Reconfigure only audio devices:
    python3 flrigWebRemote-server.py --reconfigure-audio

    # Reconfigure only WinKeyer port:
    python3 flrigWebRemote-server.py --configure-winkeyer

    # Run with debug logging:
    python3 flrigWebRemote-server.py --debug

    # Test WebRTC with synthetic audio:
    USE_TONE=1 python3 flrigWebRemote-server.py

Requirements:
    - flrig running and accessible at http://192.168.1.49:12345
    - Python packages: flask, flask-socketio, sounddevice, av, aiortc
    - For WinKeyer support: pyserial package and winkeyer.py module

Notes:
    - HTTPS is automatically enabled if certs/server/server.crt and server.key exist
    - Server listens on all interfaces (0.0.0.0) port 5000
    - WebRTC audio requires HTTPS for remote access (use self-signed cert for LAN)
    """)
    sys.exit(0)

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
FLRIG_HOST = "192.168.1.49"
FLRIG_PORT = 12345
server_url = f"http://{FLRIG_HOST}:{FLRIG_PORT}/RPC2"

# --- Audio configuration ---
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

def get_linux_alsa_description(card_idx):
    """
    On Linux, read /proc/asound/cards to find the friendly description
    for a specific ALSA card index.
    """
    try:
        if not os.path.exists('/proc/asound/cards'):
            return None

        with open('/proc/asound/cards', 'r') as f:
            lines = f.readlines()

        # Look for the card entry. Format is roughly:
        #  0 [PCH            ]: HDA-Intel - HDA Intel PCH
        #                       HDA Intel PCH at 0x...
        #  1 [CODEC          ]: USB-Audio - USB AUDIO  CODEC
        #                       Burr-Brown from TI USB AUDIO  CODEC at usb-...

        target_start = f" {card_idx} ["
        for i, line in enumerate(lines):
            if line.startswith(target_start):
                # The specific description is usually on the NEXT line, indented
                if i + 1 < len(lines):
                    desc = lines[i+1].strip()
                    return desc
    except Exception:
        pass
    return None

def enhance_device_name(name):
    """
    If on Linux and name contains (hw:X,Y), try to append the ALSA description.
    """
    if not sys.platform.startswith('linux'):
        return name

    # Regex to find (hw:N,...)
    match = re.search(r'\(hw:(\d+),', name)
    if match:
        card_idx = match.group(1)
        desc = get_linux_alsa_description(card_idx)
        if desc:
            # Append the friendly description
            return f"{name}  {{ {desc} }}"
    return name

def enumerate_input_devices():
    """
    Use sounddevice to list input-capable audio devices (cross-platform).
    Returns list of dicts: { 'index': int, 'name': str, 'channels': int, 'sample_rate': int }
    """
    devices = []
    try:
        device_list = sd.query_devices()
        for i, info in enumerate(device_list):
            if info['max_input_channels'] > 0:
                # Enhance name with Linux ALSA details if available
                display_name = enhance_device_name(info['name'])

                devices.append({
                    "index": i,
                    "name": display_name,
                    "channels": info['max_input_channels'],
                    "sample_rate": int(info['default_samplerate']),
                    "host_api": sd.query_hostapis(info['hostapi'])['name']
                })
    except Exception as e:
        print(f"Audio enumeration failed (sounddevice): {e}")
        return devices

    # Prefer USB devices first, then by index
    devices.sort(key=lambda d: (0 if "usb" in d["name"].lower() else 1, d["index"]))
    return devices


def enumerate_playback_devices():
    """
    Use sounddevice to list output-capable audio devices (cross-platform).
    Returns list of dicts: { 'index': int, 'name': str, 'channels': int, 'sample_rate': int }
    """
    devices = []
    try:
        device_list = sd.query_devices()
        for i, info in enumerate(device_list):
            if info['max_output_channels'] > 0:
                # Enhance name with Linux ALSA details if available
                display_name = enhance_device_name(info['name'])

                devices.append({
                    "index": i,
                    "name": display_name,
                    "channels": info['max_output_channels'],
                    "sample_rate": int(info['default_samplerate']),
                    "host_api": sd.query_hostapis(info['hostapi'])['name']
                })
    except Exception as e:
        print(f"Audio enumeration failed (sounddevice): {e}")
        return devices

    # Prefer USB devices first, then by index
    devices.sort(key=lambda d: (0 if "usb" in d["name"].lower() else 1, d["index"]))
    return devices


def prompt_select_device(devices, title="input"):
    """Interactive prompt to select device; returns selected device dict or None."""
    if not devices:
        print(f"No {title}-capable audio devices found.")
        return None

    print(f"\nAvailable {title} audio devices:")
    for i, d in enumerate(devices):
        usb_tag = " [USB]" if "usb" in d["name"].lower() else ""
        host_api = f" ({d.get('host_api', 'unknown')})" if 'host_api' in d else ""
        print(f"  {i}) {d['name']}{usb_tag}{host_api}")

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
    Validate stored input device by attempting to query it.
    """
    idx = cfg_audio_in.get("index") if cfg_audio_in else None
    if idx is None:
        return False
    try:
        # Try to query the device
        info = sd.query_devices(idx)
        # Verify it still has input channels
        return info['max_input_channels'] > 0
    except Exception:
        return False


def validate_stored_playback(cfg_audio_out):
    """
    Validate stored output device by attempting to query it.
    """
    idx = cfg_audio_out.get("index") if cfg_audio_out else None
    if idx is None:
        return False
    try:
        # Try to query the device
        info = sd.query_devices(idx)
        # Verify it still has output channels
        return info['max_output_channels'] > 0
    except Exception:
        return False


def validate_stored_winkeyer(cfg_wk):
    """
    Validate stored WinKeyer port still exists.
    """
    if not WINKEYER_AVAILABLE:
        return False

    port = cfg_wk.get("port") if cfg_wk else None
    if not port:
        return False

    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        return any(p.device == port for p in ports)
    except Exception:
        return False


def ensure_audio_config(force_reconfigure: bool = False, configure_winkeyer: bool = False):
    """
    Ensure we have capture (audio_in_hf, audio_in_vhf), playback (audio_out), and optionally WinKeyer configured.
    Config shape:
      {
        "audio_in_hf":  { "name": "...", "index": N },
        "audio_in_vhf": { "name": "...", "index": N },
        "audio_out":    { "name": "...", "index": N },
        "winkeyer":     { "port": "/dev/...", "description": "..." }
      }
    """
    cfg = load_config()
    changed = False

    # Pre-fetch lists to avoid querying multiple times
    in_list = enumerate_input_devices()
    out_list = enumerate_playback_devices()

    # ---------- Ensure HF CAPTURE device (audio_in_hf) ----------
    need_hf = (
            force_reconfigure or
            ("audio_in_hf" not in cfg) or
            ("index" not in cfg.get("audio_in_hf", {})) or
            (not validate_stored_capture(cfg.get("audio_in_hf")))
    )

    if need_hf:
        if not in_list:
            print("No input-capable audio devices found. Proceeding without HF input configuration.")
        else:
            print("\n--- HF Radio Input Configuration ---")
            picked_hf = None
            if force_reconfigure:
                picked_hf = prompt_select_device(in_list, title="HF Radio Input")
            else:
                picked_hf = auto_pick_device(in_list)
                if picked_hf:
                    print(f"Auto-selected HF input: {picked_hf['name']} (index {picked_hf['index']})")

            if picked_hf:
                cfg["audio_in_hf"] = {
                    "name": picked_hf["name"],
                    "index": picked_hf["index"]
                }
                changed = True
                print(f"Selected HF input: {picked_hf['name']} (index {picked_hf['index']})")
    else:
        print(f"Using configured HF input: {cfg['audio_in_hf']['name']} (index {cfg['audio_in_hf']['index']})")

    # ---------- Ensure VHF/UHF CAPTURE device (audio_in_vhf) ----------
    need_vhf = (
            force_reconfigure or
            ("audio_in_vhf" not in cfg) or
            ("index" not in cfg.get("audio_in_vhf", {})) or
            (not validate_stored_capture(cfg.get("audio_in_vhf")))
    )

    if need_vhf:
        if not in_list:
            print("No input-capable audio devices found. Proceeding without VHF/UHF input configuration.")
        else:
            print("\n--- VHF/UHF Radio Input Configuration ---")
            picked_vhf = None
            if force_reconfigure:
                picked_vhf = prompt_select_device(in_list, title="VHF/UHF Radio Input")
            else:
                picked_vhf = auto_pick_device(in_list)
                if picked_vhf:
                    print(f"Auto-selected VHF input: {picked_vhf['name']} (index {picked_vhf['index']})")

            if picked_vhf:
                cfg["audio_in_vhf"] = {
                    "name": picked_vhf["name"],
                    "index": picked_vhf["index"]
                }
                changed = True
                print(f"Selected VHF input: {picked_vhf['name']} (index {picked_vhf['index']})")
    else:
        print(f"Using configured VHF input: {cfg['audio_in_vhf']['name']} (index {cfg['audio_in_vhf']['index']})")

    # ---------- Ensure PLAYBACK device (audio_out) ----------
    need_out = (
            force_reconfigure or
            ("audio_out" not in cfg) or
            ("index" not in cfg.get("audio_out", {})) or
            (not validate_stored_playback(cfg.get("audio_out")))
    )

    if need_out:
        if not out_list:
            print("No output-capable audio devices found. Proceeding without audio_out configuration.")
        else:
            print("\n--- Audio Output Configuration ---")
            picked_out = None
            if force_reconfigure:
                picked_out = prompt_select_device(out_list, title="Audio Output")
            else:
                picked_out = auto_pick_device(out_list)
                if picked_out:
                    print(f"Auto-selected output: {picked_out['name']} (index {picked_out['index']})")
            if picked_out:
                cfg["audio_out"] = {
                    "name": picked_out["name"],
                    "index": picked_out["index"]
                }
                changed = True
                print(f"Selected output: {picked_out['name']} (index {picked_out['index']})")
    else:
        print(f"Using configured output: {cfg['audio_out']['name']} (index {cfg['audio_out']['index']})")

    # ---------- Ensure WINKEYER (optional) ----------
    if WINKEYER_AVAILABLE:
        need_wk = (
                configure_winkeyer or
                (configure_winkeyer and "winkeyer" in cfg and not validate_stored_winkeyer(cfg.get("winkeyer")))
        )

        if need_wk:
            # Always prompt when configure_winkeyer is True (user explicitly requested it)
            selected_port = winkeyer.prompt_select_winkeyer_port()
            if selected_port:
                # Get description for the selected port
                ports = winkeyer.enumerate_winkeyer_ports()
                desc = "USB Serial"
                for p in ports:
                    if p["port"] == selected_port:
                        desc = p["description"]
                        break

                cfg["winkeyer"] = {
                    "port": selected_port,
                    "description": desc
                }
                changed = True
                print(f"WinKeyer configured: {selected_port} ({desc})")
            else:
                # User declined WinKeyer
                if "winkeyer" in cfg:
                    del cfg["winkeyer"]
                    changed = True
                print("WinKeyer not configured (skipped by user)")
        else:
            # Check if WinKeyer is configured and valid
            if "winkeyer" in cfg:
                if validate_stored_winkeyer(cfg["winkeyer"]):
                    print(f"WinKeyer configured: {cfg['winkeyer']['port']} ({cfg['winkeyer']['description']})")
                else:
                    print(f"WinKeyer port no longer available: {cfg['winkeyer']['port']}")
                    print("  (use --configure-winkeyer to reconfigure)")
            else:
                print("WinKeyer not configured (use --configure-winkeyer to set up)")
    else:
        print("WinKeyer module not available (CW functionality disabled)")

    if changed:
        save_config(cfg)

    return cfg

# Optional CLI flag to force reconfiguration: --reconfigure-audio
FORCE_RECONFIG = ("--reconfigure-audio" in sys.argv)
CONFIGURE_WINKEYER = ("--configure-winkeyer" in sys.argv)

# Initialize audio configuration on startup
AUDIO_CONFIG = ensure_audio_config(force_reconfigure=FORCE_RECONFIG, configure_winkeyer=CONFIGURE_WINKEYER)

# Optional: set USE_TONE=1 in the environment to send a 1 kHz test tone
USE_TONE = (os.environ.get("USE_TONE", "0") == "1")

# Audio sample rate for the whole media path (change to 48000 if needed)
SAMPLE_RATE = 48000  # Changed from 24000 to match USB device
FRAME_SAMPLES = 960  # 20 ms at 48 kHz (was 480 for 24 kHz)

class FlrigWebRemote:
    def __init__(self):
        self.client = None
        self.current_data = {
            'frequency_a': 'Unknown',
            'frequency_b': 'Unknown',
            'mode': 'Unknown',
            'power': 0,
            'swr': 0,
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

# Audio Relay Instance
ht_relay = None

# --- Initialize WinKeyer if configured ---
winkeyer_instance = None
if WINKEYER_AVAILABLE and "winkeyer" in AUDIO_CONFIG:
    try:
        wk_port = AUDIO_CONFIG["winkeyer"]["port"]
        winkeyer_instance = winkeyer.WinKeyer(port=wk_port, default_wpm=20)
        winkeyer_instance.connect()
        print(f"WinKeyer ready on {wk_port}")
    except Exception as e:
        print(f"Failed to initialize WinKeyer: {e}")
        winkeyer_instance = None

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

class SoundDevicePcmTrack(MediaStreamTrack):
    """
    Capture mono PCM from audio device using sounddevice at SAMPLE_RATE Hz, s16le,
    and emit exact 20 ms frames (FRAME_SAMPLES) with proper timestamps.
    """
    kind = "audio"

    def __init__(self, device_index: int):
        super().__init__()
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self.samples_per_frame = FRAME_SAMPLES
        self._frame_bytes = self.samples_per_frame * self.channels * 2  # s16
        self._closed = False
        self._time_base = Fraction(1, self.sample_rate)
        self._pts = 0
        self._buffer = bytearray()

        # Audio queue for thread-safe communication
        self._audio_queue = asyncio.Queue()

        # Get device info
        try:
            device_info = sd.query_devices(device_index)
            native_rate = int(device_info['default_samplerate'])
            print(f"[audio] Opening device {device_index}: {device_info['name']} @ {native_rate} Hz")
        except Exception as e:
            print(f"[audio] Failed to query device {device_index}: {e}")
            self._closed = True
            raise

        # Start sounddevice input stream
        try:
            self.stream = sd.InputStream(
                device=device_index,
                channels=1,
                samplerate=native_rate,
                dtype='int16',
                blocksize=int(native_rate * 0.02),  # ~20ms blocks
                callback=self._audio_callback
            )

            # Resampler if native rate != target rate
            self._resampler = None
            if native_rate != self.sample_rate:
                print(f"[audio] Will resample from {native_rate} Hz to {self.sample_rate} Hz")
                self._resampler = av.AudioResampler(
                    format='s16',
                    layout='mono',
                    rate=self.sample_rate
                )
                self._input_rate = native_rate
            else:
                self._input_rate = self.sample_rate

            self.stream.start()
            print(f"[audio] Stream started successfully")
        except Exception as e:
            print(f"[audio] Failed to start audio stream: {e}")
            self._closed = True
            raise

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice from audio thread"""
        if status:
            print(f"[audio] status: {status}")
        if self._closed:
            return

        # indata is numpy array of int16, shape (frames, 1)
        audio_bytes = indata.tobytes()

        # Put in queue for async processing - use _aiortc_loop, not get_event_loop()
        try:
            _aiortc_loop.call_soon_threadsafe(
                self._audio_queue.put_nowait, audio_bytes
            )
        except Exception as e:
            print(f"[audio] queue error: {e}")

    async def recv(self) -> av.AudioFrame:
        """Called by aiortc to get next audio frame"""
        try:
            # Get audio from queue
            while len(self._buffer) < self._frame_bytes:
                if self._closed:
                    return self._silence_frame()

                try:
                    chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    print("[audio] recv timeout, returning silence")
                    return self._silence_frame()

                # Resample if needed
                if self._resampler:
                    # Create PyAV frame from raw bytes
                    input_samples = len(chunk) // 2  # int16 = 2 bytes per sample
                    input_frame = av.AudioFrame(format='s16', layout='mono', samples=input_samples)
                    input_frame.planes[0].update(chunk)
                    input_frame.sample_rate = self._input_rate

                    # Resample
                    resampled_frames = self._resampler.resample(input_frame)
                    for rf in resampled_frames:
                        self._buffer.extend(bytes(rf.planes[0]))
                else:
                    self._buffer.extend(chunk)

            # Extract one frame worth of data
            data = bytes(self._buffer[:self._frame_bytes])
            del self._buffer[:self._frame_bytes]

            # Create output frame
            frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
            frame.planes[0].update(data)
            frame.sample_rate = SAMPLE_RATE
            frame.time_base = self._time_base
            frame.pts = self._pts
            self._pts += self.samples_per_frame
            return frame

        except Exception as e:
            print(f"[audio] recv error: {e}")
            return self._silence_frame()

    def _silence_frame(self) -> av.AudioFrame:
        """Return a silent frame"""
        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(b"\x00" * self._frame_bytes)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = self._time_base
        frame.pts = self._pts
        self._pts += self.samples_per_frame
        return frame

    def stop(self) -> None:
        """Stop the audio stream"""
        if self._closed:
            return
        self._closed = True

        try:
            if hasattr(self, 'stream') and self.stream:
                self.stream.stop()
                self.stream.close()
                print("[audio] Stream stopped")
        except Exception as e:
            print(f"[audio] Error stopping stream: {e}")

        try:
            super().stop()
        except Exception:
            pass

class AudioRelay:
    """
    Pipes audio from a Source Device (e.g., VHF RX) to a Sink Device (e.g., HF TX input).
    Runs in a dedicated thread. Handles resampling if rates differ.
    """
    def __init__(self, input_device_idx, output_device_idx, buffer_duration=0.04):
        self.input_device_idx = input_device_idx
        self.output_device_idx = output_device_idx
        self.buffer_duration = buffer_duration
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        print(f"[relay] Started audio relay: Dev {self.input_device_idx} -> Dev {self.output_device_idx}")

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        print("[relay] Stopped audio relay")

    def _run_loop(self):
        import numpy as np

        try:
            # Query device capabilities
            in_info = sd.query_devices(self.input_device_idx)
            out_info = sd.query_devices(self.output_device_idx)

            in_rate = int(in_info['default_samplerate'])
            out_rate = int(out_info['default_samplerate'])

            # Determine block size
            block_size = int(in_rate * self.buffer_duration)

            # Setup Resampler if rates differ
            resampler = None
            if in_rate != out_rate:
                print(f"[relay] Resampling required: {in_rate} -> {out_rate}")
                resampler = AudioResampler(format='s16', layout='mono', rate=out_rate)

            with sd.InputStream(device=self.input_device_idx, channels=1, samplerate=in_rate,
                                dtype='int16', blocksize=block_size) as stream_in, \
                 sd.OutputStream(device=self.output_device_idx, channels=1, samplerate=out_rate,
                                 dtype='int16', blocksize=int(out_rate * self.buffer_duration)) as stream_out:

                print(f"[relay] Bridge active: {in_info['name']} -> {out_info['name']}")

                while self.running:
                    # Blocking read
                    data_in, overflow = stream_in.read(block_size)
                    if overflow:
                        print("[relay] Input overflow")

                    # Process/Resample
                    data_out = data_in
                    if resampler:
                        # Convert numpy int16 -> bytes -> av.AudioFrame -> resample -> bytes -> numpy int16
                        # This is a bit heavy but reuses the AV logic we already have.
                        # For a simple raw resampling, separate libraries are faster, but we stick to what we have.

                        # Actually, for raw PCM relay, we might not want the overhead of PyAV wrapping every chunk
                        # if we can avoid it. But since we imported AudioResampler, let's use it safely.
                        # Construct AV Frame
                        frame = av.AudioFrame(format='s16', layout='mono', samples=len(data_in))
                        frame.planes[0].update(data_in.tobytes())
                        frame.sample_rate = in_rate

                        resampled_frames = resampler.resample(frame)
                        out_bytes = b''.join(bytes(f.planes[0]) for f in resampled_frames)

                        # Convert back to numpy for sounddevice
                        data_out = np.frombuffer(out_bytes, dtype='int16').reshape(-1, 1)

                    # Blocking write
                    stream_out.write(data_out)

        except Exception as e:
            print(f"[relay] Bridge error: {e}")
        finally:
            self.running = False

class Tone1kTrack(MediaStreamTrack):
    """
    Generate a 1 kHz sine at SAMPLE_RATE mono, framed at exactly 20 ms (FRAME_SAMPLES samples).
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.sample_rate = SAMPLE_RATE
        self.samples_per_frame = FRAME_SAMPLES
        self.phase = 0.0
        self._closed = False
        # Timestamp state
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
            buf += sample.to_bytes(2, byteorder="little", signed=True)
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

# Helpers for PeerConnection lifecycle and ICE
def _audio_output_device():
    """Return audio output device string or None."""
    return AUDIO_CONFIG.get("audio_out", {}).get("index")

async def _pipe_inbound_to_audio_device(track: MediaStreamTrack, device_index: int):
    """
    Receive audio frames from inbound WebRTC track and write PCM to audio device playback.
    """
    import numpy as np
    stream = None
    resampler = None
    outbuf = bytearray()
    frame_bytes = FRAME_SAMPLES * 2  # s16 mono bytes in 20 ms at SAMPLE_RATE

    try:
        # Get device info
        device_info = sd.query_devices(device_index)
        native_rate = int(device_info['default_samplerate'])

        # Start sounddevice output stream
        stream = sd.OutputStream(
            device=device_index,
            channels=1,
            samplerate=native_rate,
            dtype='int16',
            blocksize=int(native_rate * 0.02)  # ~20ms blocks
        )
        stream.start()

        # Resampler to match device rate
        resampler = AudioResampler(format="s16", layout="mono", rate=native_rate)

        while True:
            frame = await track.recv()  # av.AudioFrame

            # Resample to device's native rate
            for converted in resampler.resample(frame):
                # Convert av.AudioFrame to bytes
                # Use .to_ndarray() instead of .to_bytes()
                audio_array = converted.to_ndarray()  # Returns numpy array
                data = audio_array.tobytes()  # Convert to bytes
                outbuf += data

                # Write in exact 20 ms chunks to avoid underruns/clicks
                target_bytes = int(native_rate * 0.02) * 2  # s16 = 2 bytes per sample
                while len(outbuf) >= target_bytes:
                    chunk = bytes(outbuf[:target_bytes])
                    del outbuf[:target_bytes]
                    try:
                        if stream and stream.active:
                            # Convert bytes to numpy array and write
                            audio_array = np.frombuffer(chunk, dtype='int16').reshape(-1, 1)
                            stream.write(audio_array)
                        else:
                            return
                    except Exception:
                        return
    except Exception as e:
        print(f"[webrtc uplink] pipeline error: {e}")
    finally:
        try:
            if stream and stream.active:
                stream.stop()
                stream.close()
        except Exception:
            pass

async def _create_pc_with_rig_rx():
    """
    Create a PeerConnection that sends rig RX audio (from audio device or synthetic tone) to the client.
    Only one active PC is allowed at a time to avoid device 'busy' errors.
    Also accepts inbound client mic audio and pipes it to audio playback.
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

    # Handle inbound client audio (mic)
    @pc.on("track")
    async def on_track(track):
        print(f"[webrtc uplink] inbound track kind={track.kind}")
        if track.kind == "audio":
            out_idx = _audio_output_device()
            if out_idx is not None:
                # Run piping as a background task
                asyncio.create_task(_pipe_inbound_to_audio_device(track, out_idx))
            else:
                print("[webrtc uplink] No audio output configured; dropping inbound audio")

    if USE_TONE:
        print("[webrtc] using Tone1kTrack source")
        track = Tone1kTrack()
        pc.addTrack(track)
        return pc

    audio_in_idx = _audio_input_device()
    if audio_in_idx is None:
        print("[webrtc] No HF audio input configured; cannot provide WebRTC audio.")
    else:
        device_name = AUDIO_CONFIG.get("audio_in_hf", {}).get("name", "unknown")
        print(f"[webrtc] creating SoundDevicePcmTrack on device: {device_name} (index {audio_in_idx})")
        try:
            track = SoundDevicePcmTrack(audio_in_idx)
            pc.addTrack(track)
            print("[webrtc] SoundDevicePcmTrack added")
        except Exception as e:
            print(f"[webrtc] failed to start SoundDevicePcmTrack: {e}")
    return pc

def _audio_input_device():
    """Return audio input device or None (default to HF)."""
    return AUDIO_CONFIG.get("audio_in_hf", {}).get("index")

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
    if not band or band == '--':
        emit('band_selected', {'success': False, 'error': 'Missing band'})
        return

    # Requested mappings
    centers = {
        "1.8":   {"freq": 1900000,   "mode": "LSB"},
        "3.5":   {"freq": 3900000,   "mode": "LSB"},
        "7":     {"freq": 7237500,   "mode": "LSB"},
        "10":    {"freq": 10136000,  "mode": "USB"},
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

@socketio.on('ht_relay_control')
def handle_ht_relay_control(data):
    """Enable or disable the VHF->HF Audio Relay"""
    global ht_relay
    action = data.get('action')

    vhf_in = AUDIO_CONFIG.get("audio_in_vhf", {}).get("index")
    hf_out = AUDIO_CONFIG.get("audio_out", {}).get("index")

    if action == 'start':
        if vhf_in is None or hf_out is None:
            emit('ht_relay_status', {'active': False, 'error': 'VHF Input or HF Output not configured'})
            return

        if ht_relay and ht_relay.running:
            emit('ht_relay_status', {'active': True, 'msg': 'Already running'})
            return

        ht_relay = AudioRelay(vhf_in, hf_out)
        ht_relay.start()
        emit('ht_relay_status', {'active': True})

    elif action == 'stop':
        if ht_relay:
            ht_relay.stop()
            ht_relay = None
        emit('ht_relay_status', {'active': False})

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


# --- CW Keyer Control (WinKeyer) ---
@socketio.on('send_cw')
def handle_send_cw(data):
    """
    Send CW message via WinKeyer.
    Data: { "message": "text to send", "wpm": optional speed }
    """
    if not WINKEYER_AVAILABLE:
        emit('cw_sent', {'success': False, 'error': 'WinKeyer module not available'})
        return

    if not winkeyer_instance or not winkeyer_instance.connected:
        emit('cw_sent', {'success': False, 'error': 'WinKeyer not connected'})
        return

    try:
        message = data.get('message', '').strip()
        wpm = data.get('wpm')  # optional

        if not message:
            emit('cw_sent', {'success': False, 'error': 'Empty message'})
            return

        success, estimated_time = winkeyer_instance.send_message(message, wpm=wpm)
        emit('cw_sent', {
            'success': True,
            'message': message,
            'estimated_time': estimated_time
        })

    except Exception as e:
        emit('cw_sent', {'success': False, 'error': str(e)})


@socketio.on('abort_cw')
def handle_abort_cw(_data=None):
    """Abort current CW transmission."""
    if winkeyer_instance and winkeyer_instance.connected:
        try:
            winkeyer_instance.abort()
            emit('cw_aborted', {'success': True})
        except Exception as e:
            emit('cw_aborted', {'success': False, 'error': str(e)})
    else:
        emit('cw_aborted', {'success': False, 'error': 'WinKeyer not connected'})


@socketio.on('set_cw_speed')
def handle_set_cw_speed(data):
    """Change WinKeyer default speed."""
    if not WINKEYER_AVAILABLE:
        emit('cw_speed_set', {'success': False, 'error': 'WinKeyer not available'})
        return

    if not winkeyer_instance or not winkeyer_instance.connected:
        emit('cw_speed_set', {'success': False, 'error': 'WinKeyer not connected'})
        return

    try:
        wpm = int(data.get('wpm', 18))
        winkeyer_instance.default_wpm = wpm
        winkeyer_instance.set_speed(wpm)
        emit('cw_speed_set', {'success': True, 'wpm': wpm})
        print(f"CW speed changed to {wpm} WPM")
    except Exception as e:
        emit('cw_speed_set', {'success': False, 'error': str(e)})


@app.route('/api/winkeyer/status')
def api_winkeyer_status():
    """API endpoint to check WinKeyer status."""
    if not WINKEYER_AVAILABLE:
        return jsonify({'available': False, 'reason': 'Module not loaded'})

    if winkeyer_instance and winkeyer_instance.connected:
        return jsonify({
            'available': True,
            'connected': True,
            'port': winkeyer_instance.port,
            'firmware': f"0x{winkeyer_instance.firmware_version:02x}" if winkeyer_instance.firmware_version else "unknown"
        })
    else:
        return jsonify({
            'available': True,
            'connected': False,
            'reason': 'Not configured or connection failed'
        })

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
    Ensures audio devices are released immediately.
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
