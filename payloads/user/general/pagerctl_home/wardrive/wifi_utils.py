"""wifi_utils.py - WiFi scan / connect / hotspot helpers.

The pager comes with pre-configured uci wireless entries:
    wireless.wlan0cli   = STA (client)   on phy0 (2.4 GHz)
    wireless.wlan0mon   = monitor        on phy0
    wireless.wlan0wpa   = WPA2 AP        on phy0 (disabled by default)
    wireless.wlan0open  = open AP        on phy0 (disabled by default)

We reuse those rather than creating new ones so we don't fight the
firmware's preset layout. Switching 'client connection' is a matter of
updating wireless.wlan0cli's ssid + key; toggling the hotspot is a
matter of enabling/disabling wireless.wlan0wpa and setting its ssid/key.
"""

import os
import subprocess


CLIENT_IFACE = 'wlan0cli'         # connects as a client to external APs
# The pager has multiple AP interfaces: wlan0wpa is the PineAP/Karma
# capture interface (heavily modified hostapd that intercepts probes
# and drops normal client associations), and wlan0mgmt is the
# "management AP" with a clean honest hostapd path. Real phones can
# only connect to wlan0mgmt — wlan0wpa is for handshake harvesting.
HOTSPOT_IFACE = 'wlan0mgmt'
SCAN_IFACE = 'wlan0cli'           # we scan from the managed client iface


