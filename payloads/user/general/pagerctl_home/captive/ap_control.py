"""ap_control.py - Bring the captive AP up/down.

Reuses the pager's pre-configured wlan0mgmt interface (the honest
management AP that wifi_utils.set_hotspot already knows how to
toggle). The captive portal just runs as a different MODE on the
same AP — we configure SSID + open/encrypted, then drive the same
hostapd path.

Marker file: because captive AP and the user's hotspot share the
same wlan0mgmt interface, there's no way to tell them apart from
the uci state alone. We drop a marker file in /tmp when we own the
AP, so on a UI restart the captive panel can recognize "this is my
AP, resume" instead of misreading it as the user's hotspot.
"""

import json
import os

from wardrive import wifi_utils


MARKER_PATH = '/tmp/pagerctl_captive_ap.flag'


def _write_marker(ssid, open_ap):
    try:
        with open(MARKER_PATH, 'w') as f:
            json.dump({'ssid': ssid, 'open': bool(open_ap)}, f)
    except Exception:
        pass


def _clear_marker():
    try:
        os.remove(MARKER_PATH)
    except Exception:
        pass


def marker_exists():
    return os.path.isfile(MARKER_PATH)


def read_marker():
    """Return {'ssid': str, 'open': bool} or None."""
    try:
        with open(MARKER_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def start_ap(ssid, open_ap=True, password=None):
    """Bring up the AP with the given SSID. open_ap=True for an open
    network, otherwise WPA2-PSK with the given password.

    Returns True on success.
    """
    if not ssid:
        return False
    if open_ap:
        wifi_utils._uci_set(f'wireless.{wifi_utils.HOTSPOT_IFACE}.ssid', ssid)
        wifi_utils._uci_set(f'wireless.{wifi_utils.HOTSPOT_IFACE}.encryption', 'none')
        wifi_utils._uci('delete', f'wireless.{wifi_utils.HOTSPOT_IFACE}.key')
        wifi_utils._uci_set(f'wireless.{wifi_utils.HOTSPOT_IFACE}.mode', 'ap')
        wifi_utils._uci_set(f'wireless.{wifi_utils.HOTSPOT_IFACE}.disabled', '0')
        wifi_utils._uci_set('wireless.wlan0wpa.disabled', '1')
        chan = wifi_utils._client_channel()
        if chan:
            wifi_utils._uci_set('wireless.radio0.channel', str(chan))
        wifi_utils._uci_commit('wireless')
        wifi_utils._wifi_reload()
        _write_marker(ssid, True)
        return True
    ok = wifi_utils.set_hotspot(True, ssid=ssid, password=password)
    if ok:
        _write_marker(ssid, False)
    return ok


def stop_ap():
    """Bring down the captive AP."""
    _clear_marker()
    return wifi_utils.set_hotspot(False)


def is_running():
    enabled, _, _ = wifi_utils.get_hotspot_state()
    return enabled
