#!/usr/bin/env python3
"""
WinKeyer USB Interface Module
Handles communication with K1EL WinKeyer devices for CW keying.
"""

import serial
import serial.tools.list_ports
import time
import logging

logger = logging.getLogger(__name__)


class WinKeyer:
    """
    WinKeyer USB interface class.
    Based on K1EL WinKeyer protocol specification.
    """

    def __init__(self, port=None, default_wpm=18):
        """
        Initialize WinKeyer interface.

        Args:
            port: Serial port device path (e.g., '/dev/cu.usbserial-141140')
            default_wpm: Default sending speed in WPM (13-25)
        """
        self.port = port
        self.default_wpm = max(13, min(25, default_wpm))
        self.ser = None
        self.connected = False
        self.firmware_version = None

    def connect(self):
        """
        Open connection to WinKeyer.

        Returns:
            tuple: (success: bool, message: str)
        """
        if not self.port:
            return False, "No port configured"

        try:
            # WinKeyer uses 1200 baud, 8N2
            self.ser = serial.Serial(
                port=self.port,
                baudrate=1200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=1.0
            )

            logger.info(f"Opened WinKeyer on {self.port}")

            # Initialize WinKeyer
            self._initialize()

            self.connected = True

            # NOW set speed after connected flag is True
            self.set_speed(self.default_wpm)
            logger.info(f"WinKeyer initialized to {self.default_wpm} WPM")

            return True, f"Connected to WinKeyer on {self.port}"

        except serial.SerialException as e:
            logger.error(f"Failed to open WinKeyer port {self.port}: {e}")
            return False, str(e)
        except Exception as e:
            logger.error(f"WinKeyer connection error: {e}")
            return False, str(e)

    def _initialize(self):
        """
        Initialize WinKeyer with host open command and default settings.
        Based on K1EL protocol: Admin command 0x00, parameter 0x02 = host open.
        """
        # Admin command: Host Open
        self.ser.write(b'\x00\x02')
        time.sleep(0.3)

        # Read firmware version response
        response = self.ser.read(self.ser.in_waiting)
        if response and len(response) > 0:
            self.firmware_version = response[0]
            logger.info(f"WinKeyer firmware version: 0x{self.firmware_version:02x}")
        else:
            logger.warning("No firmware version response from WinKeyer")
            self.firmware_version = 0

    def set_speed(self, wpm):
        """
        Set WinKeyer transmission speed.

        Args:
            wpm: Words per minute (13-25)
        """
        if not (13 <= wpm <= 25):
            logger.warning(f"WPM {wpm} out of range (13-25), clamping")
            wpm = max(13, min(25, wpm))

        # Speed command: 0x02 followed by WPM value
        self.ser.write(bytes([0x02, wpm]))
        time.sleep(0.05)
        logger.info(f"Set WinKeyer speed to {wpm} WPM")

    def send_message(self, message, wpm=None):
        """
        Send CW message via WinKeyer.

        Args:
            message: Text message to send (ASCII)
            wpm: Optional speed override (uses default if None)

        Returns:
            tuple: (success: bool, estimated_time: float)
        """
        if not self.connected or not self.ser:
            return False, 0.0

        try:
            # Set speed if different from default
            if wpm is not None and wpm != self.default_wpm:
                self.set_speed(wpm)

            # Send message as ASCII bytes
            message_bytes = message.encode('ascii', errors='ignore')
            self.ser.write(message_bytes)

            logger.info(f"Sent CW message: {message} ({wpm or self.default_wpm} WPM)")

            # Estimate transmission time
            # PARIS standard: 5 characters = 1 word
            chars_per_sec = (wpm or self.default_wpm) * 5 / 60.0
            estimated_time = len(message) / chars_per_sec + 0.5  # +0.5s margin

            return True, estimated_time

        except Exception as e:
            logger.error(f"Error sending CW message: {e}")
            return False, 0.0

    def abort(self):
        """Abort current transmission (clear buffer)."""
        if not self.connected or not self.ser:
            return

        try:
            # Clear buffer command: 0x0A
            self.ser.write(b'\x0a')
            logger.info("Aborted CW transmission")
        except Exception as e:
            logger.error(f"Error aborting CW: {e}")

    def disconnect(self):
        """Close WinKeyer connection gracefully."""
        if not self.ser:
            return

        try:
            # Admin command: Host Close (0x00, 0x03)
            self.ser.write(b'\x00\x03')
            time.sleep(0.1)
        except:
            pass

        try:
            self.ser.close()
            logger.info("WinKeyer disconnected")
        except:
            pass

        self.ser = None
        self.connected = False

    def __del__(self):
        """Destructor: ensure clean shutdown."""
        self.disconnect()


def enumerate_winkeyer_ports():
    """
    List all serial ports that might be WinKeyer devices.

    Returns:
        List of dicts with port info
    """
    ports = []
    try:
        port_list = serial.tools.list_ports.comports()
        for p in sorted(port_list, key=lambda p: p.device):
            # Filter out generic 'n/a' ports that aren't actually useful (like /dev/ttyS* on Linux)
            if p.description == 'n/a' or not p.description:
                # If it's a standard /dev/ttyS* on Linux and it's 'n/a', definitely skip it
                if p.device.startswith('/dev/ttyS'):
                    continue
            
            info = {
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid if hasattr(p, 'hwid') else '',
                "likely_winkeyer": "winkeyer" in p.description.lower() or
                                   "wk" in p.description.lower() or
                                   "usb serial" in p.description.lower()
            }
            ports.append(info)

    except Exception as e:
        logger.error(f"Failed to enumerate serial ports: {e}")

    return ports


def prompt_select_winkeyer_port():
    """
    Interactive prompt to select WinKeyer port.

    Returns:
        Port device path or None if user declines
    """
    ports = enumerate_winkeyer_ports()

    if not ports:
        print("No serial ports found.")
        return None

    print("\nAvailable serial ports:")
    for i, p in enumerate(ports):
        marker = " <- Likely WinKeyer" if p["likely_winkeyer"] else ""
        print(f"  {i}) {p['port']} - {p['description']}{marker}")

    print("\nSelect a port for WinKeyer, or type 'n' to skip WinKeyer setup.")

    while True:
        sel = input("Port number (or 'n' to skip): ").strip().lower()

        if sel in ('n', 'no', 'none', 'skip'):
            print("Skipping WinKeyer configuration.")
            return None

        if sel == "":
            # Default to first likely WinKeyer, or first port
            for p in ports:
                if p["likely_winkeyer"]:
                    return p["port"]
            return ports[0]["port"] if ports else None

        if sel.isdigit():
            n = int(sel)
            if 0 <= n < len(ports):
                return ports[n]["port"]

        print("Invalid selection. Try again or type 'n' to skip.")


def validate_port(port):
    """
    Check if a serial port device exists.

    Args:
        port: Device path string

    Returns:
        bool: True if port exists
    """
    if not port:
        return False
    try:
        ports = serial.tools.list_ports.comports()
        return any(p.device == port for p in ports)
    except Exception:
        return False
