#!/usr/bin/env python3
"""
BandHoppingBeacon.py - Band-Hopping CW Beacon Application

Cycles through 11 amateur radio bands, transmitting a CW beacon on each.
Uses flrig for rig control and WinKeyer for CW transmission.

Author: N0MQL
"""

import xmlrpc.client
import time
import random
import sys
import os
import json
from datetime import datetime

# Import WinKeyer module
try:
    import winkeyer
    WINKEYER_AVAILABLE = True
except ImportError:
    WINKEYER_AVAILABLE = False
    print("ERROR: winkeyer module not found. CW functionality is required.")
    sys.exit(1)


# Configuration
FLRIG_HOST = "192.168.1.29"
FLRIG_PORT = 12345
BEACON_MESSAGE = "BCN N0MQL N0MQL"
CW_SPEED_WPM = 20
INTER_BAND_DELAY = 60  # seconds between bands

# Band definitions: name, frequency range or discrete frequencies, antenna
BANDS = [
    {"name": "160m", "range": (1.800, 1.830), "antenna": 2},
    {"name": "80m", "range": (3.525, 3.600), "antenna": 1},
    {"name": "60m", "freqs": [5.332, 5.348, 5.3585, 5.373, 5.405], "antenna": 2},
    {"name": "40m", "range": (7.025, 7.074), "antenna": 1},
    {"name": "30m", "range": (10.100, 10.150), "antenna": 1},
    {"name": "20m", "range": (14.025, 14.074), "antenna": 1},
    {"name": "17m", "range": (18.068, 18.110), "antenna": 1},
    {"name": "15m", "range": (21.025, 21.200), "antenna": 1},
    {"name": "12m", "range": (24.890, 24.930), "antenna": 1},
    {"name": "10m", "range": (28.000, 28.300), "antenna": 1},
    {"name": "6m", "range": (50.000, 50.500), "antenna": 1},
]


