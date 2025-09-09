from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import xmlrpc.client
import threading
import time
from datetime import datetime
# ... existing code ...
import os
import json
import sys

try:
    import sounddevice as sd  # Linux-friendly, ALSA/Pulse
except ImportError:
    sd = None
    print("Warning: python-sounddevice is not installed. Audio device selection will be skipped.")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'flrig-web-remote-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# flrig connection settings
FLRIG_HOST = "localhost"  # Changed to localhost since running on same machine
FLRIG_PORT = 12345           # Default flrig XML-RPC port
server_url = f"http://{FLRIG_HOST}:{FLRIG_PORT}/RPC2"

# --- Audio configuration (Linux-first) ---
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

def enumerate_input_devices_linux():
    """Return list of input-capable devices from sounddevice (index, name, hostapi, max_input_channels)."""
    if sd is None:
        return []
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    result = []
    for idx, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            hostapi_name = hostapis[d["hostapi"]]["name"] if d.get("hostapi") is not None else "unknown"
            result.append({
                "index": idx,
                "name": d.get("name", f"Device {idx}"),
                "hostapi": hostapi_name,
                "channels": d.get("max_input_channels", 0)
            })
    # Prefer USB devices by sorting them to the top
    result.sort(key=lambda x: (0 if "usb" in x["name"].lower() else 1, x["index"]))
    return result

def prompt_select_device(devices):
    """Interactive prompt to select device; returns selected device dict or None."""
    if not devices:
        print("No input-capable audio devices found.")
        return None

    print("\nAvailable input audio devices (Linux):")
    for i, d in enumerate(devices):
        usb_tag = " [USB]" if "usb" in d["name"].lower() else ""
        print(f"  {i}) idx={d['index']:>2}  {d['name']}  ({d['hostapi']}, ch={d['channels']}){usb_tag}")

    while True:
        sel = input("Select device number (or press Enter to pick first listed): ").strip()
        if sel == "":
            return devices[0]
        if sel.isdigit():
            n = int(sel)
            if 0 <= n < len(devices):
                return devices[n]
        print("Invalid selection. Try again.")

def auto_pick_device(devices):
    """Non-interactive fallback: first USB if available, else first input device."""
    if not devices:
        return None
    return devices[0]

def ensure_audio_config(force_reconfigure: bool = False):
    """
    Ensure we have an audio device configured.
    - Loads existing config if present and valid.
    - Otherwise, enumerates devices and prompts (if TTY), or auto-picks (if not).
    """
    cfg = load_config()
    if sd is None:
        print("sounddevice not available; skipping audio device setup.")
        return cfg

    def device_exists(dev_idx):
        try:
            d = sd.query_devices(dev_idx)
            return (d.get("max_input_channels", 0) > 0)
        except Exception:
            return False

    need_reconfigure = force_reconfigure or ("audio" not in cfg) or ("device_index" not in cfg.get("audio", {}))
    if not need_reconfigure:
        # Validate the stored device
        dev_idx = cfg["audio"]["device_index"]
        if not device_exists(dev_idx):
            print(f"Configured device index {dev_idx} no longer available. Reconfiguration needed.")
            need_reconfigure = True

    if not need_reconfigure:
        # Already good
        print(f"Using configured audio device idx={cfg['audio']['device_index']}: {cfg['audio'].get('device_name','')}")
        return cfg

    devices = enumerate_input_devices_linux()
    # If no devices found
    if not devices:
        print("No input-capable audio devices found. Proceeding without audio configuration.")
        return cfg

    # Interactive if possible
    if sys.stdin.isatty():
        picked = prompt_select_device(devices)
    else:
        picked = auto_pick_device(devices)
        print(f"Non-interactive mode: auto-selected idx={picked['index']} ({picked['name']})")

    if picked is None:
        print("No device selected. Proceeding without audio configuration.")
        return cfg

    # Save config
    cfg.setdefault("audio", {})
    cfg["audio"]["device_index"] = picked["index"]
    cfg["audio"]["device_name"] = picked["name"]
    cfg["audio"]["hostapi"] = picked["hostapi"]
    cfg["audio"]["channels"] = picked["channels"]
    save_config(cfg)

    print(f"Selected audio device idx={picked['index']}: {picked['name']} ({picked['hostapi']}, ch={picked['channels']})")
    return cfg

# Optional CLI flag to force reconfiguration: --reconfigure-audio
FORCE_RECONFIG = ("--reconfigure-audio" in sys.argv)

# Initialize audio configuration on startup
AUDIO_CONFIG = ensure_audio_config(force_reconfigure=FORCE_RECONFIG)

class FlrigWebRemote:
    def __init__(self):
        self.client = None
        self.current_data = {
            'frequency_a': 'Unknown',
            'frequency_b': 'Unknown',
            'mode': 'Unknown',
            'bandwidth': 'Unknown',
            'power': 0,
            'swr': 0,
            'rf_gain': 0,
            'mic_gain': 0,
            'volume': 0,
            'connected': False,
            'last_update': 'Never'
        }
        self.initialize_connection()
# ... existing code ...
if __name__ == '__main__':
    # Start background updater thread
    update_thread = threading.Thread(target=background_updater, daemon=True)
    update_thread.start()

    # Run the Flask-SocketIO app
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
