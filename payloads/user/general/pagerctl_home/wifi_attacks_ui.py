"""wifi_attacks_ui.py - WiFi Attacks toolkit dashboard.

Captive-style two-column grid. One `Mode` cell selects which attack
primitive is active (Scan / Probe Mon / SSID Spam / Jammer / Handshake
/ WPS / Karma); other cells reconfigure and show live counters as the
attack runs.

Initial scaffolding — the UI + navigation are complete, but the
attack handlers under attacks/ are stubs. Each Mode currently renders
its cells as placeholders with no backend. Wire up attacks one at a
time by filling in attacks/<mode>.py and pointing the Mode handler
at it.
"""

import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wardrive'))

from wardrive.config import (load_config, save_config, SCREEN_W, SCREEN_H,
                              FONT_TITLE, FONT_MENU)
from wardrive.web_server import wait_any_button, drain_virt_button
from pagerctl import PAGER_EVENT_PRESS

import pager_dialogs
import theme_utils
import screen_power

from attacks import ssid_spam, handshake


# Maps Mode name → backend module. Only backends with working
# implementations land here; other modes are still scaffolding.
_ATTACK_BACKENDS = {
    'SSID Spam': ssid_spam,
    'Handshake': handshake,
}


HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_CFG = None


def _wifi_attacks_dashboard_cfg():
    global _CONFIG_CFG
    if _CONFIG_CFG is None:
        td = os.environ.get('PAGERCTL_THEME',
            os.path.join(HERE, 'themes', 'Circuitry'))
        try:
            with open(os.path.join(td,
                      'components/dashboards/wifi_attacks_dashboard.json')) as f:
                _CONFIG_CFG = json.load(f)
        except Exception:
            _CONFIG_CFG = {}
    return _CONFIG_CFG


# Ordered list of attack modes. First entry is the default on fresh
# installs. No Idle mode — Start / Stop + State already make it
# obvious when nothing is running.
# Only modes whose attack backend is feasible on this pager's mt76
# hardware are listed. Modes requiring monitor-mode frame injection
# (Jammer/Deauth, Karma, WPS) are removed because the driver blocks
# injected frames silently — confirmed by 0 TX packets on test
# sends of beacons AND deauth frames. Re-add if we find a working
# injection path.
MODES = ['SSID Spam', 'Handshake', 'Probe Mon', 'Scan']


# SSID pack metadata — shown in the Pack picker popup so the user
# knows what each option broadcasts before selecting. Keep each
# description short enough to fit in the picker labels.
SSID_PACKS = [
    ('rickroll', 'Rickroll',
     '25 SSIDs of Rick Astley "Never Gonna Give You Up" lyrics'),
    ('classics', 'Classics',
     'Common public WiFi names: Starbucks, Xfinity, attwifi, linksys...'),
    ('nearby',   'Nearby',
     'Clone every AP currently visible on a fresh scan'),
    ('custom',   'Custom',
     'User-supplied list at attacks/packs/custom.txt'),
]


