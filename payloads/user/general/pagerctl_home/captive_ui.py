"""captive_ui.py - Captive portal subscreen for pagerctl_home.

Two-column dashboard where every visible value is also the control
to change it. Left column = live status (read-only or detail-on-click),
right column = settings (toggle/cycle/picker on click). D-pad
navigates the grid, A activates the selected cell, B exits.
"""

import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wardrive'))

from wardrive.config import (load_config, save_config, SCREEN_W, SCREEN_H,
                              FONT_TITLE, FONT_MENU)
from wardrive.web_server import wait_any_button, drain_virt_button
from wardrive import wifi_utils
from pagerctl import PAGER_EVENT_PRESS

from captive import server as cap_server
from captive import dns_hijack
from captive import ap_control
from captive import captures
from captive import intercept

import pager_dialogs
import theme_utils


_CAPTIVE_CFG = None


def _captive_dashboard_cfg():
    global _CAPTIVE_CFG
    if _CAPTIVE_CFG is None:
        td = os.environ.get('PAGERCTL_THEME',
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'themes', 'Circuitry'))
        try:
            with open(os.path.join(td, 'components/dashboards/captive_dashboard.json')) as f:
                _CAPTIVE_CFG = json.load(f)
        except Exception:
            _CAPTIVE_CFG = {}
    return _CAPTIVE_CFG


HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL_TEMPLATES_DIR = os.path.join(HERE, 'captive', 'templates', 'portal')


# Curated list of common public/captive SSIDs to impersonate, each
# paired with the portal template that matches the brand's typical
# captive portal styling. Templates live under
# captive/templates/portal/<name>/index.html — any unknown value
# falls back to 'default'.
COMMON_SSIDS = [
    ('Free WiFi',            'default'),
    ('Public WiFi',          'default'),
    ('Guest WiFi',           'default'),
    ('Starbucks WiFi',       'starbucks'),
    ('Google Starbucks',     'starbucks'),
    ('McDonalds Free WiFi',  'mcdonalds'),
    ('attwifi',              'attwifi'),
    ('xfinitywifi',          'xfinity'),
    ('Boingo Hotspot',       'airport'),
    ('Hotel WiFi',           'hotel'),
    ('Airport WiFi',         'airport'),
    ('Linksys',              'default'),
    ('NETGEAR',              'default'),
    ('Library WiFi',         'default'),
    ('CoffeeShop',           'starbucks'),
    ('Tesla Service',        'default'),
]


