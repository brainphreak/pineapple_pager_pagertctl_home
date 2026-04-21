"""Settings persistence and constants for Wardrive."""

import json
import os

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PAYLOAD_DIR, 'settings.json')
LOOT_DIR = '/mmc/root/loot/wardrive'
DB_PATH = os.path.join(LOOT_DIR, 'wardrive.db')
CAPTURE_DIR = os.path.join(LOOT_DIR, 'captures')
EXPORT_DIR = os.path.join(LOOT_DIR, 'exports')

# Screen
SCREEN_W = 480
SCREEN_H = 222

# Fonts
FONT_TITLE = os.path.join(PAYLOAD_DIR, 'fonts', 'title.TTF')
FONT_MENU = os.path.join(PAYLOAD_DIR, 'fonts', 'menu.ttf')

# Images
BG_IMAGE = os.path.join(PAYLOAD_DIR, 'images', 'wardriving_bg.png')

# WiFi channels by band
CHANNELS_2_4 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
CHANNELS_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 149, 153, 157, 161, 165]
CHANNELS_6 = [1, 5, 9, 13, 17, 21, 25, 29, 33, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73, 77, 81, 85, 89, 93]

DEFAULTS = {
    'gps_enabled': True,
    'gps_device': '',  # Auto-detected on first run
    'gps_baud': 'auto',
    'scan_2_4ghz': True,
    'scan_5ghz': True,
    'scan_6ghz': False,
    'scan_mode': 'stealth',  # 'stealth' (passive, all bands) or 'active' (iw scan, 2.4GHz only w/o dongle)
    'hop_speed': 0.5,  # seconds per channel in stealth mode
    'capture_enabled': False,
    'scan_interface': 'wlan0',
    'capture_interface': 'wlan1mon',
    'wigle_api_name': '',
    'wigle_api_token': '',
    'scan_interval': 5,
    'geiger_sound': True,
    'brightness': 80,
    'screen_timeout': 60,  # seconds, 0 = never
    'web_server': False,
    'web_port': 1337,
    'show_classic_payloads': False,
}


def load_config():
    """Load settings from disk, with defaults for missing keys.

    If the main file is missing or corrupt, fall back to the .bak
    copy written by save_config so we never lose state to a crash.
    """
    config = dict(DEFAULTS)
    for candidate in (SETTINGS_FILE, SETTINGS_FILE + '.bak'):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, 'r') as f:
                saved = json.load(f)
            config.update(saved)
            return config
        except Exception:
            continue  # try next candidate
    return config


def save_config(config):
    """Atomically persist settings to disk with a rolling backup.

    Flow:
      1. Copy the current good file to settings.json.bak (if it exists)
      2. Write new content to settings.json.tmp, fsync, then os.replace
         over settings.json.  os.replace is atomic on POSIX so a power
         loss either leaves the old file or the new file, never a
         half-written one.
    """
    try:
        # Rolling backup of the last known-good file
        if os.path.isfile(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'rb') as src, \
                     open(SETTINGS_FILE + '.bak', 'wb') as dst:
                    dst.write(src.read())
            except Exception:
                pass

        tmp = SETTINGS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(config, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        pass


def backup_settings():
    """Create a timestamped manual backup. Returns path or None."""
    if not os.path.isfile(SETTINGS_FILE):
        return None
    from datetime import datetime
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(os.path.dirname(SETTINGS_FILE), f'settings_{stamp}.bak.json')
    try:
        with open(SETTINGS_FILE, 'rb') as src, open(dest, 'wb') as dst:
            dst.write(src.read())
        return dest
    except Exception:
        return None


def list_backups():
    """Return sorted list of manual backup files, newest first."""
    d = os.path.dirname(SETTINGS_FILE)
    try:
        files = [f for f in os.listdir(d)
                 if f.startswith('settings_') and f.endswith('.bak.json')]
    except Exception:
        return []
    return sorted(files, reverse=True)


def restore_backup(filename):
    """Restore a named backup into settings.json. Returns bool."""
    src = os.path.join(os.path.dirname(SETTINGS_FILE), filename)
    if not os.path.isfile(src):
        return False
    try:
        with open(src, 'r') as f:
            data = json.load(f)
        save_config(data)  # uses atomic path
        return True
    except Exception:
        return False


def ensure_dirs():
    """Create loot directories if they don't exist."""
    for d in [LOOT_DIR, CAPTURE_DIR, EXPORT_DIR]:
        os.makedirs(d, exist_ok=True)
