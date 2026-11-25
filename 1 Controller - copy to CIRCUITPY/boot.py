# boot.py - runs once at power-up, before code.py
# Adds a second USB serial port ("data") next to the normal REPL console.
# Keyboard / media-key HID stays enabled (CircuitPython default).
# After copying this file, unplug and replug the Pico for it to take effect.
import usb_cdc

usb_cdc.enable(console=True, data=True)