class BandHoppingBeacon:
    def __init__(self):
        self.flrig = None
        self.wk = None
        self.config = self.load_config()

    def load_config(self):
        """Load configuration from flrigWebRemote.config.json"""
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "flrigWebRemote.config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: failed to read config: {e}")
        return {}

    def connect_flrig(self):
        """Establish connection to flrig via XML-RPC"""
        server_url = f"http://{FLRIG_HOST}:{FLRIG_PORT}/RPC2"
        try:
            self.flrig = xmlrpc.client.ServerProxy(server_url)
            # Test connection
            version = self.flrig.main.get_version()
            print(f"✓ Connected to flrig (version {version})")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to flrig at {server_url}: {e}")
            return False

    def connect_winkeyer(self):
        """Initialize WinKeyer connection"""
        if not WINKEYER_AVAILABLE:
            print("✗ WinKeyer module not available")
            return False

        # Get WinKeyer port from config
        wk_config = self.config.get("winkeyer", {})
        wk_port = wk_config.get("port")

        if not wk_port:
            print("✗ WinKeyer port not configured. Run: python3 flrigWebRemote-server.py --configure-winkeyer")
            return False

        try:
            # Create WinKeyer instance with the configured port
            self.wk = winkeyer.WinKeyer(port=wk_port, default_wpm=CW_SPEED_WPM)

            # Connect to the device
            success, message = self.wk.connect()

            if not success:
                print(f"✗ Failed to connect to WinKeyer: {message}")
                return False

            print(f"✓ {message}")
            print(f"✓ WinKeyer speed set to {CW_SPEED_WPM} WPM")
            return True

        except Exception as e:
            print(f"✗ Failed to initialize WinKeyer: {e}")
            return False

    def select_frequency(self, band):
        """Select a frequency for the given band"""
        if "freqs" in band:
            # 60m: pick one discrete frequency at random
            freq_mhz = random.choice(band["freqs"])
        else:
            # Normal band: pick random frequency avoiding edges by 200 Hz
            low, high = band["range"]
            edge_offset = 0.0002  # 200 Hz in MHz
            low += edge_offset
            high -= edge_offset
            freq_mhz = random.uniform(low, high)

        return freq_mhz

    def set_frequency(self, freq_mhz):
        """Set rig frequency via flrig"""
        try:
            # flrig rig.set_frequency expects a double (float) in Hz
            freq_hz = freq_mhz * 1_000_000.0
            self.flrig.rig.set_frequency(freq_hz)
            time.sleep(0.1)  # Small delay for rig to process
            return True
        except Exception as e:
            print(f"  ✗ Failed to set frequency: {e}")
            return False

    def set_antenna(self, antenna_num):
        """Set antenna via flrig user command buttons"""
        try:
            # Use rig.cmd() to trigger user-defined buttons
            # cmd 1 = Ant1, cmd 2 = Ant2 (as configured in the web interface)
            cmd = antenna_num  # antenna_num is already 1 or 2
            self.flrig.rig.cmd(cmd)
            time.sleep(0.1)  # Small delay for rig to process
            return True
        except Exception as e:
            # Some rigs might not support cmd(), log but don't fail
            print(f"  ⚠ Antenna switching command failed: {e}")
            return True  # Don't fail the beacon cycle for this

    def send_beacon(self):
        """Send CW beacon message via WinKeyer"""
        try:
            success, estimated_time = self.wk.send_message(BEACON_MESSAGE)
            if success:
                print(f"  ✓ Beacon transmission started (est. {estimated_time:.1f}s)")
                # Wait for transmission to complete, but allow interruption
                try:
                    time.sleep(estimated_time)
                except KeyboardInterrupt:
                    # Abort the transmission immediately
                    print(f"\n  ⚠ Transmission interrupted, aborting...")
                    self.wk.abort()
                    raise  # Re-raise to propagate to main loop
                return True
            else:
                print(f"  ✗ Failed to send CW")
                return False
        except KeyboardInterrupt:
            raise  # Re-raise to propagate
        except Exception as e:
            print(f"  ✗ Failed to send CW: {e}")
            return False

    def process_band(self, band):
        """Process one band: set freq, antenna, send beacon"""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing {band['name']}...")

        # Select frequency
        freq_mhz = self.select_frequency(band)
        print(f"  Frequency: {freq_mhz:.4f} MHz")

        # Set frequency
        if not self.set_frequency(freq_mhz):
            return False

        # Set antenna
        ant_name = f"Ant{band['antenna']}"
        print(f"  Antenna: {ant_name}")
        if not self.set_antenna(band["antenna"]):
            return False

        # Small delay to let rig settle
        time.sleep(0.5)

        # Send beacon
        print(f"  Sending: {BEACON_MESSAGE}")
        if not self.send_beacon():
            return False

        print(f"  ✓ Beacon sent on {band['name']}")
        return True

    def run(self):
        """Main beacon loop"""
        print("\n" + "="*60)
        print("Band-Hopping CW Beacon - N0MQL")
        print("="*60)

        # Connect to flrig
        if not self.connect_flrig():
            print("\nFailed to connect to flrig. Exiting.")
            return 1

        # Connect to WinKeyer
        if not self.connect_winkeyer():
            print("\nFailed to connect to WinKeyer. Exiting.")
            return 1

        print(f"\nBeacon message: {BEACON_MESSAGE}")
        print(f"Inter-band delay: {INTER_BAND_DELAY} seconds")
        print(f"Cycle time: {len(BANDS)} bands × {INTER_BAND_DELAY}s = {len(BANDS) * INTER_BAND_DELAY // 60} minutes")
        print("\nStarting beacon cycle... (Press Ctrl+C to stop)\n")

        cycle = 0
        try:
            while True:
                cycle += 1
                print(f"\n{'='*60}")
                print(f"Starting Cycle #{cycle}")
                print(f"{'='*60}")

                for band in BANDS:
                    self.process_band(band)

                    # Wait before next band (except after last band, wait anyway for cycle)
                    print(f"  Waiting {INTER_BAND_DELAY} seconds...")
                    time.sleep(INTER_BAND_DELAY)

                print(f"\n✓ Cycle #{cycle} complete")

        except KeyboardInterrupt:
            print("\n\nBeacon stopped by user.")
            return 0
        except Exception as e:
            print(f"\n\nUnexpected error: {e}")
            import traceback
            traceback.print_exc()
            return 1
        finally:
            # Cleanup
            if self.wk:
                try:
                    self.wk.disconnect()
                    print("✓ WinKeyer connection closed")
                except:
                    pass


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("\nUsage:")
        print("  python3 BandHoppingBeacon.py")
        print("\nRequirements:")
        print("  - flrig running at http://192.168.1.29:12345")
        print("  - WinKeyer configured in flrigWebRemote.config.json")
        print("  - winkeyer.py module installed")
        print("\nConfiguration:")
        print("  Run this first if WinKeyer not configured:")
        print("    python3 flrigWebRemote-server.py --configure-winkeyer")
        return 0

    beacon = BandHoppingBeacon()
    return beacon.run()


if __name__ == "__main__":
    sys.exit(main())