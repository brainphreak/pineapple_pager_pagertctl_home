"""ssid_spam.py - UCI-managed SSID beacon flood.

Raw monitor-mode injection is blocked by the mt76 driver (tested:
0 TX packets for beacon + deauth). So we drive real OpenWrt AP
interfaces via uci + wifi reload, which is the same mechanism the
stock hostapd uses and the driver is happy with.

Strategy:
  1. Snapshot /etc/config/wireless.
  2. Remove the wlan1mon wifi-iface so radio1 is available.
  3. Add N wifi-iface entries (mode=ap) on radio1 with SSIDs from
     the pack. Try up to MAX_BSS; the driver may accept multi-BSS or
     refuse — if refused, only one AP comes up and we rotate.
  4. wifi reload. OpenWrt / hostapd bring up the AP(s).
  5. Background thread rotates the SSIDs every ROTATE_INTERVAL
     seconds via `uci set ...ssid='next'; uci commit; wifi reload`.
  6. On stop: restore the snapshot, wifi reload.

Snapshot + restore makes this safe — any failure during start
triggers a full restore. Other dashboards (wardrive / captive) see
their interfaces come back as they were.
"""

import os
import random
import subprocess
import threading
import time


HERE = os.path.dirname(os.path.abspath(__file__))
PACKS_DIR = os.path.join(HERE, 'packs')

WIRELESS_CONF = '/etc/config/wireless'
SNAPSHOT = '/tmp/pagerctl_ssid_spam_wireless.bak'

# UCI section prefix for our transient wifi-ifaces. Removed on stop.
SECTION_PREFIX = 'pagerspam_'

# mt76 on phy1 reports num_global_macaddr=1 in hostapd conf — driver
# only supports ONE BSSID per radio. We compensate with a two-tier
# rotation:
#
#   - Fast tier (every ROTATE_INTERVAL): `wifi reload` pushes a new
#     SSID through the already-running hostapd. Cheap (<1s), no
#     blackout, scanners catch several SSIDs per 3-5s scan sweep.
#     BSSID stays constant during this tier — scanners that dedupe
#     by BSSID just update the SSID they show.
#
#   - Slow tier (every MAC_ROTATE_EVERY fast ticks): full
#     `wifi down/up radio1` cycle which forces hostapd to re-read
#     macaddr from UCI and come up with a fresh BSSID. Scanner sees
#     this as a brand-new AP and adds it to the cache. Over the
#     scanner's ~30-60s cache window, multiple distinct APs
#     accumulate this way, each with the SSID that was live when
#     the MAC flipped.
MAX_BSS = 1
ROTATE_INTERVAL = 1.0
MAC_ROTATE_EVERY = 10

# Available rotation intervals (seconds) surfaced in the UI as the
# `Rotate` cycle. Lower = more SSIDs per scan sweep but more wifi
# reload churn; higher = less churn but fewer unique SSIDs visible
# before the scanner's cache window closes.
ROTATE_OPTIONS = [0.25, 0.5, 1.0, 2.0, 5.0]

PRIMARY_RADIO = 'radio1'
SECONDARY_RADIO = 'radio0'  # shared with wlan0cli — don't reconfigure
NETWORK = 'lan'

# Remember the primary radio's existing settings so stop() can
# restore them. Secondary radio (radio0) is never reconfigured.
_radio_snapshot = {}

_state = {
    'running': False,
    'started_at': None,
    'ssid_count': 0,
    'errors': 0,
    'error_msg': None,
    'frames': 0,
    'captures': 0,
    'bss_count': 0,  # how many APs actually came up
}
_stop_event = None
_rotate_thread = None
_snapshot_taken = False


def _load_pack(name):
    for path in (os.path.join(PACKS_DIR, f'{name}.txt'),
                 os.path.join(PACKS_DIR, name)):
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                lines = [ln.strip()[:32] for ln in f
                         if ln.strip() and not ln.strip().startswith('#')]
                if lines:
                    return lines
        except Exception:
            pass
    return []


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None


