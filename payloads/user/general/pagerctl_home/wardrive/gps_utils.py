"""gps_utils.py - GPS device detection shared between wardrive_ui
and settings_ui.

Keeps the USB device picker + friendly name lookup logic in one place
so the settings menu and the wardrive menu stay in sync.
"""

import glob
import os
import subprocess


_EXCLUDE_KEYWORDS = [
    'uart', 'jtag', 'spi', 'i2c', 'debug',
    'ehci', 'xhci', 'ohci', 'hub',
    'wireless_device', 'csr8510', 'bluetooth',
]


def detect_gps_devices():
    """List serial devices that might be GPS receivers.

    Walks /dev/ttyACM* and /dev/ttyUSB* and filters out known
    internal pager peripherals (UART bridges, BT dongles, etc.)
    by checking the USB product string.
    """
    devices = []
    for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
        for dev in sorted(glob.glob(pattern)):
            product = get_device_product(dev).lower()
            if product and any(kw in product for kw in _EXCLUDE_KEYWORDS):
                continue
            devices.append(dev)
    return devices


def get_device_product(dev_path):
    """Return the USB product string for a tty device, or '' if unknown."""
    dev_name = os.path.basename(dev_path)
    try:
        d = os.path.realpath(f'/sys/class/tty/{dev_name}/device')
        for _ in range(5):
            d = os.path.dirname(d)
            pf = os.path.join(d, 'product')
            if os.path.isfile(pf):
                with open(pf) as f:
                    return f.read().strip()
    except Exception:
        pass
    return ''


def get_device_name(dev_path):
    """Return 'Product (ttyACMx)' for menu display."""
    product = get_device_product(dev_path)
    short = os.path.basename(dev_path)
    return f'{product} ({short})' if product else short


def get_gpsd_baud(device=None):
    """Return the baud rate currently in use on the GPS serial device,
    as a string (e.g. '9600'). Returns '' if it can't be determined.

    Tries `stty -F /dev/ttyXXX speed` first (works without gpsd).
    """
    if not device:
        return ''
    if not os.path.exists(device):
        return ''
    try:
        r = subprocess.run(['stty', '-F', device, 'speed'],
                            capture_output=True, text=True, timeout=2)
        out = r.stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return ''


def short_device_label(dev_path):
    """Compact label for the settings value column — trims common
    trailing strings like 'Receiver' / 'GPS/GNSS'."""
    if not dev_path or not os.path.exists(dev_path):
        return 'Not set'
    product = get_device_product(dev_path)
    short = os.path.basename(dev_path)
    if not product:
        return short
    for strip in (' Receiver', ' receiver', ' Module', ' module',
                  ' - GPS/GNSS', ' GPS/GNSS', '/GNSS', ' - GPS'):
        product = product.replace(strip, '')
    if len(product) > 14:
        product = product[:14].rstrip()
    return f'{product} ({short})'
