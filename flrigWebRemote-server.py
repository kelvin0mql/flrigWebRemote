from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
import xmlrpc.client
import threading
import time
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'flrig-web-remote-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# flrig connection settings
FLRIG_HOST = "192.168.1.29"  # Your flrig host
FLRIG_PORT = 12345           # Default flrig XML-RPC port
server_url = f"http://{FLRIG_HOST}:{FLRIG_PORT}/RPC2"

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
            # Get frequency data
            freq_a_hz = float(self.client.rig.get_vfoA())
            self.current_data['frequency_a'] = f"{freq_a_hz / 1e6:.3f}"

            try:
                freq_b_hz = float(self.client.rig.get_vfoB())
                self.current_data['frequency_b'] = f"{freq_b_hz / 1e6:.3f}"
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

            # Get control levels (these might not be available on all rigs)
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

# Global instance
flrig_remote = FlrigWebRemote()

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
    # Send current data immediately when client connects
    socketio.emit('status_update', flrig_remote.current_data)

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

if __name__ == '__main__':
    # Start background updater thread
    update_thread = threading.Thread(target=background_updater, daemon=True)
    update_thread.start()

    # Run the Flask-SocketIO app
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