def _uci(*args, timeout=5):
    return _run(['uci'] + list(args), timeout=timeout)


def _wifi_reload(timeout=15):
    return _run(['wifi', 'reload'], timeout=timeout)


def _wifi_restart_radio(timeout=15):
    """Force a full radio teardown + bring-up so hostapd picks up a
    new `macaddr` from UCI. `wifi reload` alone just pushes SSID
    updates via hostapd's control interface and leaves the netdev's
    MAC frozen at whatever was there on initial bring-up. This is
    how we get a fresh BSSID every rotation so scanners accumulate
    all the SSIDs in the pack instead of deduping by BSSID."""
    _run(['wifi', 'down', PRIMARY_RADIO], timeout=timeout)
    _run(['wifi', 'up', PRIMARY_RADIO], timeout=timeout)


def _snapshot_wireless():
    global _snapshot_taken
    try:
        with open(WIRELESS_CONF, 'rb') as src, open(SNAPSHOT, 'wb') as dst:
            dst.write(src.read())
        _snapshot_taken = True
        return True
    except Exception as e:
        _state['error_msg'] = f'snapshot: {e}'[:120]
        return False


def _restore_wireless():
    global _snapshot_taken
    if not _snapshot_taken or not os.path.isfile(SNAPSHOT):
        return
    try:
        with open(SNAPSHOT, 'rb') as src, open(WIRELESS_CONF, 'wb') as dst:
            dst.write(src.read())
        try:
            os.remove(SNAPSHOT)
        except Exception:
            pass
        _snapshot_taken = False
    except Exception as e:
        _state['error_msg'] = f'restore: {e}'[:120]


def _configure_radio(channel):
    """Force radio1 to 2.4GHz + fixed channel so hostapd will accept
    the AP. radio1 (phy1) is tri-band capable so we can switch it.
    Snapshots the prior band/channel/htmode for restore on stop.
    Secondary radio (radio0) is never touched — it's shared with
    wlan0cli (the user's WiFi client) and reconfiguring it would
    drop the connection."""
    for key in ('band', 'channel', 'htmode', 'disabled'):
        r = _uci('get', f'wireless.{PRIMARY_RADIO}.{key}')
        if r and r.returncode == 0:
            _radio_snapshot[key] = r.stdout.strip().strip("'")
    try:
        ch = int(channel) if str(channel).isdigit() else 6
    except (TypeError, ValueError):
        ch = 6
    _uci('set', f'wireless.{PRIMARY_RADIO}.band=2g')
    _uci('set', f'wireless.{PRIMARY_RADIO}.channel={ch}')
    _uci('set', f'wireless.{PRIMARY_RADIO}.htmode=HT20')
    _uci('set', f'wireless.{PRIMARY_RADIO}.disabled=0')


def _add_ap_section_on(radio, ssid_idx, ssids, numbered, section_name):
    """Create a single wifi-iface on `radio` broadcasting ssids[ssid_idx]
    with a deterministic per-SSID BSSID. Used for both primary and
    secondary radios."""
    ssid = _format_ssid(ssids[ssid_idx], ssid_idx, len(ssids), numbered)
    mac = _bssid_for(ssids[ssid_idx], ssid_idx)
    _uci('set', f'wireless.{section_name}=wifi-iface')
    _uci('set', f'wireless.{section_name}.device={radio}')
    _uci('set', f'wireless.{section_name}.mode=ap')
    _uci('set', f'wireless.{section_name}.network={NETWORK}')
    _uci('set', f'wireless.{section_name}.encryption=none')
    _uci('set', f'wireless.{section_name}.ssid={ssid}')
    _uci('set', f'wireless.{section_name}.macaddr={mac}')
    _uci('set', f'wireless.{section_name}.hidden=0')


