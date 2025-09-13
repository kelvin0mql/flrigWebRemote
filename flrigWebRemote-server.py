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

# WebRTC / aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer  # ALSA capture/playback via ffmpeg

app = Flask(__name__)
app.config['SECRET_KEY'] = 'flrig-web-remote-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# flrig connection settings
FLRIG_HOST = "localhost"  # Changed to localhost since running on same machine
FLRIG_PORT = 12345        # Default flrig XML-RPC port
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
            if sys.stdin.isatty():
                picked_in = prompt_select_device(in_list, title="input")
            else:
                picked_in = auto_pick_device(in_list)
                if picked_in:
                    print(f"Non-interactive mode: auto-selected input {picked_in['name']} -> {picked_in['alsa_device']}")
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
            if sys.stdin.isatty():
                picked_out = prompt_select_device(out_list, title="output")
            else:
                picked_out = auto_pick_device(out_list)
                if picked_out:
                    print(f"Non-interactive mode: auto-selected output {picked_out['name']} -> {picked_out['alsa_device']}")
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

            # Get mode and bandwidth
            try:
                self.current_data['mode'] = self.client.rig.get_mode()
            except:
                self.current_data['mode'] = "Unknown"

            try:
                self.current_data['bandwidth'] = str(self.client.rig.get_bw())
            except:
                self.current_data['bandwidth'] = "Unknown"

            # Get power and SWR
            try:
                self.current_data['power'] = int(float(self.client.rig.get_power()))
            except:
                self.current_data['power'] = 0

            try:
                self.current_data['swr'] = float(self.client.rig.get_swr())
            except:
                self.current_data['swr'] = 0.0

            # Get control levels (may not be available on all rigs)
            try:
                self.current_data['rf_gain'] = int(float(self.client.rig.get_rf_gain()))
            except:
                self.current_data['rf_gain'] = 0

            try:
                self.current_data['mic_gain'] = int(float(self.client.rig.get_mic_gain()))
            except:
                self.current_data['mic_gain'] = 0

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


def _alsa_input_device():
    """Return ALSA input device string like 'plughw:X,Y' or None."""
    return AUDIO_CONFIG.get("audio_in", {}).get("alsa_device")


async def _create_pc_with_rig_rx():
    """
    Create a PeerConnection that sends rig RX audio (from ALSA) to the client.
    """
    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    def on_connectionstatechange():
        print("WebRTC connection state:", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            try:
                pcs.discard(pc)
                asyncio.ensure_future(_cleanup_pc(pc))
            except Exception:
                pass

    # Add rig RX track (ALSA capture via ffmpeg/MediaPlayer)
    alsa_in = _alsa_input_device()
    if not alsa_in:
        print("No ALSA input configured; cannot provide WebRTC audio.")
    else:
        # Capture mono 16kHz for conversational quality/latency
        player = MediaPlayer(
            alsa_in,
            format="alsa",
            options={
                "ac": "1",
                "ar": "16000",
                # Optional filtering similar to your ffmpeg chain:
                # "af": "highpass=f=300,lowpass=f=3000"
            },
        )
        if player.audio:
            pc.addTrack(player.audio)
        else:
            print("Failed to create audio track from ALSA input.")

    return pc


async def _cleanup_pc(pc: RTCPeerConnection):
    try:
        await pc.close()
    except Exception:
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
        time.sleep(2)  # Update every 2 seconds


@socketio.on('connect')
def handle_connect():
    print('Client connected')
    emit('status_update', flrig_remote.current_data)


@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')


@socketio.on('ptt_control')
def handle_ptt_control(data):
    """Handle PTT control requests."""
    success, message = flrig_remote.ptt_control(data['action'])
    emit('ptt_response', {'success': success, 'error': message if not success else None})


@socketio.on('tune_control')
def handle_tune_control(data):
    """Handle Tune control requests."""
    success, message = flrig_remote.tune_control(data['action'])
    emit('tune_response', {'success': success, 'error': message if not success else None})


@socketio.on('frequency_change')
def handle_frequency_change(data):
    """Handle frequency change requests."""
    freq_hz = data.get('frequency')
    success, message = flrig_remote.set_frequency(freq_hz)
    emit('frequency_changed', {'success': success, 'error': message if not success else None})


# ------------- Audio: replaced HTTP streaming with WebRTC (Opus) -------------

@app.post('/api/webrtc/offer')
def api_webrtc_offer():
    """
    Signaling endpoint: accepts an SDP offer, returns an SDP answer.
    Publishes the rig RX ALSA capture as an Opus track to the browser.
    """
    try:
        payload = request.get_json(force=True, silent=False)
        offer = RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])
    except Exception as e:
        return jsonify({"error": f"invalid offer: {e}"}), 400

    async def handle():
        pc = await _create_pc_with_rig_rx()
        await pc.setRemoteDescription(offer)
        # Create answer; Opus will be negotiated automatically by the browser and aiortc
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        }

    # Run the coroutine in a dedicated event loop for this request
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(handle())
        loop.run_until_complete(asyncio.sleep(0))  # yield once
        loop.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"webrtc failed: {e}"}), 500


# NOTE: Legacy HTTP audio endpoints (/audio.wav, /audio, /audio.mp3) have been removed.


if __name__ == '__main__':
    # Start background updater thread
    update_thread = threading.Thread(target=background_updater, daemon=True)
    update_thread.start()

    # Run the Flask-SocketIO app
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