class CaptiveUI:

    def __init__(self):
        self.config = load_config()
        self.pager = None
        self.bg_handle = None
        self.is_foreground = False
        self.running = False
        self.sel_col = 0
        self.sel_row = 0
        self._client_count_cached = 0
        self._client_count_at = 0.0
        self._last_sel = None
        # {label_text: width_px} — populated lazily by _draw_cell so
        # we don't pay the ttf_width cost for static labels every
        # frame. Labels never change so the cached width stays valid
        # for the lifetime of the panel.
        self._label_w_cache = {}

    def run(self, pager, engine=None):
        self.pager = pager
        self.engine = engine
        self.is_foreground = True
        self.config = load_config()  # refresh
        self.running = cap_server.is_running()

        # Resume captive in the background if the AP is still up
        # from a previous session. The work involves several
        # subprocess calls (uci, nft, dnsmasq) that together can
        # block the main thread for 1–3 seconds, which makes the
        # panel feel frozen on entry. Do it on a worker thread so
        # the grid renders and accepts input immediately; the State
        # cell will flip to RUNNING when the worker finishes.
        if not self.running and not getattr(self, '_resuming', False):
            self._resuming = True
            import threading
            threading.Thread(target=self._resume_worker, daemon=True).start()

        # Colors
        self.WHITE = theme_utils.get_color(pager, 'white')
        self.GREEN = theme_utils.get_color(pager, 'green')
        self.RED = theme_utils.get_color(pager, 'red')
        self.YELLOW = theme_utils.get_color(pager, 'warning_accent')
        self.CYAN = theme_utils.get_color(pager, 'info_accent')
        self.DIM = theme_utils.get_color(pager, 'dim')
        self.ORANGE = theme_utils.get_color(pager, 'orange')
        self.BG_SEL = theme_utils.get_color(pager, 'selection_bg')

        # Pull grid / text / bg-sample from captive_dashboard.json
        self._apply_dashboard_cfg()

        if not self.bg_handle:
            bg = os.path.join(HERE, 'themes', 'Circuitry', 'assets',
                              'alert_dialog_bg_term_blue.png')
            if os.path.isfile(bg):
                self.bg_handle = pager.load_image(bg)

        pager.clear_input_events()

        last_full_render = 0
        last_counter_refresh = 0
        try:
            while True:
                now = time.time()
                # Initial full render (one-time on entry). Subsequent
                # full renders only happen if forced by a config-
                # changing action.
                if last_full_render == 0:
                    self._render(full=True)
                    last_full_render = now
                    last_counter_refresh = now
                # Periodic counter refresh: full redraw. We tried
                # fill_rect-erase + redraw of just the value text,
                # but no bg-color hardcode matches the image cleanly,
                # leaving visible rectangles around the counters.
                # A full render every 2s is fine — selection change
                # is already incremental so navigation stays fast.
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
                    prev_sel = (self.sel_col, self.sel_row)
                    handled = self._handle_button(btn)
                    if handled is not None:
                        exit_result = handled
                        break
                    new_sel = (self.sel_col, self.sel_row)
                    if new_sel != prev_sel:
                        # Selection-only change → incremental redraw
                        # of the old + new cells. ~10× faster than a
                        # full grid redraw.
                        self._render_selection_change(prev_sel, new_sel)
                    else:
                        # Action fired (toggle/edit) — config likely
                        # changed; force a full redraw next tick.
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
                        'power': pager.BTN_POWER,
                    }
                    handled = self._handle_button(btn_map.get(v, 0))
                    if handled == 'power':
                        self.is_foreground = False
                        return 'power'
                    if handled == 'b':
                        self.is_foreground = False
                        return None
                    last_render = 0

                time.sleep(0.03)
        finally:
            self.is_foreground = False

    # -- Item grid ----------------------------------------------------

    def _items(self):
        """Build the 2-column item grid based on current state.

        Returns a list of (col, row, item_dict) tuples. item_dict has:
          label, value (string for display), type, action (callable)
        """
        c = self.config
        ssid = c.get('captive_ssid', '') or '(unset)'
        ap_open = c.get('captive_ap_open', True)
        password = c.get('captive_password', '')
        portal = c.get('captive_portal_enabled', True)
        spoof = c.get('captive_spoof_enabled', False)
        mitm = c.get('captive_mitm_enabled', False)
        banner = c.get('captive_mirror_banner', True)
        internet = c.get('captive_internet_allowed', True)
        tmpl = c.get('captive_portal_template', 'default')

        state_label = 'RUNNING' if self.running else 'STOPPED'
        state_color = self.GREEN if self.running else self.RED

        clients = self._client_count()
        creds = captures.count()
        try:
            from captive import mirror
            cached = mirror.cache_count()
        except Exception:
            cached = 0

        # Left column = live status / counters then bare action buttons
        left = [
            {'label': 'State',       'value': state_label,    'value_color': state_color,
             'type': 'action',       'action': self._toggle_attack},
            {'label': 'Internet',    'value': 'ON' if internet else 'OFF',
             'value_color': self.GREEN if internet else self.RED,
             'type': 'toggle',       'action': self._toggle_internet},
            {'label': 'Clients',     'value': str(clients),   'value_color': self.CYAN,
             'type': 'readonly'},
            {'label': 'Creds',       'value': str(creds),     'value_color': self.YELLOW,
             'type': 'action',       'action': self._view_captures},
            {'label': 'Cache',       'value': str(cached),    'value_color': self.ORANGE,
             'type': 'action',       'action': self._view_cache},
            {'label': 'Clear Creds', 'value': '',             'value_color': self.RED,
             'type': 'action',       'action': self._clear_creds, 'no_colon': True},
            {'label': 'Clear Cache', 'value': '',             'value_color': self.RED,
             'type': 'action',       'action': self._clear_cache, 'no_colon': True},
        ]

        # Right column = configurable settings
        right = [
            {'label': 'SSID',     'value': ssid[:14],
             'value_color': self.WHITE,
             'type': 'picker',  'action': self._pick_ssid_menu},
            {'label': 'AP',       'value': 'Open' if ap_open else 'WPA',
             'value_color': self.WHITE,
             'type': 'cycle',   'action': self._toggle_ap_mode},
            {'label': 'Pass',     'value': '****' if password else '(open)',
             'value_color': self.DIM if not password else self.WHITE,
             'type': 'edit',    'action': self._edit_password},
            {'label': 'Captive Portal', 'value': 'ON' if portal else 'OFF',
             'value_color': self.GREEN if portal else self.DIM,
             'type': 'toggle',  'action': self._toggle_portal},
            {'label': 'Spoof (R/O)', 'value': 'ON' if spoof else 'OFF',
             'value_color': self.GREEN if spoof else self.DIM,
             'type': 'toggle',  'action': self._toggle_spoof},
            {'label': 'MITM (R/W)',  'value': 'ON' if mitm else 'OFF',
             'value_color': self.GREEN if mitm else self.DIM,
             'type': 'toggle',  'action': self._toggle_mitm},
            {'label': 'POC Banner',  'value': 'ON' if banner else 'OFF',
             'value_color': self.GREEN if banner else self.DIM,
             'type': 'toggle',  'action': self._toggle_banner},
        ]

        grid = []
        for r, item in enumerate(left):
            grid.append((0, r, item))
        for r, item in enumerate(right):
            grid.append((1, r, item))
        return grid

    # -- Render -------------------------------------------------------

    # Default layout constants — overridden per-instance from
    # components/dashboards/captive_dashboard.json in __init__ so a
    # theme can move/resize the grid without any Python edits.
    _COL_WIDTHS = (200, 240)
    _COL_X = (25, 240)
    _ROW_Y_START = 30
    _ROW_H = 24
    _FONT_SIZE = 17
    _LABEL_GAP = 6

    def _apply_dashboard_cfg(self):
        """Pull grid + text config from captive_dashboard.json and
        store on self. Preserves the class-level defaults when the
        JSON is missing or truncated."""
        cfg = _captive_dashboard_cfg()
        grid = cfg.get('grid') or {}
        text = cfg.get('text') or {}
        bg = cfg.get('bg_sample') or {}

        col_x = grid.get('col_x')
        if isinstance(col_x, list) and len(col_x) >= 1:
            self._COL_X = tuple(int(v) for v in col_x)
        col_w = grid.get('col_widths')
        if isinstance(col_w, list) and len(col_w) >= 1:
            self._COL_WIDTHS = tuple(int(v) for v in col_w)
        self._ROW_Y_START = int(grid.get('row_y_start', self._ROW_Y_START))
        self._ROW_H = int(grid.get('row_height', self._ROW_H))
        self._LABEL_GAP = int(grid.get('label_gap', self._LABEL_GAP))
        self._COUNTER_ERASE_W = int(grid.get('counter_erase_width', 80))

        fs = text.get('font_size', self._FONT_SIZE)
        if isinstance(fs, (int, float)):
            self._FONT_SIZE = int(fs)
        else:
            self._FONT_SIZE = theme_utils.get_size(fs or 'medium')

        p = self.pager
        self._LABEL_SEL_C = theme_utils.get_color(
            p, text.get('label_selected', 'warning_accent'))
        self._LABEL_UNSEL_C = theme_utils.get_color(
            p, text.get('label_unselected', 'dim'))
        self._VALUE_DEFAULT_C = theme_utils.get_color(
            p, text.get('value_default', 'white'))

        self._BG_FILL = (int(bg.get('r', 15)),
                         int(bg.get('g', 28)),
                         int(bg.get('b', 56)))

    def _render(self, full=True):
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)

        items = self._items()
        self._items_cache = items

        for col, row, item in items:
            self._draw_cell(col, row, item)

        # Status-bar widgets are opt-in via the component JSON's
        # `status_bar` key (same convention the engine uses for every
        # other screen). Omit or set to null in captive_dashboard.json
        # to hide the battery/clock overlay.
        if self.engine and _captive_dashboard_cfg().get('status_bar'):
            for w in self.engine.widgets:
                try:
                    w.render(p, self.engine.renderer)
                except Exception:
                    pass

        self._last_sel = (self.sel_col, self.sel_row)
        p.flip()

    # Default bg-sample used if captive_dashboard.json doesn't supply
    # one. Overwritten per-instance by _apply_dashboard_cfg.
    _BG_FILL = (15, 28, 56)
    _COUNTER_ERASE_W = 80

    def _draw_cell(self, col, row, item):
        """Draw a single grid cell. Selection is indicated by label
        color (yellow vs dim) — no background highlight rect — so a
        selection change can be repainted by simply re-drawing the
        two affected cells' labels in the new color (TTF overdraw
        cleanly replaces the same glyph pixels)."""
        p = self.pager
        x = self._COL_X[col]
        y = self._ROW_Y_START + row * self._ROW_H
        is_sel = (col == self.sel_col and row == self.sel_row)
        label_color = self.YELLOW if is_sel else self.DIM
        value_color = item.get('value_color', self.WHITE)

        label_text = item['label'] if item.get('no_colon') else item['label'] + ':'
        p.draw_ttf(x, y, label_text, label_color, FONT_MENU, self._FONT_SIZE)
        lw = self._label_w_cache.get(label_text)
        if lw is None:
            lw = p.ttf_width(label_text, FONT_MENU, self._FONT_SIZE)
            self._label_w_cache[label_text] = lw
        value_str = str(item.get('value', ''))
        if value_str:
            p.draw_ttf(x + lw + self._LABEL_GAP, y, value_str, value_color,
                       FONT_MENU, self._FONT_SIZE)

    def _refresh_counters(self):
        """Repaint just the dynamic counter values (Clients, Creds,
        Cache) without redrawing the entire grid. Erases the value
        area with a bg-matching fill and draws the new value text."""
        items = getattr(self, '_items_cache', None)
        if not items:
            return
        p = self.pager
        bg_color = p.rgb(*self._BG_FILL)
        try:
            from captive import mirror
            cached = mirror.cache_count()
        except Exception:
            cached = 0
        new_values = {
            'Clients:': str(self._client_count()),
            'Creds:':   str(captures.count()),
            'Cache:':   str(cached),
        }
        for col, row, item in items:
            label_text = (item['label'] if item.get('no_colon')
                           else item['label'] + ':')
            new_val = new_values.get(label_text)
            if new_val is None:
                continue
            x = self._COL_X[col]
            y = self._ROW_Y_START + row * self._ROW_H
            lw = self._label_w_cache.get(label_text)
            if lw is None:
                lw = p.ttf_width(label_text, FONT_MENU, self._FONT_SIZE)
                self._label_w_cache[label_text] = lw
            value_x = x + lw + self._LABEL_GAP
            # Erase the old value area (small rect just covering the
            # number) then draw the new value.
            p.fill_rect(value_x, y - 2, self._COUNTER_ERASE_W, self._ROW_H - 2, bg_color)
            value_color = item.get('value_color', self.WHITE)
            p.draw_ttf(value_x, y, new_val, value_color,
                       FONT_MENU, self._FONT_SIZE)
            item['value'] = new_val
        p.flip()

    def _render_selection_change(self, prev_sel, new_sel):
        """Repaint just the two label cells affected by a D-pad
        move. Selection is encoded in label color, so re-drawing
        the same label text at the same position in a new color
        cleanly overwrites the old glyph pixels with no need to
        erase. ~2 TTF calls instead of 22+ for a full grid."""
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
            x = self._COL_X[col]
            y = self._ROW_Y_START + row * self._ROW_H
            is_sel = (col == self.sel_col and row == self.sel_row)
            color = self.YELLOW if is_sel else self.DIM
            label_text = (target['label'] if target.get('no_colon')
                          else target['label'] + ':')
            p.draw_ttf(x, y, label_text, color, FONT_MENU, self._FONT_SIZE)
        self._last_sel = new_sel
        p.flip()

    # -- Input handling -----------------------------------------------

    def _handle_button(self, btn):
        """Returns 'b' or 'power' to exit the screen, None otherwise."""
        p = self.pager
        # Reuse items list cached by the most recent render to avoid
        # rebuilding (with its file-enum and config dict work) on
        # every single button press.
        items = getattr(self, '_items_cache', None) or self._items()
        cols = {0: [], 1: []}
        for col, row, item in items:
            cols[col].append((row, item))

        if btn & p.BTN_POWER:
            return 'power'
        if btn & p.BTN_B:
            return 'b'

        if btn & p.BTN_UP:
            n = len(cols[self.sel_col])
            if n:
                self.sel_row = (self.sel_row - 1) % n
        elif btn & p.BTN_DOWN:
            n = len(cols[self.sel_col])
            if n:
                self.sel_row = (self.sel_row + 1) % n
        elif btn & p.BTN_LEFT:
            self.sel_col = 0
            n = len(cols[self.sel_col])
            if n and self.sel_row >= n:
                self.sel_row = n - 1
        elif btn & p.BTN_RIGHT:
            self.sel_col = 1
            n = len(cols[self.sel_col])
            if n and self.sel_row >= n:
                self.sel_row = n - 1
        elif btn & p.BTN_A:
            # Find selected item and activate
            for col, row, item in items:
                if col == self.sel_col and row == self.sel_row:
                    action = item.get('action')
                    if action and item.get('type') != 'readonly':
                        action()
                    break
        return None

    # -- Item actions -------------------------------------------------

    def _bg(self):
        if self.bg_handle:
            self.pager.draw_image(0, 0, self.bg_handle)
        else:
            self.pager.clear(0)

    def _toggle_attack(self):
        if self.running:
            ok = pager_dialogs.popup_menu(
                self.pager, 'Stop captive portal?',
                [('No, keep running', False), ('YES, stop', True)],
                bg_drawer=self._bg)
            if ok:
                self._do_stop()
        else:
            ok = pager_dialogs.popup_menu(
                self.pager, 'Start captive portal?',
                [('No, cancel', False), ('YES, start', True)],
                bg_drawer=self._bg)
            if ok:
                self._do_start()

    def _toggle_portal(self):
        self.config['captive_portal_enabled'] = \
            not self.config.get('captive_portal_enabled', True)
        save_config(self.config)
        self._refresh_server_state()

    def _toggle_spoof(self):
        self.config['captive_spoof_enabled'] = \
            not self.config.get('captive_spoof_enabled', False)
        save_config(self.config)
        self._refresh_server_state()

    def _toggle_mitm(self):
        self.config['captive_mitm_enabled'] = \
            not self.config.get('captive_mitm_enabled', False)
        save_config(self.config)
        self._refresh_server_state()

    def _toggle_banner(self):
        self.config['captive_mirror_banner'] = \
            not self.config.get('captive_mirror_banner', True)
        save_config(self.config)
        self._refresh_server_state()

    def _toggle_internet(self):
        new = not self.config.get('captive_internet_allowed', True)
        # Confirm before killing internet — affects all br-lan
        # clients including the user's own SSH/web access.
        if not new:
            ok = pager_dialogs.popup_menu(
                self.pager, 'Block real internet for clients?',
                [('No, cancel', False), ('YES, block', True)],
                bg_drawer=self._bg)
            if not ok:
                return
        self.config['captive_internet_allowed'] = new
        save_config(self.config)
        try:
            intercept.set_internet(new)
        except Exception:
            pass

    def _clear_cache(self):
        try:
            from captive import mirror
            count = mirror.cache_count()
        except Exception:
            count = 0
        if count == 0:
            self._flash('Cache empty', 0.8)
            return
        ok = pager_dialogs.popup_menu(
            self.pager, f'Clear {count} cached site(s)?',
            [('No, cancel', False), ('YES, clear', True)],
            bg_drawer=self._bg)
        if not ok:
            return
        try:
            from captive import mirror
            removed = mirror.clear_cache()
            self._flash(f'Cleared {removed}', 0.8)
        except Exception:
            self._flash('Clear failed', 1.0)

    def _clear_creds(self):
        n = captures.count()
        if n == 0:
            self._flash('No creds yet', 0.8)
            return
        ok = pager_dialogs.popup_menu(
            self.pager, f'Clear {n} captured cred(s)?',
            [('No, cancel', False), ('YES, clear', True)],
            bg_drawer=self._bg)
        if not ok:
            return
        removed = captures.clear_all()
        self._flash(f'Cleared {removed}', 0.8)

    def _toggle_ap_mode(self):
        self.config['captive_ap_open'] = \
            not self.config.get('captive_ap_open', True)
        save_config(self.config)

    def _edit_password(self):
        cur = self.config.get('captive_password', '')
        new = pager_dialogs.edit_string(
            self.pager, 'Password (blank=open)', cur, secret=True,
            max_length=63, bg_drawer=self._bg)
        if new is None:
            return
        self.config['captive_password'] = new
        self.config['captive_ap_open'] = (len(new) == 0)
        save_config(self.config)

    def _pick_ssid_menu(self):
        """Top-level SSID picker — choose how to set the SSID."""
        items = [
            ('Type custom', 'type'),
            ('Pick common', 'common'),
            ('Clone nearby', 'clone'),
            ('Portal template', 'tmpl'),
            ('Cancel', None),
        ]
        choice = pager_dialogs.popup_menu(
            self.pager, 'Set SSID / Template', items, bg_drawer=self._bg)
        if choice == 'type':
            cur = self.config.get('captive_ssid', '')
            new = pager_dialogs.edit_string(
                self.pager, 'SSID', cur, secret=False, max_length=32,
                bg_drawer=self._bg)
            if new is not None:
                self.config['captive_ssid'] = new
                save_config(self.config)
        elif choice == 'common':
            # Show each common SSID with its mapped template in the
            # label so the user can see what they're picking.
            sub = [(f'{ssid}  [{tmpl}]', (ssid, tmpl))
                   for ssid, tmpl in COMMON_SSIDS] + [('Cancel', None)]
            picked = pager_dialogs.popup_menu(
                self.pager, 'Common SSIDs', sub, bg_drawer=self._bg)
            if picked:
                ssid, tmpl = picked
                self.config['captive_ssid'] = ssid
                self.config['captive_portal_template'] = tmpl
                save_config(self.config)
                self._refresh_server_state()
                self._flash(f'Set {ssid}  [{tmpl}]', 0.8)
        elif choice == 'clone':
            self._clone_nearby_ssid()
        elif choice == 'tmpl':
            self._pick_portal_template()

    def _pick_portal_template(self):
        """Standalone portal template picker, reads the list of
        template directories at captive/templates/portal/ so
        user-uploaded templates show up automatically."""
        tmpl_dir = os.path.join(HERE, 'captive', 'templates', 'portal')
        try:
            names = sorted(n for n in os.listdir(tmpl_dir)
                           if os.path.isdir(os.path.join(tmpl_dir, n)))
        except Exception:
            names = ['default']
        if not names:
            self._flash('no templates found', 1.0)
            return
        current = self.config.get('captive_portal_template', 'default')
        items = [(n, n) for n in names] + [('Cancel', None)]
        try:
            initial = names.index(current)
        except ValueError:
            initial = 0
        picked = pager_dialogs.popup_menu(
            self.pager, 'Portal Template', items,
            bg_drawer=self._bg, initial_selected=initial)
        if picked:
            self.config['captive_portal_template'] = picked
            save_config(self.config)
            self._refresh_server_state()
            self._flash(f'Template: {picked}', 0.8)

    def _clone_nearby_ssid(self):
        self._flash('Scanning...', 0.4)
        try:
            networks = wifi_utils.scan_networks()
        except Exception:
            networks = []
        if not networks:
            self._flash('No networks found')
            return
        items = []
        for n in networks[:20]:
            ssid = n.get('ssid', '')
            sig = n.get('signal', -100)
            enc = n.get('enc', 'open')
            label = f'{ssid[:18]} {sig}dBm {enc}'
            items.append((label, n))
        items.append(('Cancel', None))
        choice = pager_dialogs.popup_menu(
            self.pager, 'Clone Nearby', items, bg_drawer=self._bg)
        if choice:
            self.config['captive_ssid'] = choice.get('ssid', '')
            self.config['captive_ap_open'] = (choice.get('enc') == 'open')
            save_config(self.config)

    def _flash(self, msg, duration=0.8):
        p = self.pager
        self._bg()
        fs = 18
        tw = p.ttf_width(msg, FONT_MENU, fs)
        box_w = max(tw + 40, 240)
        box_h = 50
        bx = (SCREEN_W - box_w) // 2
        by = (SCREEN_H - box_h) // 2
        p.fill_rect(bx, by, box_w, box_h, theme_utils.get_color(p, 'modal_bg'))
        edge = self.CYAN
        p.fill_rect(bx, by, box_w, 1, edge)
        p.fill_rect(bx, by + box_h - 1, box_w, 1, edge)
        p.fill_rect(bx, by, 1, box_h, edge)
        p.fill_rect(bx + box_w - 1, by, 1, box_h, edge)
        p.draw_ttf(bx + (box_w - tw) // 2, by + (box_h - fs) // 2, msg,
                   self.WHITE, FONT_MENU, fs)
        p.flip()
        time.sleep(duration)
        p.clear_input_events()

    # -- Lifecycle ----------------------------------------------------

    def _client_count(self):
        """Count WiFi clients associated to the captive portal.

        Cached for 3 seconds so the per-render call doesn't fork a
        subprocess every tick. Returns 0 unless captive is running —
        otherwise the count would reflect leftover hotspot clients.
        """
        if not self.running:
            return 0
        now = time.time()
        if now - self._client_count_at < 3.0:
            return self._client_count_cached
        import subprocess
        count = 0
        for iface in ('wlan0mgmt', 'wlan0wpa'):
            try:
                r = subprocess.run(['iw', 'dev', iface, 'station', 'dump'],
                                    capture_output=True, text=True, timeout=2)
            except Exception:
                continue
            if r.returncode != 0:
                continue
            count = sum(1 for line in r.stdout.split('\n')
                        if line.startswith('Station '))
            break
        self._client_count_cached = count
        self._client_count_at = now
        return count

    def _do_start(self):
        # Mutual exclusion: offer to stop the conflicting mode rather
        # than just bailing out with an error.
        mode = wifi_utils.get_active_wifi_mode()
        if mode == 'hotspot':
            ok = pager_dialogs.popup_menu(
                self.pager, 'Hotspot is on. Disable it?',
                [('No, cancel', False), ('YES, disable', True)],
                bg_drawer=self._bg)
            if not ok:
                return
            wifi_utils.set_hotspot(False)
        elif mode == 'pineap':
            ok = pager_dialogs.popup_menu(
                self.pager, 'PineAP is on. Disable it?',
                [('No, cancel', False), ('YES, disable', True)],
                bg_drawer=self._bg)
            if not ok:
                return
            wifi_utils.set_pineap_capture(False)
        elif mode == 'wifi_attacks':
            ok = pager_dialogs.popup_menu(
                self.pager, 'WiFi Attacks is on. Stop it?',
                [('No, cancel', False), ('YES, stop', True)],
                bg_drawer=self._bg)
            if not ok:
                return
            try:
                from attacks import ssid_spam as _ss
                _ss.stop()
            except Exception:
                pass

        # Defensive wardrive check — only conflict if wardrive uses
        # the same radio (radio0). Default config uses wlan1mon (phy1)
        # which is independent.
        try:
            iface = self.config.get('capture_interface', 'wlan1mon')
            if iface.startswith('wlan0'):
                self._flash('Wardrive on wlan0 - change iface', 2.0)
                return
        except Exception:
            pass

        ssid = self.config.get('captive_ssid', '')
        if not ssid:
            self._flash('Set SSID first', 1.2)
            return
        ap_open = self.config.get('captive_ap_open', True)
        password = self.config.get('captive_password', '')
        if not ap_open and (not password or len(password) < 8):
            self._flash('Set 8+ char password', 1.5)
            return

        ok = ap_control.start_ap(ssid, open_ap=ap_open, password=password)
        if not ok:
            self._flash('AP start failed', 1.5)
            return
        intercept.enable()        # nft: redirect port 80 to us
        intercept.set_internet(self.config.get('captive_internet_allowed', True))
        # DNS hijack is only needed when Spoof or MITM is on — those
        # modes have to trap the victim on the pager when they type a
        # real URL. Pure Portal mode must NOT hijack DNS, otherwise
        # passed clients can't resolve real domains and lose internet.
        self._sync_dns_hijack()
        cap_server.start(port=80)
        self._refresh_server_state()
        self.running = True
        self._flash('Attack running', 0.8)

    def _resume_worker(self):
        """Background-thread helper that does the heavy startup
        work for auto-resume. Runs off the main loop so the panel
        stays responsive on entry."""
        try:
            try:
                hotspot_up, hs_ssid, _ = wifi_utils.get_hotspot_state()
            except Exception:
                hotspot_up, hs_ssid = False, ''
            marker = ap_control.marker_exists()
            if not (marker or hotspot_up):
                return
            if not marker and hotspot_up:
                ap_control._write_marker(hs_ssid or 'captive', True)
            cap_server.start(port=80)
            intercept.enable()
            intercept.set_internet(
                self.config.get('captive_internet_allowed', True))
            self._sync_dns_hijack()  # only restarts dnsmasq if spoof/mitm on
            self.running = True
            # Push the saved Portal/Spoof/MITM toggles into the freshly
            # started server. Without this the server's in-memory state
            # keeps its defaults (everything but Portal off) even though
            # the panel reads "ON" from config — so MITM/Spoof silently
            # don't fire on requests after a UI restart.
            self._refresh_server_state()
        except Exception:
            pass
        finally:
            self._resuming = False

    def _do_stop(self):
        cap_server.stop()
        intercept.disable()
        dns_hijack.disable()
        ap_control.stop_ap()
        self.running = False
        self._flash('Attack stopped', 0.8)

    def _sync_dns_hijack(self):
        """Enable DNS hijack only when the explicit `captive_dns_hijack`
        config flag is on. Spoof and MITM do NOT auto-enable it because
        the port-80 nft redirect already catches HTTP regardless of
        destination IP — the Host header is enough to drive the spoof
        library lookup. Hijacking DNS only adds value if you want to
        also catch direct-IP traffic, but it breaks iOS connectivity
        probes (the OS marks the network as 'no internet') and isn't
        worth the trade-off by default.
        """
        want = self.config.get('captive_dns_hijack', False)
        try:
            if want and not dns_hijack.is_active():
                dns_hijack.enable()
            elif not want and dns_hijack.is_active():
                dns_hijack.disable()
        except Exception:
            pass

    def _refresh_server_state(self):
        cap_server.update_state(
            portal_enabled=self.config.get('captive_portal_enabled', True),
            spoof_enabled=self.config.get('captive_spoof_enabled', False),
            mitm_enabled=self.config.get('captive_mitm_enabled', False),
            mirror_banner=self.config.get('captive_mirror_banner', True),
            portal_template=self.config.get('captive_portal_template', 'default'),
        )
        if self.running:
            self._sync_dns_hijack()

    # -- Captures viewer ----------------------------------------------

    def _view_captures(self):
        recent = captures.list_recent(20)
        if not recent:
            self._flash('No captures yet', 0.8)
            return
        idx = 0
        while True:
            self._draw_capture(recent[idx], idx + 1, len(recent))
            btn = wait_any_button(self.pager)
            if btn & self.pager.BTN_UP:
                idx = (idx - 1) % len(recent)
            elif btn & self.pager.BTN_DOWN:
                idx = (idx + 1) % len(recent)
            elif btn & self.pager.BTN_B:
                return

    # -- Cached sites viewer -----------------------------------------

    def _view_cache(self):
        from captive import mirror
        entries = mirror.list_cached_hosts()
        if not entries:
            self._flash('Cache empty', 0.8)
            return
        idx = 0
        while True:
            self._draw_cache_entry(entries[idx], idx + 1, len(entries))
            btn = wait_any_button(self.pager)
            if btn & self.pager.BTN_UP:
                idx = (idx - 1) % len(entries)
            elif btn & self.pager.BTN_DOWN:
                idx = (idx + 1) % len(entries)
            elif btn & self.pager.BTN_A:
                # A on a cached entry = delete it (with confirm)
                host = entries[idx][0]
                ok = pager_dialogs.popup_menu(
                    self.pager, f'Delete {host}?',
                    [('No, cancel', False), ('YES, delete', True)],
                    bg_drawer=self._bg)
                if ok:
                    mirror.remove_host(host)
                    entries = mirror.list_cached_hosts()
                    if not entries:
                        self._flash('Cache empty', 0.8)
                        return
                    if idx >= len(entries):
                        idx = len(entries) - 1
            elif btn & self.pager.BTN_B:
                return

    def _draw_cache_entry(self, entry, n, total):
        host, asset_count, total_bytes = entry
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)
        title = f'Cached {n}/{total}'
        tw = p.ttf_width(title, FONT_TITLE, 22)
        p.draw_ttf((SCREEN_W - tw) // 2, 12, title, self.CYAN, FONT_TITLE, 22)
        p.draw_ttf(20, 56, host, self.WHITE, FONT_MENU, 16)
        if total_bytes >= 1024:
            size_str = f'{total_bytes / 1024:.1f} KB'
        else:
            size_str = f'{total_bytes} B'
        p.draw_ttf(20, 86, f'files: {asset_count}',
                    self.YELLOW, FONT_MENU, 14)
        p.draw_ttf(20, 106, f'size:  {size_str}',
                    self.YELLOW, FONT_MENU, 14)
        p.draw_ttf(20, 140, 'A=delete   up/down=nav   B=back',
                    self.DIM, FONT_MENU, 12)
        p.flip()

    def _draw_capture(self, cap, n, total):
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)
        title = f'Credential {n}/{total}'
        tw = p.ttf_width(title, FONT_TITLE, 22)
        p.draw_ttf((SCREEN_W - tw) // 2, 12, title, self.CYAN, FONT_TITLE, 22)
        host = cap.get('host', '?')
        src = cap.get('source', '?')
        p.draw_ttf(20, 44, f'host: {host}', self.WHITE, FONT_MENU, 14)
        p.draw_ttf(20, 62, f'src: {src}', self.DIM, FONT_MENU, 14)
        y = 84
        for k, v in (cap.get('fields') or {}).items():
            line = f'{k}: {v}'[:48]
            p.draw_ttf(20, y, line, self.YELLOW, FONT_MENU, 14)
            y += 16
            if y > 190:
                break
        p.flip()


_instance = None


def get_captive():
    global _instance
    if _instance is None:
        _instance = CaptiveUI()
    return _instance