def _restore_radio():
    for key, val in _radio_snapshot.items():
        _uci('set', f'wireless.{PRIMARY_RADIO}.{key}={val}')
    _radio_snapshot.clear()


def _delete_wlan1mon_iface():
    """Remove the UCI wifi-iface that owns wlan1mon so radio1 is
    free for our AP sections. The uci show key name isn't stable
    across installs (can be wlan1mon or default_radio2) so we
    enumerate."""
    r = _uci('show', 'wireless', timeout=5)
    if r is None or r.returncode != 0:
        return
    for line in (r.stdout or '').splitlines():
        if '=wifi-iface' not in line:
            continue
        # line: wireless.NAME=wifi-iface
        key = line.split('=', 1)[0]
        name = key.split('.', 1)[1]
        # Check if this iface is a monitor on radio1
        dev = (_uci('get', f'{key}.device').stdout or '').strip().strip("'")
        mode = (_uci('get', f'{key}.mode').stdout or '').strip().strip("'")
        if dev == PRIMARY_RADIO and mode == 'monitor':
            _uci('delete', key)


def _bssid_for(ssid, index):
    """Deterministic per-SSID fake BSSID. Different MAC per SSID so
    scanners don't dedupe and the whole pack accumulates in the
    wifi list instead of replacing the previous entry each rotation.
    Locally-administered bit set, multicast bit cleared."""
    rng = random.Random(f'{index}|{ssid}')
    mac = [rng.randint(0, 255) for _ in range(6)]
    mac[0] = (mac[0] & 0xFC) | 0x02
    return ':'.join(f'{b:02x}' for b in mac)


def _format_ssid(raw, index, total, numbered):
    """Optionally prefix with a zero-padded index so scanners that
    sort alphabetically display the pack in order. SSID max is 32
    bytes (802.11), so numbers eat a few chars off the end."""
    if not numbered:
        return raw[:32]
    pad = max(2, len(str(total)))
    prefix = f'{index + 1:0{pad}d} '
    budget = 32 - len(prefix)
    return prefix + raw[:budget]


def _add_ap_sections(ssids, numbered, radios):
    """Create one wifi-iface AP per radio in `radios`. Returns a
    list of (radio_name, section_name, initial_ssid_idx) tuples.
    Radios broadcasting simultaneously get offset initial SSID
    indices so they cover different slices of the pack."""
    sections = []
    n_radios = len(radios)
    for r_i, radio in enumerate(radios):
        # Half-pack offset for second radio (1/2, 1/3, ... of len)
        initial_idx = (r_i * len(ssids)) // max(1, n_radios)
        section = f'{SECTION_PREFIX}{radio}_0'
        _add_ap_section_on(radio, initial_idx, ssids, numbered, section)
        sections.append((radio, section, initial_idx))
    _uci('commit', 'wireless')
    return sections


def _remove_our_sections():
    r = _uci('show', 'wireless', timeout=5)
    if r is None or r.returncode != 0:
        return
    for line in (r.stdout or '').splitlines():
        key = line.split('=', 1)[0]
        if f'.{SECTION_PREFIX}' in key and '.' not in key.split(f'.{SECTION_PREFIX}', 1)[1]:
            _uci('delete', key)
    _uci('commit', 'wireless')


def _count_up_bss():
    """How many of our APs actually came up after wifi reload? Counts
    interfaces in `iw dev` whose name matches our section mapping.
    The kernel names them unpredictably (wlan1, wlan1-1, etc.) so we
    just count AP-type interfaces on phy1."""
    r = _run(['iw', 'dev'])
    if r is None:
        return 0
    ap_count = 0
    in_phy1 = False
    current_type = None
    for line in (r.stdout or '').splitlines():
        s = line.strip()
        if s.startswith('phy#'):
            in_phy1 = s == 'phy#1'
            continue
        if in_phy1 and s.startswith('type '):
            if s == 'type AP':
                ap_count += 1
    return ap_count