class WifiAttacksUI:

    def __init__(self):
        self.config = load_config()
        self.pager = None
        self.engine = None
        self.is_foreground = False
        self.running = False
        self.sel_col = 0
        self.sel_row = 0
        self._last_sel = None
        self._label_w_cache = {}

    def run(self, pager, engine=None):
        self.pager = pager
        self.engine = engine
        self.is_foreground = True
        self.config = load_config()

        self.WHITE = theme_utils.get_color(pager, 'white')
        self.GREEN = theme_utils.get_color(pager, 'green')
        self.RED = theme_utils.get_color(pager, 'red')
        self.YELLOW = theme_utils.get_color(pager, 'warning_accent')
        self.CYAN = theme_utils.get_color(pager, 'info_accent')
        self.DIM = theme_utils.get_color(pager, 'dim')
        self.ORANGE = theme_utils.get_color(pager, 'orange')

        self._apply_dashboard_cfg()

        # Default background — reuses the Circuitry terminal bg like
        # captive_dashboard. Themes can override via a bg_image key in
        # wifi_attacks_dashboard.json.
        self.bg_handle = None
        bg_rel = _wifi_attacks_dashboard_cfg().get('bg_image',
            'assets/alert_dialog_bg_term_blue.png')
        td = os.environ.get('PAGERCTL_THEME',
            os.path.join(HERE, 'themes', 'Circuitry'))
        bg_path = os.path.join(td, bg_rel)
        if os.path.isfile(bg_path):
            try:
                self.bg_handle = pager.load_image(bg_path)
            except Exception:
                self.bg_handle = None

        pager.clear_input_events()

        # Screen dim/off timers — honors Settings > Display > Dim
        # Timeout / Screen Timeout the same as the main dashboard.
        sp = screen_power.ScreenPower(load_config)
        sp.register_activity()

        last_full_render = 0
        last_counter_refresh = 0
        try:
            while True:
                sp.tick(pager)
                now = time.time()
                # Skip rendering entirely when the screen is off.
                if not sp.is_off():
                    if last_full_render == 0:
                        self._render(full=True)
                        last_full_render = now
                        last_counter_refresh = now
                    elif now - last_counter_refresh >= 2.0:
                        self._render(full=True)
                        last_counter_refresh = now

                exit_result = None
                while pager.has_input_events():
                    ev = pager.get_input_event()
                    if not ev:
                        break
                    btn, etype, _ = ev
                    if etype != PAGER_EVENT_PRESS:
                        continue
                    # Dormant (dim or off) → any press wakes and is
                    # absorbed (not dispatched) so the first press
                    # just wakes the screen.
                    if sp.is_dormant():
                        sp.wake(pager)
                        last_full_render = 0
                        break
                    sp.register_activity()
                    prev_sel = (self.sel_col, self.sel_row)
                    handled = self._handle_button(btn)
                    if handled is not None:
                        exit_result = handled
                        break
                    new_sel = (self.sel_col, self.sel_row)
                    if new_sel != prev_sel:
                        self._render_selection_change(prev_sel, new_sel)
                    else:
                        last_full_render = 0

                if exit_result == 'power':
                    self.is_foreground = False
                    return 'power'
                if exit_result == 'b':
                    self.is_foreground = False
                    return None

                v = drain_virt_button()
                if v:
                    btn_map = {
                        'up': pager.BTN_UP, 'down': pager.BTN_DOWN,
                        'left': pager.BTN_LEFT, 'right': pager.BTN_RIGHT,
                        'a': pager.BTN_A, 'b': pager.BTN_B,
                    }
                    vb = btn_map.get(v)
                    if vb:
                        if sp.is_dormant():
                            sp.wake(pager)
                            last_full_render = 0
                        else:
                            sp.register_activity()
                            prev_sel = (self.sel_col, self.sel_row)
                            handled = self._handle_button(vb)
                            if handled == 'b':
                                self.is_foreground = False
                                return None
                            if handled == 'power':
                                self.is_foreground = False
                                return 'power'
                            new_sel = (self.sel_col, self.sel_row)
                            if new_sel != prev_sel:
                                self._render_selection_change(prev_sel, new_sel)

                time.sleep(0.04)
        finally:
            self.is_foreground = False

    def _apply_dashboard_cfg(self):
        cfg = _wifi_attacks_dashboard_cfg()
        grid = cfg.get('grid') or {}
        text = cfg.get('text') or {}

        col_x = grid.get('col_x') or [25, 240]
        self._COL_X = tuple(int(v) for v in col_x)
        self._ROW_Y_START = int(grid.get('row_y_start', 30))
        self._ROW_H = int(grid.get('row_height', 24))
        self._LABEL_GAP = int(grid.get('label_gap', 6))

        fs = text.get('font_size', 17)
        self._FONT_SIZE = (int(fs) if isinstance(fs, (int, float))
                            else theme_utils.get_size(fs or 'medium'))

        p = self.pager
        self._LABEL_SEL_C = theme_utils.get_color(
            p, text.get('label_selected', 'warning_accent'))
        self._LABEL_UNSEL_C = theme_utils.get_color(
            p, text.get('label_unselected', 'dim'))
        self._VALUE_DEFAULT_C = theme_utils.get_color(
            p, text.get('value_default', 'white'))

    # -- Item grid build ---------------------------------------------

    def _items(self):
        """Return a list of (col, row, item) tuples. Which cells show
        depends on the current Mode — cells not relevant to the mode
        are omitted entirely."""
        # Sync running flag with backend state every render so the
        # button label flips when an attack stops on its own.
        self._refresh_running()

        c = self.config
        mode = c.get('wifi_attack_mode', MODES[0])
        target_raw = c.get('wifi_attack_target')
        if isinstance(target_raw, dict):
            target = (target_raw.get('ssid')
                      or target_raw.get('bssid', '')
                      or '(set)')
        elif target_raw:
            target = str(target_raw)
        else:
            target = 'All'
        channel = c.get('wifi_attack_channel', 'auto')
        iface = c.get('wifi_attack_iface', 'wlan1mon')
        burst = c.get('wifi_attack_burst', 64)
        duration = c.get('wifi_attack_duration', '30s')
        pack = c.get('wifi_attack_pack', 'rickroll')
        wps_method = c.get('wifi_attack_wps_method', 'pixie')
        deauth_assist = c.get('wifi_attack_deauth_assist', False)

        backend = _ATTACK_BACKENDS.get(mode)
        stats = backend.stats() if backend else {}
        frames = stats.get('frames', 0)
        errors = stats.get('errors', 0)
        captures = stats.get('captures', 0)

        state_label = 'RUNNING' if self.running else 'IDLE'
        state_color = self.GREEN if self.running else self.DIM
        action_label = 'Stop' if self.running else 'Start'
        action_color = self.RED if self.running else self.GREEN

        # Action is an explicit button — cycling Mode never starts an
        # attack. You have to navigate to the Start cell and press A.
        left = [
            {'label': 'Mode', 'value': mode, 'value_color': self.YELLOW,
             'type': 'picker', 'action': self._pick_mode_menu},
            {'label': action_label, 'value': '',
             'value_color': action_color,
             'type': 'action', 'action': self._toggle_attack,
             'no_colon': True},
            {'label': 'State', 'value': state_label,
             'value_color': state_color, 'type': 'readonly'},
            {'label': 'Frames', 'value': str(frames),
             'value_color': self.CYAN, 'type': 'readonly'},
            {'label': 'Captures', 'value': str(captures),
             'value_color': self.YELLOW, 'type': 'readonly'},
            {'label': 'Errors', 'value': str(errors),
             'value_color': self.RED, 'type': 'readonly'},
        ]

        right = []
        if mode in ('Jammer', 'Handshake', 'WPS', 'SSID Spam', 'Scan'):
            right.append({'label': 'Iface', 'value': iface,
                           'value_color': self.WHITE,
                           'type': 'cycle', 'action': self._cycle_iface})
        if mode in ('Jammer', 'Handshake', 'WPS', 'SSID Spam',
                    'Probe Mon', 'Scan'):
            right.append({'label': 'Channel', 'value': str(channel),
                           'value_color': self.WHITE,
                           'type': 'cycle', 'action': self._cycle_channel})
        if mode in ('Jammer', 'Handshake', 'WPS'):
            right.append({'label': 'Target', 'value': target[:14],
                           'value_color': self.WHITE,
                           'type': 'picker',
                           'action': self._pick_target})
        if mode == 'Jammer':
            right.append({'label': 'Burst', 'value': str(burst),
                           'value_color': self.WHITE,
                           'type': 'cycle', 'action': self._cycle_burst})
        if mode in ('Jammer', 'Handshake'):
            right.append({'label': 'Duration', 'value': str(duration),
                           'value_color': self.WHITE,
                           'type': 'cycle', 'action': self._cycle_duration})
        if mode == 'Handshake':
            right.append({'label': 'Deauth Assist',
                           'value': 'ON' if deauth_assist else 'OFF',
                           'value_color': (self.GREEN if deauth_assist
                                           else self.DIM),
                           'type': 'toggle',
                           'action': self._toggle_deauth_assist})
        if mode == 'SSID Spam':
            right.append({'label': 'Pack', 'value': pack,
                           'value_color': self.WHITE,
                           'type': 'picker', 'action': self._pick_pack_menu})
            numbered = c.get('wifi_attack_numbered', True)
            right.append({'label': 'Numbered',
                           'value': 'ON' if numbered else 'OFF',
                           'value_color': self.GREEN if numbered else self.DIM,
                           'type': 'toggle',
                           'action': self._toggle_numbered})
            dual = c.get('wifi_attack_dual_radio', False)
            right.append({'label': 'Dual Radio',
                           'value': 'ON' if dual else 'OFF',
                           'value_color': self.GREEN if dual else self.DIM,
                           'type': 'toggle',
                           'action': self._toggle_dual_radio})
            rotate = c.get('wifi_attack_rotate', 1.0)
            right.append({'label': 'Rotate',
                           'value': f'{float(rotate):g}s',
                           'value_color': self.WHITE,
                           'type': 'cycle',
                           'action': self._cycle_rotate})
        if mode == 'WPS':
            right.append({'label': 'Method', 'value': wps_method,
                           'value_color': self.WHITE,
                           'type': 'cycle', 'action': self._cycle_wps_method})

        items = []
        for row, item in enumerate(left):
            items.append((0, row, item))
        for row, item in enumerate(right):
            items.append((1, row, item))
        return items

    # -- Action handlers (scaffolding — attacks not implemented yet) -

    def _pick_mode_menu(self):
        """Popup menu listing every attack mode. Picking one stores it
        in config — no attack is started. The user still has to press
        the Start button to arm it. Blocked while an attack is
        running so you can't abandon a live backend by accident."""
        if self.running:
            pager_dialogs.popup_menu(
                self.pager, 'Stop current attack first',
                [('OK', 'ok')], bg_drawer=self._bg)
            return
        cur = self.config.get('wifi_attack_mode', MODES[0])
        items = [(m, m) for m in MODES] + [('Cancel', None)]
        try:
            initial = MODES.index(cur)
        except ValueError:
            initial = 0
        picked = pager_dialogs.popup_menu(
            self.pager, 'Attack Mode', items,
            bg_drawer=self._bg, initial_selected=initial)
        if picked:
            self.config['wifi_attack_mode'] = picked
            save_config(self.config)

    def _toggle_attack(self):
        """Explicit Start/Stop action. Never fires from Mode changes —
        only from pressing A on the Start (or Stop when running)
        cell. Dispatches to the backend for the current Mode."""
        mode = self.config.get('wifi_attack_mode', MODES[0])
        backend = _ATTACK_BACKENDS.get(mode)
        if backend is None:
            # Unimplemented mode — toggle the flag so the UI reflects
            # intent, but nothing runs.
            self.running = not self.running
            return
        if backend.is_running():
            backend.stop()
        else:
            if not self._resolve_iface_conflict():
                return
            # Dual-radio spams from phy0 too, which means dropping
            # the client WiFi connection for the duration. Confirm
            # before doing that.
            if (mode == 'SSID Spam'
                    and self.config.get('wifi_attack_dual_radio', False)):
                choice = pager_dialogs.popup_menu(
                    self.pager,
                    'Dual Radio drops WiFi client. Continue?',
                    [('Continue', 'go'), ('Cancel', None)],
                    bg_drawer=self._bg,
                )
                if choice != 'go':
                    return
            backend.start(dict(self.config))
        self._refresh_running()

    def _resolve_iface_conflict(self):
        """Check whether anything else is holding the radio / iface
        the attack wants to use. If so, popup asking the user to
        stop that service; stops it on confirmation. Returns True if
        safe to start, False if the user cancelled or the conflict
        couldn't be cleared."""
        from wardrive import wifi_utils
        conflicts = []

        try:
            mode = wifi_utils.get_active_wifi_mode()
        except Exception:
            mode = None
        if mode == 'captive':
            conflicts.append(('captive', 'Captive Portal'))
        elif mode == 'hotspot':
            conflicts.append(('hotspot', 'Hotspot'))
        elif mode == 'pineap':
            conflicts.append(('pineap', 'PineAP'))

        try:
            from wardrive_ui import _instance as _wd
            if _wd is not None and getattr(_wd, 'scan_state', 'stopped') in (
                    'scanning', 'paused'):
                conflicts.append(('wardrive', 'Wardrive scan'))
        except Exception:
            pass

        if not conflicts:
            return True

        # If multiple blockers exist, prompt once listing them all
        # together; stopping can be done in series on confirmation.
        names = ', '.join(label for _, label in conflicts)
        choice = pager_dialogs.popup_menu(
            self.pager,
            f'{names} is using the interface',
            [('Stop it and continue', 'stop'),
             ('Cancel', 'cancel')],
            bg_drawer=self._bg,
        )
        if choice != 'stop':
            return False

        for kind, _label in conflicts:
            try:
                if kind == 'captive':
                    from captive import server as cap_server
                    from captive import dns_hijack
                    from captive import ap_control
                    cap_server.stop()
                    dns_hijack.disable()
                    ap_control.stop_ap()
                elif kind == 'hotspot':
                    wifi_utils.set_hotspot(False)
                elif kind == 'pineap':
                    wifi_utils.set_pineap_capture(False)
                elif kind == 'wardrive':
                    from wardrive_ui import _instance as _wd
                    if _wd is not None:
                        try:
                            _wd.stop_all()
                        except Exception:
                            pass
            except Exception:
                pass

        return True

    def _refresh_running(self):
        """Sync self.running with the active backend's state so the
        Start/Stop label + State cell stay accurate."""
        mode = self.config.get('wifi_attack_mode', MODES[0])
        backend = _ATTACK_BACKENDS.get(mode)
        self.running = bool(backend and backend.is_running())

    def _cycle_iface(self):
        cur = self.config.get('wifi_attack_iface', 'wlan1mon')
        nxt = 'wlan0mon' if cur == 'wlan1mon' else 'wlan1mon'
        self.config['wifi_attack_iface'] = nxt
        save_config(self.config)

    def _cycle_channel(self):
        cur = str(self.config.get('wifi_attack_channel', 'auto'))
        options = ['auto', '1', '6', '11']
        try:
            i = options.index(cur)
        except ValueError:
            i = 0
        self.config['wifi_attack_channel'] = options[(i + 1) % len(options)]
        save_config(self.config)

    def _pick_target(self):
        """Scan nearby APs and let the user pick one as the attack
        target. Selection is stored in settings as an
        `wifi_attack_target` dict: {ssid, bssid, channel}. Picking
        'All' / clearing sets it to None — backend falls back to
        promiscuous capture on all BSSIDs."""
        if self.running:
            pager_dialogs.popup_menu(
                self.pager, 'Stop current attack first',
                [('OK', 'ok')], bg_drawer=self._bg)
            return
        self._flash_msg('Scanning...')
        networks = self._scan_aps_with_bssid()

        items = [('All (promiscuous)', '__all__')]
        for n in (networks or [])[:20]:
            ssid = (n.get('ssid') or '(hidden)')[:18]
            bssid = n.get('bssid', '')
            sig = n.get('signal', -100)
            ch = n.get('channel', '?')
            enc = n.get('enc', '?')
            label = f'{ssid} {sig}dBm ch{ch} {enc}'
            items.append((label[:54], {
                'ssid': n.get('ssid', ''),
                'bssid': bssid,
                'channel': ch,
                'enc': enc,
            }))
        if len(items) == 1:
            items.append(('(no APs found)', None))
        items.append(('Cancel', None))

        picked = pager_dialogs.popup_menu(
            self.pager, 'Target AP', items, bg_drawer=self._bg)
        if picked == '__all__':
            self.config['wifi_attack_target'] = None
            save_config(self.config)
            return
        if picked is None or not isinstance(picked, dict):
            return
        self.config['wifi_attack_target'] = picked
        save_config(self.config)

    def _scan_aps_with_bssid(self):
        """Return list of {ssid, bssid, signal, channel, enc} dicts
        from `iw dev wlan0cli scan`. Preserves BSSID + channel that
        wifi_utils.scan_networks() discards."""
        import subprocess as _sp
        try:
            r = _sp.run(['iw', 'dev', 'wlan0cli', 'scan'],
                         capture_output=True, text=True, timeout=12)
        except Exception:
            return []
        out = []
        cur = None
        for line in (r.stdout or '').split('\n'):
            s = line.strip()
            if line.startswith('BSS '):
                if cur and cur.get('bssid'):
                    out.append(cur)
                # Parse BSSID from "BSS aa:bb:cc:dd:ee:ff(on ..."
                bssid = ''
                try:
                    bssid = line.split()[1].split('(')[0]
                except Exception:
                    pass
                cur = {'ssid': '', 'bssid': bssid,
                       'signal': -100, 'channel': '?', 'enc': 'open'}
            elif cur is None:
                continue
            elif s.startswith('signal:'):
                try:
                    cur['signal'] = int(float(s.split()[1]))
                except Exception:
                    pass
            elif s.startswith('SSID:'):
                cur['ssid'] = s.split(':', 1)[1].strip()
            elif s.startswith('DS Parameter set: channel '):
                try:
                    cur['channel'] = int(s.split()[-1])
                except Exception:
                    pass
            elif s.startswith('freq:'):
                # 5GHz APs report freq without a DS parameter set.
                try:
                    f = int(s.split()[1])
                    if 2412 <= f <= 2484:
                        cur['channel'] = (f - 2407) // 5 if f != 2484 else 14
                    elif f >= 5000:
                        cur['channel'] = (f - 5000) // 5
                except Exception:
                    pass
            elif 'WPA' in s and 'version' in s.lower():
                cur['enc'] = 'wpa'
            elif 'RSN' in s and 'version' in s.lower():
                cur['enc'] = 'wpa2'
        if cur and cur.get('bssid'):
            out.append(cur)
        out.sort(key=lambda x: x.get('signal', -100), reverse=True)
        return out

    def _flash_msg(self, msg, duration=0.3):
        """Quick centered flash for 'Scanning...' etc."""
        p = self.pager
        self._bg()
        fs = theme_utils.get_size('medium')
        tw = p.ttf_width(msg, FONT_MENU, fs)
        p.draw_ttf((SCREEN_W - tw) // 2, SCREEN_H // 2 - fs // 2, msg,
                   self.YELLOW, FONT_MENU, fs)
        p.flip()
        time.sleep(duration)

    def _cycle_burst(self):
        cur = int(self.config.get('wifi_attack_burst', 64))
        options = [16, 64, 256, 1024]
        try:
            i = options.index(cur)
        except ValueError:
            i = 0
        self.config['wifi_attack_burst'] = options[(i + 1) % len(options)]
        save_config(self.config)

    def _cycle_duration(self):
        cur = str(self.config.get('wifi_attack_duration', '30s'))
        options = ['10s', '30s', '60s', '5m', 'continuous']
        try:
            i = options.index(cur)
        except ValueError:
            i = 0
        self.config['wifi_attack_duration'] = options[(i + 1) % len(options)]
        save_config(self.config)

    def _toggle_deauth_assist(self):
        cur = bool(self.config.get('wifi_attack_deauth_assist', False))
        self.config['wifi_attack_deauth_assist'] = not cur
        save_config(self.config)

    def _pick_pack_menu(self):
        """Popup picker for SSID packs. Shows 'Name - short description'
        for each pack so the user can tell what's inside before
        committing. Blocked mid-run like the Mode picker."""
        if self.running:
            pager_dialogs.popup_menu(
                self.pager, 'Stop current attack first',
                [('OK', 'ok')], bg_drawer=self._bg)
            return
        cur = self.config.get('wifi_attack_pack', SSID_PACKS[0][0])
        items = []
        initial = 0
        for idx, (key, title, desc) in enumerate(SSID_PACKS):
            label = f'{title} - {desc}'
            items.append((label[:54], key))
            if key == cur:
                initial = idx
        items.append(('Cancel', None))
        picked = pager_dialogs.popup_menu(
            self.pager, 'SSID Pack', items,
            bg_drawer=self._bg, initial_selected=initial)
        if picked:
            self.config['wifi_attack_pack'] = picked
            save_config(self.config)

    def _toggle_numbered(self):
        cur = bool(self.config.get('wifi_attack_numbered', True))
        self.config['wifi_attack_numbered'] = not cur
        save_config(self.config)

    def _toggle_dual_radio(self):
        if self.running:
            pager_dialogs.popup_menu(
                self.pager, 'Stop current attack first',
                [('OK', 'ok')], bg_drawer=self._bg)
            return
        cur = bool(self.config.get('wifi_attack_dual_radio', False))
        self.config['wifi_attack_dual_radio'] = not cur
        save_config(self.config)

    def _cycle_rotate(self):
        options = ssid_spam.ROTATE_OPTIONS
        try:
            cur = float(self.config.get('wifi_attack_rotate', 1.0))
        except (TypeError, ValueError):
            cur = 1.0
        try:
            i = options.index(cur)
        except ValueError:
            i = options.index(1.0) if 1.0 in options else 0
        self.config['wifi_attack_rotate'] = options[(i + 1) % len(options)]
        save_config(self.config)

    def _cycle_wps_method(self):
        cur = self.config.get('wifi_attack_wps_method', 'pixie')
        nxt = 'brute' if cur == 'pixie' else 'pixie'
        self.config['wifi_attack_wps_method'] = nxt
        save_config(self.config)

    # -- Rendering ---------------------------------------------------

    def _bg(self):
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)

    def _draw_cell(self, col, row, item):
        p = self.pager
        x = self._COL_X[col] if col < len(self._COL_X) else self._COL_X[-1]
        y = self._ROW_Y_START + row * self._ROW_H
        is_sel = (col == self.sel_col and row == self.sel_row)
        label_color = self._LABEL_SEL_C if is_sel else self._LABEL_UNSEL_C
        value_color = item.get('value_color', self._VALUE_DEFAULT_C)

        label_text = (item['label'] if item.get('no_colon')
                      else item['label'] + ':')
        p.draw_ttf(x, y, label_text, label_color, FONT_MENU, self._FONT_SIZE)
        lw = self._label_w_cache.get(label_text)
        if lw is None:
            lw = p.ttf_width(label_text, FONT_MENU, self._FONT_SIZE)
            self._label_w_cache[label_text] = lw
        value_str = str(item.get('value', ''))
        if value_str:
            p.draw_ttf(x + lw + self._LABEL_GAP, y, value_str, value_color,
                       FONT_MENU, self._FONT_SIZE)

    def _render(self, full=True):
        p = self.pager
        self._bg()
        items = self._items()
        self._items_cache = items
        self._clamp_selection(items)
        for col, row, item in items:
            self._draw_cell(col, row, item)
        if self.engine and _wifi_attacks_dashboard_cfg().get('status_bar'):
            for w in self.engine.widgets:
                try:
                    w.render(p, self.engine.renderer)
                except Exception:
                    pass
        # WIP footer on every mode — SSID Spam is hardware-limited,
        # everything else isn't implemented yet. Flag the whole
        # dashboard until the attack backends are all functional.
        fs = theme_utils.get_size('small')
        msg = '(WIP Under Development)'
        tw = p.ttf_width(msg, FONT_MENU, fs)
        p.draw_ttf((SCREEN_W - tw) // 2, SCREEN_H - fs - 22, msg,
                   self.ORANGE, FONT_MENU, fs)
        self._last_sel = (self.sel_col, self.sel_row)
        p.flip()

    def _render_selection_change(self, prev_sel, new_sel):
        items = getattr(self, '_items_cache', None)
        if not items:
            return self._render(full=True)
        p = self.pager
        for col, row in (prev_sel, new_sel):
            target = None
            for c, r, it in items:
                if c == col and r == row:
                    target = it
                    break
            if target is None:
                continue
            x = self._COL_X[col] if col < len(self._COL_X) else self._COL_X[-1]
            y = self._ROW_Y_START + row * self._ROW_H
            is_sel = (col == self.sel_col and row == self.sel_row)
            color = self._LABEL_SEL_C if is_sel else self._LABEL_UNSEL_C
            label_text = (target['label'] if target.get('no_colon')
                          else target['label'] + ':')
            p.draw_ttf(x, y, label_text, color, FONT_MENU, self._FONT_SIZE)
        self._last_sel = new_sel
        p.flip()

    def _clamp_selection(self, items):
        """Keep sel_col/sel_row in range when mode changes remove cells."""
        by_col = {0: 0, 1: 0}
        for c, r, _ in items:
            by_col[c] = max(by_col[c], r + 1)
        # If current column has no cells, switch to the one that does
        if by_col.get(self.sel_col, 0) == 0:
            self.sel_col = 1 if self.sel_col == 0 else 0
        max_rows = by_col.get(self.sel_col, 1)
        if self.sel_row >= max_rows:
            self.sel_row = max(0, max_rows - 1)

    # -- Input -------------------------------------------------------

    def _handle_button(self, btn):
        p = self.pager
        items = getattr(self, '_items_cache', None) or self._items()
        by_col = {0: 0, 1: 0}
        for c, r, _ in items:
            by_col[c] = max(by_col[c], r + 1)

        if btn & p.BTN_UP:
            self.sel_row = (self.sel_row - 1) % max(1, by_col.get(self.sel_col, 1))
        elif btn & p.BTN_DOWN:
            self.sel_row = (self.sel_row + 1) % max(1, by_col.get(self.sel_col, 1))
        elif btn & p.BTN_LEFT:
            if by_col.get(0, 0) > 0:
                self.sel_col = 0
                self.sel_row = min(self.sel_row, by_col[0] - 1)
        elif btn & p.BTN_RIGHT:
            if by_col.get(1, 0) > 0:
                self.sel_col = 1
                self.sel_row = min(self.sel_row, by_col[1] - 1)
        elif btn & p.BTN_A:
            cur = None
            for c, r, it in items:
                if c == self.sel_col and r == self.sel_row:
                    cur = it
                    break
            if cur:
                action = cur.get('action')
                if callable(action):
                    try:
                        action()
                    except Exception:
                        pass
        elif btn & p.BTN_B:
            return 'b'
        elif btn & p.BTN_POWER:
            return 'power'
        return None


_instance = None


def get_wifi_attacks():
    global _instance
    if _instance is None:
        _instance = WifiAttacksUI()
    return _instance