def _uci(*args):
    """Run uci with the given args, return stdout or ''. Silent on error."""
    try:
        r = subprocess.run(['uci'] + list(args),
                            capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception:
        return ''


def _uci_set(key, value):
    """Set a uci key to a string value. Don't add shell quoting — subprocess
    passes args literally, so wrapping in extra quotes would store the
    quotes themselves as part of the value."""
    _uci('set', f'{key}={value}')


def _uci_commit(package='wireless'):
    _uci('commit', package)


def _wifi_reload():
    try:
        subprocess.run(['wifi', 'reload'], capture_output=True, timeout=15)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Client (STA) mode
# ----------------------------------------------------------------------

def get_client_ssid():
    """Return the SSID the pager is currently configured to join, or ''."""
    raw = _uci('get', f'wireless.{CLIENT_IFACE}.ssid').strip()
    return raw.strip("'")


def get_client_status():
    """Try `iw dev wlanXcli link` to return the SSID we're actually
    associated with (or '' if not associated)."""
    try:
        r = subprocess.run(['iw', 'dev', CLIENT_IFACE, 'link'],
                            capture_output=True, text=True, timeout=3)
        for line in r.stdout.split('\n'):
            line = line.strip()
            if line.lower().startswith('ssid:'):
                return line.split(None, 1)[1].strip()
    except Exception:
        pass
    return ''


def connect_network(ssid, password, encryption='psk2'):
    """Configure wlan0cli for the given network and reload WiFi.

    encryption: 'psk2' (WPA2), 'psk' (WPA), or 'none'.
    Returns True if uci commands succeeded (doesn't verify connection).
    """
    if not ssid:
        return False
    _uci_set(f'wireless.{CLIENT_IFACE}.ssid', ssid)
    if password:
        _uci_set(f'wireless.{CLIENT_IFACE}.encryption', encryption)
        _uci_set(f'wireless.{CLIENT_IFACE}.key', password)
    else:
        _uci_set(f'wireless.{CLIENT_IFACE}.encryption', 'none')
        _uci('delete', f'wireless.{CLIENT_IFACE}.key')
    _uci_set(f'wireless.{CLIENT_IFACE}.disabled', '0')
    _uci_commit('wireless')
    _wifi_reload()
    return True


# ----------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------

def scan_networks():
    """Return a list of dicts: [{'ssid': 'x', 'signal': -60, 'enc': 'wpa2'}, ...]
    sorted by signal strength (strongest first). Hidden SSIDs are skipped.
    Runs `iw dev <iface> scan` which requires the client interface to
    be up in managed mode (it normally is on this platform)."""
    results = []
    try:
        r = subprocess.run(['iw', 'dev', SCAN_IFACE, 'scan'],
                            capture_output=True, text=True, timeout=12)
    except Exception:
        return results
    cur = None
    for line in r.stdout.split('\n'):
        stripped = line.strip()
        if line.startswith('BSS '):
            if cur and cur.get('ssid'):
                results.append(cur)
            cur = {'ssid': '', 'signal': -100, 'enc': 'open'}
        elif cur is None:
            continue
        elif stripped.startswith('signal:'):
            try:
                # e.g. "signal: -48.00 dBm"
                cur['signal'] = int(float(stripped.split()[1]))
            except Exception:
                pass
        elif stripped.startswith('SSID:'):
            cur['ssid'] = stripped.split(':', 1)[1].strip()
        elif 'WPA' in stripped and 'version' in stripped.lower():
            cur['enc'] = 'wpa'
        elif 'RSN' in stripped and 'version' in stripped.lower():
            cur['enc'] = 'wpa2'
        elif stripped.startswith('capability:') and 'Privacy' in stripped:
            if cur.get('enc') == 'open':
                cur['enc'] = 'wep'
    if cur and cur.get('ssid'):
        results.append(cur)
    # Deduplicate by SSID, keep strongest
    by_ssid = {}
    for ap in results:
        prev = by_ssid.get(ap['ssid'])
        if prev is None or ap['signal'] > prev['signal']:
            by_ssid[ap['ssid']] = ap
    return sorted(by_ssid.values(), key=lambda x: x['signal'], reverse=True)


# ----------------------------------------------------------------------
# Hotspot (AP) mode
# ----------------------------------------------------------------------

def get_hotspot_state():
    """Return (enabled_bool, ssid_str, key_str)."""
    dis = _uci('get', f'wireless.{HOTSPOT_IFACE}.disabled').strip().strip("'")
    ssid = _uci('get', f'wireless.{HOTSPOT_IFACE}.ssid').strip().strip("'")
    key = _uci('get', f'wireless.{HOTSPOT_IFACE}.key').strip().strip("'")
    return (dis == '0', ssid, key)


# ----------------------------------------------------------------------
# PineAP capture (the modified hostapd interface for handshake harvesting)
# ----------------------------------------------------------------------

PINEAP_IFACE = 'wlan0wpa'


def get_pineap_state():
    """Return True if the PineAP capture interface is currently enabled."""
    dis = _uci('get', f'wireless.{PINEAP_IFACE}.disabled').strip().strip("'")
    return dis == '0'


def get_active_wifi_mode():
    """Return one of 'captive', 'pineap', 'hotspot', 'wifi_attacks',
    or None.

    Used to enforce mutual exclusion: only one of these AP modes can
    run at a time because they share radio0 / the AP interfaces /
    hostapd. Callers refuse to enable a new mode while another is
    active, with a "Disable X first" message.

    Order matters — captive's HTTP server check is the most specific
    (captive uses wlan0mgmt, but so does hotspot, so the server flag
    is the disambiguator).
    """
    try:
        from captive import server as cap_server
        if cap_server.is_running():
            return 'captive'
    except Exception:
        pass
    # Captive AP marker survives a UI restart even though the
    # in-memory server singleton doesn't. If the marker is set,
    # the wlan0mgmt interface belongs to captive portal, not the
    # user's hotspot — don't misreport it as 'hotspot'.
    try:
        from captive import ap_control
        if ap_control.marker_exists():
            return 'captive'
    except Exception:
        pass
    # SSID Spam leaves /tmp/pagerctl_ssid_spam_wireless.bak while
    # active — simpler signal than importing the attacks module
    # (avoids a circular import into the wardrive/ package).
    try:
        import os as _os
        if _os.path.isfile('/tmp/pagerctl_ssid_spam_wireless.bak'):
            return 'wifi_attacks'
    except Exception:
        pass
    if get_pineap_state():
        return 'pineap'
    try:
        enabled, _, _ = get_hotspot_state()
        if enabled:
            return 'hotspot'
    except Exception:
        pass
    return None


def set_pineap_capture(enabled):
    """Enable/disable the PineAP/Karma capture interface (wlan0wpa).

    Mutually exclusive with the management hotspot — enabling this
    interface forces the hotspot interface (wlan0mgmt) off and vice
    versa, since they both want exclusive use of radio0 in conflicting
    modes (PineAP karma intercepts probes; the hotspot wants normal
    client associations).

    Also starts/stops the pineapd service so its UI/management
    daemon is active when the capture interface is up.
    """
    _uci_set(f'wireless.{PINEAP_IFACE}.disabled', '0' if enabled else '1')
    if enabled:
        # Free the radio for PineAP — disable the management hotspot
        _uci_set(f'wireless.{HOTSPOT_IFACE}.disabled', '1')
    _uci_commit('wireless')
    _wifi_reload()
    # pineapd controls the PineAP UI/state. Start/stop it.
    try:
        if enabled:
            subprocess.run(['/etc/init.d/pineapd', 'start'],
                            capture_output=True, timeout=10)
        else:
            subprocess.run(['/etc/init.d/pineapd', 'stop'],
                            capture_output=True, timeout=10)
    except Exception:
        pass
    return True


def _client_channel():
    """Return the channel (int) wlan0cli is currently associated to,
    or None if not associated. Used to align the radio channel with
    the upstream when bringing up an AP on the same radio."""
    try:
        r = subprocess.run(['iw', 'dev', CLIENT_IFACE, 'link'],
                            capture_output=True, text=True, timeout=3)
        for line in r.stdout.split('\n'):
            line = line.strip()
            if line.startswith('freq:'):
                freq = int(float(line.split()[1]))
                if 2412 <= freq <= 2484:
                    return (freq - 2407) // 5 if freq != 2484 else 14
                if 5170 <= freq <= 5825:
                    return (freq - 5000) // 5
    except Exception:
        pass
    return None


def set_hotspot(enabled, ssid=None, password=None):
    """Enable/disable the hotspot AP interface. Optionally update
    ssid + key. Runs `wifi reload` on success.

    Uses wlan0mgmt (the honest management AP), NOT wlan0wpa which
    is the PineAP/Karma capture interface and won't accept normal
    client associations.

    When enabling, also:
      - Aligns radio0.channel with whatever channel wlan0cli is
        currently connected to. Required because STA+AP on the
        same radio must share a channel.
      - Disables wlan0wpa so the PineAP capture interface doesn't
        contend with the management AP.

    The pager is pre-wired: wlan0mgmt is a member of br-lan
    (172.16.52.0/24) with DHCP via dhcp.lan, and the firewall
    lan→wan zone forwarding is enabled with masquerading on wan
    (which contains wlan0cli). So enabling the AP automatically
    gives clients DHCP and NAT out through the upstream WiFi
    connection — no network/firewall changes needed.
    """
    if ssid:
        _uci_set(f'wireless.{HOTSPOT_IFACE}.ssid', ssid)
    if password is not None:
        _uci_set(f'wireless.{HOTSPOT_IFACE}.encryption', 'psk2')
        _uci_set(f'wireless.{HOTSPOT_IFACE}.key', password)
    _uci_set(f'wireless.{HOTSPOT_IFACE}.mode', 'ap')
    _uci_set(f'wireless.{HOTSPOT_IFACE}.disabled',
             '0' if enabled else '1')
    if enabled:
        # Match radio channel to whatever the client is on, so the
        # AP and STA don't fight for the radio.
        chan = _client_channel()
        if chan:
            _uci_set('wireless.radio0.channel', str(chan))
        # Make sure the PineAP capture iface isn't enabled too —
        # it confuses normal clients on the same radio.
        _uci_set('wireless.wlan0wpa.disabled', '1')
    _uci_commit('wireless')
    _wifi_reload()
    return True


# ----------------------------------------------------------------------
# Network recovery — restore /etc/config/{wireless,network,firewall,dhcp}
# from baseline copies snapshotted at install time
# ----------------------------------------------------------------------

BASELINE_DIR = '/etc/pagerctl_baseline'
BASELINE_CONFIGS = ('wireless', 'network', 'firewall', 'dhcp')


def fix_net():
    """Restore all four wireless/network/firewall/dhcp configs from the
    baseline snapshot taken at install time, then restart all related
    services. Used by the Settings → WiFi → Fix Net button as a
    one-click recovery if the user breaks something.

    Returns (success, restored_count, message).
    """
    if not os.path.isdir(BASELINE_DIR):
        return (False, 0, 'No baseline')
    restored = 0
    for cfg in BASELINE_CONFIGS:
        src = os.path.join(BASELINE_DIR, cfg)
        dst = os.path.join('/etc/config', cfg)
        if not os.path.isfile(src):
            continue
        try:
            with open(src, 'rb') as fs, open(dst, 'wb') as fd:
                fd.write(fs.read())
            restored += 1
        except Exception:
            pass
    if restored == 0:
        return (False, 0, 'No baseline files')
    # Reload services in dependency order
    try:
        subprocess.run(['/etc/init.d/network', 'reload'], capture_output=True, timeout=20)
    except Exception:
        pass
    try:
        subprocess.run(['/etc/init.d/firewall', 'reload'], capture_output=True, timeout=20)
    except Exception:
        pass
    try:
        subprocess.run(['/etc/init.d/dnsmasq', 'reload'], capture_output=True, timeout=20)
    except Exception:
        pass
    _wifi_reload()
    return (True, restored, f'Restored {restored} configs')
