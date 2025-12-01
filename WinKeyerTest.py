#!/usr/bin/env python3
"""
WinKeyer USB Test Script
Tests connection and sends TEST at 20 WPM
"""

import serial
import serial.tools.list_ports
import time
import sys


def list_serial_ports():
    """List all available serial ports."""
    ports = serial.tools.list_ports.comports()
    return sorted(ports, key=lambda p: p.device)


def prompt_select_port(ports):
    """Interactive prompt to select serial port."""
    if not ports:
        print("No serial ports found.")
        return None

    print("\nAvailable serial ports:")
    for i, port in enumerate(ports):
        usb_info = ""
        if port.vid is not None:
            usb_info = f" [VID:PID={port.vid:04X}:{port.pid:04X}]"
        if "winkeyer" in port.description.lower() or "wk" in port.description.lower():
            usb_info += " <- Likely WinKeyer"
        print(f"  {i}) {port.device} - {port.description}{usb_info}")

    while True:
        sel = input("\nSelect port number (or press Enter for first): ").strip()
        if sel == "":
            return ports[0]
        if sel.isdigit():
            n = int(sel)
            if 0 <= n < len(ports):
                return ports[n]
        print("Invalid selection. Try again.")


def initialize_winkeyer(ser):
    """
    Initialize WinKeyer with basic settings.
    Based on K1EL WinKeyer protocol [[1]](https://www.k1elsystems.com/files/WK3_Datasheet_v1.3.pdf).
    """
    # Admin command 0x00: Open/Reset WinKeyer
    ser.write(b'\x00\x02')  # 0x00 = admin command, 0x02 = host open
    time.sleep(0.3)

    # Read echo of firmware version
    response = ser.read(ser.in_waiting)
    if response:
        print(f"WinKeyer firmware response: {response.hex()}")

    # Set WPM speed command (0x02)
    # Speed in WPM (5-99), we want 20 WPM
    ser.write(b'\x02\x14')  # 0x02 = speed command, 0x14 = 20 WPM
    time.sleep(0.05)


def send_cw_message(ser, message):
    """
    Send CW message to WinKeyer.
    Message is sent as ASCII text.
    """
    # Buffered speed/command message (0x1B for speed change)
    # For now, just send the message directly
    message_bytes = message.encode('ascii')
    ser.write(message_bytes)
    print(f"Sent message: {message}")

    # Wait for buffer to empty (estimate based on message length and speed)
    # At 20 WPM with ~50 character/minute = ~2.5 char/sec
    # "TEST" = 4 chars, roughly 1.5 seconds + margin
    wait_time = len(message) * 0.5 + 1.0
    time.sleep(wait_time)


def close_winkeyer(ser):
    """Close WinKeyer session."""
    # Admin command: Close
    ser.write(b'\x00\x03')  # 0x00 = admin command, 0x03 = host close
    time.sleep(0.1)


def main():
    print("WinKeyer USB Test Script")
    print("=" * 50)

    # List and select port
    ports = list_serial_ports()
    if not ports:
        print("Error: No serial ports found!")
        sys.exit(1)

    selected_port = prompt_select_port(ports)
    if not selected_port:
        print("No port selected.")
        sys.exit(1)

    print(f"\nConnecting to {selected_port.device}...")

    try:
        # Open serial port (1200 baud, 8N2 is WinKeyer default)
        ser = serial.Serial(
            port=selected_port.device,
            baudrate=1200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=1.0
        )

        print(f"Connected to {selected_port.device}")

        # Initialize WinKeyer
        print("Initializing WinKeyer...")
        initialize_winkeyer(ser)

        # Send TEST
        print("\nSending TEST at 20 WPM...")
        send_cw_message(ser, "TEST")

        print("\nCW transmission complete!")

        # Close WinKeyer
        print("Closing WinKeyer...")
        close_winkeyer(ser)

        ser.close()
        print("Done!")

    except serial.SerialException as e:
        print(f"Error opening serial port: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