def _rotate_loop(ssids, sections, numbered, stop_event, interval=ROTATE_INTERVAL):
    """Two-tier rotation across one or more radios.

    Fast tier (every tick, ~1s): update each radio's AP SSID via
    `wifi reload`. No blackout. Scanners deduping by BSSID see each
    live AP cycle through multiple SSIDs per scan sweep.

    Slow tier (every MAC_ROTATE_EVERY ticks, staggered across
    radios): full `wifi down/up <radio>` on one radio at a time so
    at least one AP stays live during the 1-2s restart blackout.
    The restarted radio comes up with a fresh BSSID from its UCI
    `macaddr`, adding a new AP to scanner caches.

    Each section has its own idx (seeded from initial offsets) so
    dual-radio mode covers different slices of the pack at any
    given moment."""
    if not sections:
        return

    # Per-section state: current idx into ssids
    state = [(radio, section, initial) for radio, section, initial in sections]
    # idx for each section advances independently
    idxs = [initial for (_, _, initial) in sections]
    ticks = 0

    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            break

        # Which radio (if any) is due for a MAC flip this tick —
        # stagger so only one radio restarts per MAC-flip window.
        flip_i = -1
        if ticks % MAC_ROTATE_EVERY == (MAC_ROTATE_EVERY - 1):
            flip_i = (ticks // MAC_ROTATE_EVERY) % len(sections)

        # Advance each section's SSID
        for i, (radio, section, _initial) in enumerate(state):
            idxs[i] = (idxs[i] + 1) % len(ssids)
            raw_idx = idxs[i]
            ssid = _format_ssid(ssids[raw_idx], raw_idx,
                                len(ssids), numbered)
            _uci('set', f'wireless.{section}.ssid={ssid}')
            if i == flip_i:
                mac = _bssid_for(ssids[raw_idx], raw_idx)
                _uci('set', f'wireless.{section}.macaddr={mac}')
        _uci('commit', 'wireless')

        if flip_i >= 0:
            # Bounce just that radio. Other radios keep broadcasting
            # during the ~1-2s blackout.
            radio = state[flip_i][0]
            _run(['wifi', 'down', radio], timeout=15)
            _run(['wifi', 'up', radio], timeout=15)
        else:
            _wifi_reload()

        ticks += 1


def _cleanup_stale_state():
    """If a prior run left UCI `pagerspam_*` sections or a stale
    snapshot on disk, tear them down before we take a fresh snapshot.
    Otherwise `_snapshot_wireless()` would capture the polluted
    config as the "original" and stop() would restore a broken
    state. Also covers the case where pagerctl_home was killed
    mid-run so `resume_if_running()` never got to hook in."""
    had_stale = False

    # If a snapshot file exists, restore it (represents the real
    # pre-run config); then nuke any pagerspam sections that survived.
    if os.path.isfile(SNAPSHOT):
        had_stale = True
        try:
            with open(SNAPSHOT, 'rb') as src, open(WIRELESS_CONF, 'wb') as dst:
                dst.write(src.read())
        except Exception:
            pass
        try:
            os.remove(SNAPSHOT)
        except Exception:
            pass

    # Belt-and-suspenders: scrub any pagerspam sections directly even
    # if no snapshot existed (someone rebooted mid-run).
    r = _uci('show', 'wireless', timeout=5)
    if r and r.returncode == 0:
        for line in (r.stdout or '').splitlines():
            if f'.{SECTION_PREFIX}' not in line:
                continue
            key = line.split('=', 1)[0]
            parts = key.split(f'.{SECTION_PREFIX}', 1)
            if len(parts) != 2 or '.' in parts[1]:
                continue
            had_stale = True
            _uci('delete', key)
        _uci('commit', 'wireless')

    if had_stale:
        _wifi_reload()
    return had_stale


def start(config):
    global _stop_event, _rotate_thread

    if _state['running']:
        return True

    # Clean up leftovers from a prior (crashed / force-killed) run
    # before we snapshot — otherwise we'd snapshot the polluted state
    # and stop() would restore that instead of the real original.
    _cleanup_stale_state()

    pack = config.get('wifi_attack_pack', 'rickroll')
    ssids = _load_pack(pack)
    if not ssids:
        _state['errors'] += 1
        _state['error_msg'] = 'empty pack'
        return False

    _state['ssid_count'] = len(ssids)
    _state['errors'] = 0
    _state['error_msg'] = None
    _state['frames'] = 0
    _state['bss_count'] = 0

    if not _snapshot_wireless():
        _state['errors'] += 1
        return False

    channel = config.get('wifi_attack_channel', '6')
    numbered = bool(config.get('wifi_attack_numbered', True))
    dual = bool(config.get('wifi_attack_dual_radio', False))
    try:
        interval = float(config.get('wifi_attack_rotate', ROTATE_INTERVAL))
    except (TypeError, ValueError):
        interval = ROTATE_INTERVAL
    if interval <= 0:
        interval = ROTATE_INTERVAL

    radios = [PRIMARY_RADIO]
    if dual:
        # Secondary radio (radio0) rides on the client's channel
        # (radio's #channels <= 1 constraint). We do NOT reconfigure
        # radio0 — its client wlan0cli stays on whatever channel it
        # was using.
        radios.append(SECONDARY_RADIO)

    try:
        _delete_wlan1mon_iface()
        _configure_radio(channel)
        # Dual mode adds an AP on phy0 (radio0). phy0's mt76 driver
        # refuses AP + monitor in the same interface combination, so
        # wlan0mon has to go down while we're active. The full
        # /etc/config/wireless snapshot above restores it on stop.
        # wlan0cli (your client) stays up — client + AP + other-sta
        # is a permitted combo.
        if dual:
            # phy0's driver rejects AP alongside monitor AND often
            # alongside managed sta too. Dual mode commits to maximum
            # broadcast visibility — disable everything on phy0
            # except our AP. The snapshot restores wlan0cli /
            # wlan0mon / dummy_radio0 on stop.
            _uci('set', 'wireless.wlan0mon.disabled=1')
            _uci('set', 'wireless.wlan0cli.disabled=1')
            _uci('set', 'wireless.dummy_radio0.disabled=1')
        sections = _add_ap_sections(ssids, numbered=numbered, radios=radios)
    except Exception as e:
        _state['errors'] += 1
        _state['error_msg'] = f'uci: {e}'[:120]
        _restore_wireless()
        _wifi_reload()
        return False

    r = _wifi_reload()
    if r is None or r.returncode != 0:
        _state['errors'] += 1
        _state['error_msg'] = 'wifi reload failed'[:120]
        _restore_wireless()
        _wifi_reload()
        return False

    # Give OpenWrt/hostapd a moment to bring APs up
    time.sleep(2)
    _state['bss_count'] = _count_up_bss()

    _state['running'] = True
    _state['started_at'] = time.time()

    _stop_event = threading.Event()
    _rotate_thread = threading.Thread(
        target=_rotate_loop,
        args=(ssids, sections, numbered, _stop_event, interval),
        daemon=True,
    )
    _rotate_thread.start()
    return True


def stop():
    global _stop_event, _rotate_thread

    if not _state['running']:
        return

    # Stop the rotation thread first so it doesn't race us.
    if _stop_event is not None:
        _stop_event.set()
    if _rotate_thread is not None and _rotate_thread.is_alive():
        _rotate_thread.join(timeout=max(ROTATE_INTERVAL + 1, 4))

    # Remove our AP sections and restore the original wireless config.
    try:
        _remove_our_sections()
    except Exception:
        pass
    _restore_wireless()
    _wifi_reload()

    _state['running'] = False
    _state['bss_count'] = 0
    _stop_event = None
    _rotate_thread = None


def is_running():
    """Return True only if we think we're running AND the live
    state backs it up (at least one phy*-ap interface in AP mode).
    Flips the flag back to False if hostapd got wedged externally,
    so the UI stops claiming RUNNING when nothing's actually
    broadcasting."""
    if not _state['running']:
        return False
    try:
        r = _run(['iw', 'dev'], timeout=3)
        if r and r.returncode == 0:
            text = r.stdout or ''
            # Any line like "type AP" inside the iw dev output means
            # at least one AP is up. Cheap, no-dependency check.
            if 'type AP' in text:
                return True
            _state['running'] = False
            _state['error_msg'] = 'hostapd AP vanished'
    except Exception:
        pass
    return _state['running']


def resume_if_running():
    """Called at pagerctl_home startup. A previous run may still be
    broadcasting via hostapd + our UCI sections; if so, restore the
    Python-side state so the UI correctly shows RUNNING and the
    rotation thread resumes cycling. The user's Stop button then
    tears it down cleanly the normal way.

    Returns True if we resumed a prior session."""
    global _stop_event, _rotate_thread, _snapshot_taken

    have_snapshot = os.path.isfile(SNAPSHOT)
    # (radio, section_name, initial_idx=0) tuples for the rotation thread
    leftover_sections = []
    r = _run(['uci', 'show', 'wireless'], timeout=5)
    if r and r.returncode == 0:
        found = {}  # section -> radio
        for line in (r.stdout or '').splitlines():
            if f'.{SECTION_PREFIX}' not in line:
                continue
            key = line.split('=', 1)[0]
            parts = key.split(f'.{SECTION_PREFIX}', 1)
            if len(parts) != 2 or '.' in parts[1]:
                continue
            section = f'{SECTION_PREFIX}{parts[1]}'
            # Look up the radio this section targets
            dr = _uci('get', f'{key}.device')
            if dr and dr.returncode == 0:
                radio = dr.stdout.strip().strip("'")
                if radio:
                    found[section] = radio
        for section, radio in sorted(found.items()):
            leftover_sections.append((radio, section, 0))

    if not have_snapshot or not leftover_sections:
        return False

    # Reload the pack from current settings so rotation continues
    # with the expected list. Config is read from wardrive/settings.json
    # via load_config (same path the UI uses).
    try:
        from wardrive.config import load_config as _lc
        cfg = _lc()
    except Exception:
        cfg = {}
    pack = cfg.get('wifi_attack_pack', 'rickroll')
    numbered = bool(cfg.get('wifi_attack_numbered', True))
    try:
        interval = float(cfg.get('wifi_attack_rotate', ROTATE_INTERVAL))
    except (TypeError, ValueError):
        interval = ROTATE_INTERVAL
    if interval <= 0:
        interval = ROTATE_INTERVAL
    ssids = _load_pack(pack)
    if not ssids:
        return False

    _snapshot_taken = True  # so a stop() call restores the snapshot
    _state['running'] = True
    _state['started_at'] = time.time()
    _state['ssid_count'] = len(ssids)
    _state['errors'] = 0
    _state['error_msg'] = None
    _state['frames'] = 0

    _stop_event = threading.Event()
    _rotate_thread = threading.Thread(
        target=_rotate_loop,
        args=(ssids, leftover_sections, numbered, _stop_event, interval),
        daemon=True,
    )
    _rotate_thread.start()
    return True


def stats():
    if _state['running'] and _state['started_at']:
        _state['frames'] = int(time.time() - _state['started_at'])
    return {
        'running': _state['running'],
        'frames': _state['frames'],
        'errors': _state['errors'],
        'captures': _state.get('captures', 0),
        'ssid_count': _state['ssid_count'],
        'bss_count': _state.get('bss_count', 0),
        'error_msg': _state.get('error_msg'),
    }
