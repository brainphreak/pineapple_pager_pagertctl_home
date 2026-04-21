"""wardrive_ui.py - Integrated wardrive dashboard for pagerctl_home.

Takes over rendering while active, returns control on exit.
Zero overhead when not displayed. Scan keeps running in background.
"""

import os
import sys
import time
import json
import queue
import threading
import glob

from pagerctl import PAGER_EVENT_PRESS

# Add wardrive modules to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wardrive'))

from wardrive.config import (load_config, save_config, ensure_dirs, DB_PATH,
                              EXPORT_DIR, CHANNELS_2_4, CHANNELS_5, CHANNELS_6,
                              SCREEN_W, SCREEN_H, FONT_TITLE, FONT_MENU)
from wardrive.database import Database
from wardrive.scanner import Scanner, PassiveScanner
from wardrive.gps_module import GpsReader, GpsState
from wardrive.wigle_export import WigleWriter
from wardrive.web_server import start_web_ui, stop_web_ui, wait_any_button, drain_virt_button

import theme_utils


_SUBMENU_CFG = None
_MSGBOX_CFG = None


def _wardrive_submenu_cfg():
    global _SUBMENU_CFG
    if _SUBMENU_CFG is None:
        td = os.environ.get('PAGERCTL_THEME',
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'themes', 'Circuitry'))
        try:
            with open(os.path.join(td, 'components/dialogs/wardrive_submenu.json')) as f:
                _SUBMENU_CFG = json.load(f)
        except Exception:
            _SUBMENU_CFG = {}
    return _SUBMENU_CFG


def _wardrive_msgbox_cfg():
    global _MSGBOX_CFG
    if _MSGBOX_CFG is None:
        td = os.environ.get('PAGERCTL_THEME',
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'themes', 'Circuitry'))
        try:
            with open(os.path.join(td, 'components/dialogs/wardrive_message_box.json')) as f:
                _MSGBOX_CFG = json.load(f)
        except Exception:
            _MSGBOX_CFG = {}
    return _MSGBOX_CFG


def _size_or_px(v, default='medium'):
    if isinstance(v, (int, float)):
        return int(v)
    return theme_utils.get_size(v or default)


class WardriveUI:
    """Wardrive dashboard with integrated scan + settings menu.

    Workflow:
    - Enter: dashboard shows immediately with current stats
    - A button: unified menu (scan control + settings)
    - B button: exit to home (scan keeps running in background)
    - Re-enter: dashboard shows current stats, no prompts
    """

    LAYOUT_REL = 'components/dashboards/wardrive_dashboard.json'

    def __init__(self):
        self.config = load_config()
        ensure_dirs()

        self.db = Database(DB_PATH)
        self.gps_state = GpsState()
        self.stop_event = threading.Event()
        self.scan_queue = queue.Queue()

        self.scanner = None
        self.gps_reader = None

        self.scan_state = 'stopped'
        self.start_time = time.time()
        self.paused_elapsed = 0
        self.current_channel = 0
        self.wigle_writer = WigleWriter(EXPORT_DIR)
        self.is_foreground = False  # set True while run() is on screen

        # Dedicated background worker thread that consumes the scan
        # queue and does the heavy DB/wigle work, so the main loop
        # never blocks on wardrive processing.
        self._bg_worker = None
        self._bg_stop = threading.Event()
        self._bg_new_aps = 0      # incremented by worker, drained by main loop
        self._bg_lock = threading.Lock()

        # Theme-driven dashboard layout (reloaded each run() so JSON
        # edits take effect on re-entry)
        self._layout = None
        self._layout_path = None
        self._layout_mtime = 0

        # Web UI is managed by wardrive.web_server module-level singleton
        # so settings toggle and wardrive share the same instance.
        if self.config.get('web_server', False):
            start_web_ui(port=self.config.get('web_port', 1337))

        self.bg_handle = None
        self.pager = None

    def run(self, pager):
        """Show wardrive dashboard. Returns when user presses B."""
        self.pager = pager
        self.is_foreground = True

        # Load/reload the theme-driven dashboard layout
        self._load_layout()

        # Fallback colors for the menu/popup code that still uses them
        self.WHITE = theme_utils.get_color(pager, 'white')
        self.GREEN = theme_utils.get_color(pager, 'green')
        self.CYAN = theme_utils.get_color(pager, 'info_accent')
        self.YELLOW = theme_utils.get_color(pager, 'warning_accent')
        self.RED = theme_utils.get_color(pager, 'red')
        self.DIM = theme_utils.get_color(pager, 'dim')
        self.ORANGE = theme_utils.get_color(pager, 'orange')

        # Load background from the layout's bg_image path (theme-relative)
        self.bg_handle = None
        if self._layout and 'bg_image' in self._layout:
            theme_dir = os.environ.get('PAGERCTL_THEME',
                os.path.join(os.path.dirname(__file__), 'themes', 'Circuitry'))
            bg_path = os.path.join(theme_dir, self._layout['bg_image'])
            if os.path.isfile(bg_path):
                self.bg_handle = pager.load_image(bg_path)

        pager.clear_input_events()

        # Track when we last rendered so we only redraw when something
        # visible actually changed (elapsed seconds tick, new APs, etc.)
        last_elapsed_seconds = -1
        needs_render = True

        while True:
            # -- Pull new-AP count from the background worker --
            new_aps = self._drain_bg_count() if self.scan_state == 'scanning' else 0
            if new_aps > 0:
                self._geiger_sound(new_aps)
                needs_render = True

            # -- Throttled render: only redraw when something changed --
            now = time.time()
            if self.scan_state == 'scanning':
                elapsed = self.paused_elapsed + int(now - self.start_time)
            elif self.scan_state == 'paused':
                elapsed = self.paused_elapsed
            else:
                elapsed = 0
            if elapsed != last_elapsed_seconds:
                needs_render = True
                last_elapsed_seconds = elapsed

            if needs_render:
                self._render_dashboard()
                needs_render = False

            # -- Drain physical events from the single event queue --
            exit_result = None
            while pager.has_input_events():
                event = pager.get_input_event()
                if not event:
                    break
                button, event_type, _ = event
                if event_type != PAGER_EVENT_PRESS:
                    continue
                if button == pager.BTN_A:
                    self._show_menu()
                    needs_render = True
                    break
                if button == pager.BTN_POWER:
                    exit_result = 'power'
                    break
                if button == pager.BTN_B:
                    exit_result = 'b'
                    break
            if exit_result == 'power':
                self.is_foreground = False
                return 'power'
            if exit_result == 'b':
                self.is_foreground = False
                return None

            # -- Drain virtual events from the web Control panel --
            v = drain_virt_button()
            if v == 'a':
                self._show_menu()
                needs_render = True
            elif v == 'power':
                self.is_foreground = False
                return 'power'
            elif v == 'b':
                self.is_foreground = False
                return None

            # -- Short sleep: 30 ms floor. Determines worst-case input
            #    latency. 30 ms is well under perceptible. --
            time.sleep(0.03)

    def _drain_until_released(self, pager, btn_mask):
        """Wait for a button to be released and drain any parallel event
        queue entries so they don't leak into the caller's main loop."""
        deadline = time.time() + 0.5
        while time.time() < deadline:
            try:
                held, _, _ = pager.poll_input()
            except Exception:
                break
            if not (held & btn_mask):
                break
            time.sleep(0.01)
        pager.clear_input_events()

    def poll_background(self, pager):
        """Lightweight callback from the main loop while wardrive is
        backgrounded. The actual scan processing runs in self._bg_loop
        (background thread), so this just plays a click when the worker
        reports new APs. Always returns instantly."""
        if self.is_foreground or self.scan_state != 'scanning':
            return
        cnt = self._drain_bg_count()
        if cnt <= 0:
            return
        if (self.config.get('sound_enabled', True)
                and self.config.get('geiger_sound', True)):
            try:
                pager.beep(700, 15)
            except Exception:
                pass

    def stop_all(self):
        """Stop all threads. Call on app exit."""
        self._stop_threads()
        self.db.close()

    # -- Theme-driven dashboard rendering --

    def _load_layout(self):
        """Load (or hot-reload) wardrive_dashboard.json from the theme."""
        theme_dir = os.environ.get('PAGERCTL_THEME',
            os.path.join(os.path.dirname(__file__), 'themes', 'Circuitry'))
        path = os.path.join(theme_dir, self.LAYOUT_REL)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._layout = None
            return
        if self._layout is not None and path == self._layout_path and mtime == self._layout_mtime:
            return
        try:
            with open(path) as f:
                self._layout = json.load(f)
            self._layout_path = path
            self._layout_mtime = mtime
        except Exception:
            self._layout = None

    def _gather_stats(self):
        """Return a flat dict of variables the dashboard JSON can reference.

        Every value is a string (or number) so templates render without
        surprises. Colors are palette names that the schema can look up."""
        s = {}
        db_stats = self.db.get_stats()
        s['total'] = str(db_stats['total'])
        s['open']  = str(db_stats['open'])
        s['wep']   = str(db_stats['wep'])
        s['wpa']   = str(db_stats['wpa2'] + db_stats['wpa3'])
        s['handshakes'] = str(db_stats.get('handshakes', 0))

        gps = self.gps_state.copy()
        if not self.config.get('gps_enabled', True):
            s['gps_fix'], s['gps_fix_color'] = 'OFF', 'dim'
        elif gps.fix_mode >= 3:
            s['gps_fix'], s['gps_fix_color'] = '3D Fix', 'green'
        elif gps.fix_mode >= 2:
            s['gps_fix'], s['gps_fix_color'] = '2D Fix', 'yellow'
        else:
            s['gps_fix'], s['gps_fix_color'] = 'No Fix', 'red'
        s['gps_lat']  = f'{gps.lat:.4f}'
        s['gps_lon']  = f'{gps.lon:.4f}'
        s['gps_sats'] = str(gps.satellites)
        speed_mph = gps.speed * 2.237 if gps.has_fix else 0
        s['gps_speed'] = f'{speed_mph:.0f}mph'

        if self.scan_state == 'paused':
            elapsed = self.paused_elapsed
        elif self.scan_state == 'scanning':
            elapsed = self.paused_elapsed + int(time.time() - self.start_time)
        else:
            elapsed = 0
        s['elapsed'] = f'{elapsed//3600:02d}:{(elapsed%3600)//60:02d}:{elapsed%60:02d}'

        s['channel'] = f'CH:{self.current_channel}' if self.current_channel else 'CH:--'
        s['mode'] = 'STL' if self.config.get('scan_mode') == 'stealth' else 'ACT'

        if self.scan_state == 'stopped':
            s['state'], s['state_color'] = 'STOP', 'red'
        elif self.scan_state == 'paused':
            s['state'], s['state_color'] = 'PAUSE', 'yellow'
        else:
            s['state'], s['state_color'] = 'SCAN', 'green'

        bat = self._get_battery()
        if bat is None:
            s['battery'], s['battery_color'] = '--%', 'dim'
        else:
            s['battery'] = f'{bat}%'
            s['battery_color'] = 'green' if bat > 50 else 'yellow' if bat > 20 else 'red'

        return s

    def _render_dashboard(self):
        p = self.pager

        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)

        cfg = self._layout
        if not cfg:
            p.flip()
            return

        stats = self._gather_stats()
        palette = cfg.get('colors', {})

        def resolve_color(name_or_rgb, default='value'):
            if isinstance(name_or_rgb, list) and len(name_or_rgb) == 3:
                return p.rgb(*name_or_rgb)
            rgb = palette.get(name_or_rgb)
            if rgb is None:
                rgb = palette.get(default, [255, 255, 255])
            return p.rgb(*rgb)

        # Optional title
        title = cfg.get('title')
        if title:
            text = title.get('text', '')
            fs = title.get('fs', 22)
            font = FONT_TITLE if title.get('font') == 'title' else FONT_MENU
            color = resolve_color(title.get('color', 'cyan'), 'value')
            x = title.get('x', 0)
            y = title.get('y', 6)
            if x == 'center':
                tw = p.ttf_width(text, font, fs)
                x = (SCREEN_W - tw) // 2
            p.draw_ttf(x, y, text, color, font, fs)

        # Fields — each is a label (optional) + value at an absolute position
        for field in cfg.get('fields', []):
            x = field.get('x', 0)
            y = field.get('y', 0)
            fs = field.get('fs', 16)
            font = FONT_TITLE if field.get('font') == 'title' else FONT_MENU
            gap = field.get('gap', 6)

            # Label
            label = field.get('label')
            if label is None:
                lk = field.get('label_source')
                if lk:
                    label = stats.get(lk)
            label_color = resolve_color(field.get('label_color', 'label'), 'label')

            # Value
            src = field.get('source', '')
            val = field.get('static_text', stats.get(src, ''))
            val = '' if val is None else str(val)

            # Dynamic color via color_source (stats key holding a palette name)
            color_key = field.get('color_source')
            if color_key:
                name = stats.get(color_key) or field.get('color', 'value')
                value_color = resolve_color(name, 'value')
            else:
                value_color = resolve_color(field.get('color', 'value'), 'value')

            # Alignment — 'left' default; 'right' means x is the RIGHT edge
            align = field.get('align', 'left')
            if label:
                label_text = f'{label}:'
                lw = p.ttf_width(label_text, font, fs)
                vw = p.ttf_width(val, font, fs)
                if align == 'right':
                    # Right-align the whole "label: value" block
                    total = lw + gap + vw
                    lx = x - total
                    vx = lx + lw + gap
                elif align == 'center':
                    total = lw + gap + vw
                    lx = x - total // 2
                    vx = lx + lw + gap
                else:
                    lx = x
                    vx = x + lw + gap
                p.draw_ttf(lx, y, label_text, label_color, font, fs)
                p.draw_ttf(vx, y, val, value_color, font, fs)
            else:
                # Value only
                if align == 'right':
                    vw = p.ttf_width(val, font, fs)
                    p.draw_ttf(x - vw, y, val, value_color, font, fs)
                elif align == 'center':
                    vw = p.ttf_width(val, font, fs)
                    p.draw_ttf(x - vw // 2, y, val, value_color, font, fs)
                else:
                    p.draw_ttf(x, y, val, value_color, font, fs)

        # Status-bar widgets — opt-in via wardrive_dashboard.json's
        # top-level `status_bar` key, same convention as every other
        # dashboard. Omit or null to hide (current default).
        if (getattr(self, 'engine', None)
                and self._layout
                and self._layout.get('status_bar')):
            for w in self.engine.widgets:
                try:
                    w.render(p, self.engine.renderer)
                except Exception:
                    pass

        p.flip()

    def _lv(self, lx, vx, y, label, value, fs, lc, vc):
        self.pager.draw_ttf(lx, y, f"{label}:", lc, FONT_MENU, fs)
        self.pager.draw_ttf(vx, y, value, vc, FONT_MENU, fs)

    # -- Unified menu (scan control + settings) --

    def _show_menu(self):
        p = self.pager
        selected = 0

        while True:
            # Build menu based on scan state
            items = []
            if self.scan_state == 'scanning':
                items.append(("Pause Scan", 'pause'))
                items.append(("Stop Scan", 'stop'))
            elif self.scan_state == 'paused':
                items.append(("Resume Scan", 'resume'))
                items.append(("Stop Scan", 'stop'))
            else:
                items.append(("Start Scan", 'start'))

            items.append(("GPS Settings", 'gps'))
            items.append(("Scan Settings", 'scan'))
            items.append(("Wigle Settings", 'wigle'))

            if selected >= len(items):
                selected = len(items) - 1

            # Draw
            if self.bg_handle:
                p.draw_image(0, 0, self.bg_handle)
            else:
                p.clear(0)

            self._draw_submenu("Wardrive", items, selected)
            # _draw_submenu already flips.

            button = wait_any_button(p)
            if button & p.BTN_UP:
                selected = (selected - 1) % len(items)
            elif button & p.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif button & p.BTN_A:
                action = items[selected][1]
                if action == 'start':
                    self._do_start()
                    return
                elif action == 'pause':
                    self._do_pause()
                    return
                elif action == 'resume':
                    self._do_resume()
                    return
                elif action == 'stop':
                    self._do_stop()
                    return
                elif action == 'gps':
                    self._gps_settings()
                elif action == 'scan':
                    self._scan_settings()
                elif action == 'wigle':
                    self._wigle_settings()
            elif button & p.BTN_B:
                return

    # -- Scan control actions --

    def _do_start(self):
        # Check for existing session data — if present, prompt user
        has_data = False
        try:
            has_data = self.db.get_stats()['total'] > 0
        except Exception:
            pass

        if has_data or self.wigle_writer.filepath:
            choice = self._ask_session()
            if choice == 'new':
                self._archive_session()
                self.wigle_writer.start_session()
            else:
                if not self.wigle_writer.filepath:
                    latest = self.wigle_writer.get_latest_file()
                    if latest:
                        self.wigle_writer.resume_session(latest)
                    else:
                        self.wigle_writer.start_session()
        else:
            self.wigle_writer.start_session()

        self.scan_state = 'scanning'
        self.stop_event.clear()
        self._start_threads()
        self.start_time = time.time()
        self.paused_elapsed = 0
        self._persist_running(True)
        try: self.pager.beep(1000, 200)
        except: pass

    def _do_pause(self):
        self.scan_state = 'paused'
        self.paused_elapsed += int(time.time() - self.start_time)
        self._stop_threads()
        self._persist_running(False)
        try: self.pager.beep(600, 150)
        except: pass

    def _do_resume(self):
        self.scan_state = 'scanning'
        self.start_time = time.time()
        self.stop_event.clear()
        self._start_threads()
        self._persist_running(True)
        try: self.pager.beep(1000, 150)
        except: pass

    def _do_stop(self):
        # Stop threads but DON'T archive — preserve session so user can
        # continue or create new on next start.
        self.scan_state = 'stopped'
        self._stop_threads()
        self.start_time = time.time()
        self.paused_elapsed = 0
        self._persist_running(False)
        try: self.pager.beep(400, 200)
        except: pass

    def _persist_running(self, running):
        """Save a 'was_scanning' flag so we can auto-resume after a
        UI restart (pagerctl_home re-exec) without losing the session."""
        self.config['was_scanning'] = bool(running)
        save_config(self.config)

    def auto_resume(self):
        """Headless resume at app startup: if the previous run was
        actively scanning, re-open the session file and restart the
        scanner threads without bringing up the wardrive UI.

        Called from pagerctl_home.main() before the main loop enters.
        Safe to call repeatedly — no-op if already scanning.
        """
        if self.scan_state != 'stopped':
            return
        # Continue the existing session — never prompt at startup.
        if not self.wigle_writer.filepath:
            latest = self.wigle_writer.get_latest_file()
            if latest:
                try:
                    self.wigle_writer.resume_session(latest)
                except Exception:
                    self.wigle_writer.start_session()
            else:
                self.wigle_writer.start_session()
        self.stop_event.clear()
        try:
            self._start_threads()
        except Exception:
            return
        self.scan_state = 'scanning'
        self.start_time = time.time()
        self.paused_elapsed = 0

    def _ask_session(self):
        """Prompt: Continue existing session or start a new one.
        Returns 'continue' or 'new'."""
        p = self.pager
        try:
            existing = self.db.get_stats()['total']
        except Exception:
            existing = 0
        items = [(f"Continue ({existing} APs)", 'continue'), ("New Session", 'new')]
        selected = 0
        while True:
            self._draw_submenu("Wardrive", items, selected)
            button = wait_any_button(p)
            if button & p.BTN_UP or button & p.BTN_DOWN:
                selected = 1 - selected
            elif button & p.BTN_A:
                return items[selected][1]
            elif button & p.BTN_B:
                return 'continue'

    # -- Settings submenus --

    def _gps_settings(self):
        """GPS toggle, device picker, baud, restart."""
        p = self.pager
        selected = 0

        while True:
            gps_on = self.config.get('gps_enabled', True)
            gps_dev = self.config.get('gps_device', '')
            baud = self.config.get('gps_baud', 'auto')
            dev_label = self._short_device_label(gps_dev) if gps_dev else 'Not set'

            items = [
                (f"GPS: {'ON' if gps_on else 'OFF'}", 'toggle_gps'),
                (f"Device: {dev_label}", 'pick_device'),
                (f"Baud: {baud}", 'cycle_baud'),
                ("Restart gpsd", 'restart_gps'),
            ]

            if selected >= len(items):
                selected = len(items) - 1

            self._draw_submenu("GPS Settings", items, selected)

            button = wait_any_button(p)
            if button & p.BTN_UP:
                selected = (selected - 1) % len(items)
            elif button & p.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif button & p.BTN_A:
                action = items[selected][1]
                if action == 'toggle_gps':
                    self.config['gps_enabled'] = not gps_on
                    save_config(self.config)
                elif action == 'pick_device':
                    self._pick_gps_device()
                elif action == 'cycle_baud':
                    bauds = ['auto', '4800', '9600', '38400', '115200']
                    idx = bauds.index(str(baud)) if str(baud) in bauds else 0
                    self.config['gps_baud'] = bauds[(idx + 1) % len(bauds)]
                    save_config(self.config)
                elif action == 'restart_gps':
                    dev = self.config.get('gps_device') or self._auto_detect_gps() or '/dev/ttyACM0'
                    b = self.config.get('gps_baud', 'auto')
                    baud_arg = '' if b in ('auto', 0, '0') else f'-s {b} '
                    os.system(f'killall gpsd 2>/dev/null; sleep 1; gpsd {baud_arg}{dev} 2>/dev/null &')
                    self._show_message("gpsd restarted")
            elif button & p.BTN_B:
                return

    def _pick_gps_device(self):
        """Scan serial devices and let user pick a GPS."""
        p = self.pager
        self._show_message("Scanning for GPS...", 0.5)
        devices = self._detect_gps_devices()
        if not devices:
            self._show_message("No GPS devices found")
            return

        selected = 0
        while True:
            labels = [(self._get_device_name(d), d) for d in devices]
            labels.append(("Back", None))
            if selected >= len(labels):
                selected = len(labels) - 1
            self._draw_submenu("Select GPS", labels, selected)

            button = wait_any_button(p)
            if button & p.BTN_UP:
                selected = (selected - 1) % len(labels)
            elif button & p.BTN_DOWN:
                selected = (selected + 1) % len(labels)
            elif button & p.BTN_A:
                dev = labels[selected][1]
                if dev is None:
                    return
                self.config['gps_device'] = dev
                save_config(self.config)
                self._show_message(f"Set: {os.path.basename(dev)}")
                if self.gps_reader:
                    try:
                        self.gps_reader.restart_gpsd(dev, self.config.get('gps_baud', 'auto'))
                    except Exception:
                        pass
                return
            elif button & p.BTN_B:
                return

    def _detect_gps_devices(self):
        """List serial devices, filtering out known internal/non-GPS devices."""
        exclude = ['uart', 'jtag', 'spi', 'i2c', 'debug', 'ehci', 'hub',
                   'wireless_device', 'csr8510', 'bluetooth']
        devices = []
        for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
            for dev in sorted(glob.glob(pattern)):
                product = self._get_device_product(dev).lower()
                if product and any(kw in product for kw in exclude):
                    continue
                devices.append(dev)
        return devices

    def _get_device_product(self, dev_path):
        """Walk sysfs to find the USB product string for a tty device."""
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
        return ""

    def _get_device_name(self, dev_path):
        product = self._get_device_product(dev_path)
        short = os.path.basename(dev_path)
        return f"{product} ({short})" if product else short

    def _short_device_label(self, dev_path):
        if not dev_path or not os.path.exists(dev_path):
            return "Not set"
        product = self._get_device_product(dev_path)
        short = os.path.basename(dev_path)
        if not product:
            return short
        for strip in [' Receiver', ' receiver', ' Module', ' module',
                      ' - GPS/GNSS', ' GPS/GNSS', '/GNSS', ' - GPS']:
            product = product.replace(strip, '')
        if len(product) > 14:
            product = product[:14].rstrip()
        return f"{product} ({short})"

    def _scan_settings(self):
        """Scan mode, bands, geiger clicks."""
        p = self.pager
        selected = 0

        while True:
            mode = self.config.get('scan_mode', 'stealth')
            b24 = self.config.get('scan_2_4ghz', True)
            b5 = self.config.get('scan_5ghz', True)
            b6 = self.config.get('scan_6ghz', False)
            clicks = self.config.get('geiger_sound', True)

            items = [
                (f"Mode: {mode.title()}", 'cycle_mode'),
                (f"2.4GHz: {'ON' if b24 else 'OFF'}", 'toggle_24'),
                (f"5GHz: {'ON' if b5 else 'OFF'}", 'toggle_5'),
                (f"6GHz: {'ON' if b6 else 'OFF'}", 'toggle_6'),
                (f"Clicks: {'ON' if clicks else 'OFF'}", 'toggle_clicks'),
            ]

            if selected >= len(items):
                selected = len(items) - 1

            self._draw_submenu("Scan Settings", items, selected)

            button = wait_any_button(p)
            if button & p.BTN_UP:
                selected = (selected - 1) % len(items)
            elif button & p.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif button & p.BTN_A:
                action = items[selected][1]
                if action == 'cycle_mode':
                    self.config['scan_mode'] = 'active' if mode == 'stealth' else 'stealth'
                elif action == 'toggle_24':
                    self.config['scan_2_4ghz'] = not b24
                elif action == 'toggle_5':
                    self.config['scan_5ghz'] = not b5
                elif action == 'toggle_6':
                    self.config['scan_6ghz'] = not b6
                elif action == 'toggle_clicks':
                    self.config['geiger_sound'] = not clicks
                save_config(self.config)
            elif button & p.BTN_B:
                save_config(self.config)
                return

    def _wigle_settings(self):
        """Wigle credential status and file uploads."""
        p = self.pager
        selected = 0

        while True:
            c = self.config
            has_creds = bool(c.get('wigle_api_name') and c.get('wigle_api_token'))
            cred_status = "set" if has_creds else "not set"
            files = self._get_wigle_files()

            items = [
                (f"API Creds: {cred_status}", 'info'),
                (f"Upload Files ({len(files)})", 'upload_picker'),
                ("Upload All", 'upload_all'),
                ("Back", 'back'),
            ]
            if selected >= len(items):
                selected = len(items) - 1

            self._draw_submenu("Wigle", items, selected)

            button = wait_any_button(p)
            if button & p.BTN_UP:
                selected = (selected - 1) % len(items)
            elif button & p.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif button & p.BTN_A:
                action = items[selected][1]
                if action == 'info':
                    name = c.get('wigle_api_name')
                    if name:
                        self._show_message(f"Name: ...{name[-8:]}")
                    else:
                        self._show_message("Set via web UI :8888")
                elif action == 'upload_picker':
                    self._wigle_upload_picker()
                elif action == 'upload_all':
                    self._wigle_upload_all()
                elif action == 'back':
                    return
            elif button & p.BTN_B:
                return

    def _get_wigle_files(self):
        files = []
        try:
            for f in sorted(os.listdir(EXPORT_DIR)):
                if f.startswith('wigle_') and f.endswith('.csv'):
                    files.append(f)
        except Exception:
            pass
        return files

    def _wigle_upload_picker(self):
        p = self.pager
        selected = 0
        while True:
            files = self._get_wigle_files()
            if not files:
                self._show_message("No files found")
                return
            items = [(f, f) for f in files]
            items.append(("Back", None))
            if selected >= len(items):
                selected = len(items) - 1

            self._draw_submenu("Upload", items, selected)

            button = wait_any_button(p)
            if button & p.BTN_UP:
                selected = (selected - 1) % len(items)
            elif button & p.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif button & p.BTN_A:
                chosen = items[selected][1]
                if chosen is None:
                    return
                self._wigle_upload_one(chosen)
            elif button & p.BTN_B:
                return

    def _wigle_upload_one(self, filename):
        from wardrive.wigle_export import upload_to_wigle
        name = self.config.get('wigle_api_name', '')
        token = self.config.get('wigle_api_token', '')
        if not name or not token:
            self._show_message("Set API creds via web UI")
            return
        filepath = os.path.join(EXPORT_DIR, filename)
        self._show_message(f"Uploading {filename}...", 0.3)
        try:
            success, msg = upload_to_wigle(filepath, name, token)
        except Exception as e:
            success, msg = False, str(e)[:30]
        self._show_message(msg if msg else ("OK" if success else "Failed"))

    def _wigle_upload_all(self):
        from wardrive.wigle_export import upload_to_wigle
        name = self.config.get('wigle_api_name', '')
        token = self.config.get('wigle_api_token', '')
        if not name or not token:
            self._show_message("Set API creds via web UI")
            return
        files = self._get_wigle_files()
        if not files:
            self._show_message("No files to upload")
            return
        ok_count = 0
        for f in files:
            filepath = os.path.join(EXPORT_DIR, f)
            self._show_message(f"Uploading {f}...", 0.3)
            try:
                ok, _ = upload_to_wigle(filepath, name, token)
            except Exception:
                ok = False
            if ok:
                ok_count += 1
        self._show_message(f"Uploaded {ok_count}/{len(files)}")

    def _show_message(self, msg, duration=1.2):
        """Flash a centered message box over the current screen.
        Geometry, colors, and text style come from
        components/dialogs/wardrive_message_box.json."""
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)

        cfg = _wardrive_msgbox_cfg()
        box = cfg.get('box') or {}
        tcfg = cfg.get('text') or {}

        font = FONT_TITLE if tcfg.get('font') == 'title' else FONT_MENU
        fs = _size_or_px(tcfg.get('font_size', 'medium'), 'medium')
        txt_c = theme_utils.get_color(p, tcfg.get('color', 'white'))

        pad_x = int(box.get('padding_x', 40))
        min_w = int(box.get('min_width', 200))
        box_h = int(box.get('height', 50))
        bw = int(box.get('border_width', 1))
        fill = theme_utils.get_color(p, box.get('fill', 'modal_bg'))
        border = theme_utils.get_color(p, box.get('border_color', 'info_accent'))

        tw = p.ttf_width(msg, font, fs)
        box_w = max(tw + pad_x, min_w)
        bx = (SCREEN_W - box_w) // 2
        by = (SCREEN_H - box_h) // 2

        p.fill_rect(bx, by, box_w, box_h, fill)
        p.fill_rect(bx, by, box_w, bw, border)
        p.fill_rect(bx, by + box_h - bw, box_w, bw, border)
        p.fill_rect(bx, by, bw, box_h, border)
        p.fill_rect(bx + box_w - bw, by, bw, box_h, border)
        p.draw_ttf(bx + (box_w - tw) // 2, by + (box_h - fs) // 2, msg,
                   txt_c, font, fs)
        p.flip()
        time.sleep(duration)

    def _draw_submenu(self, title, items, selected):
        """Draw a settings submenu. Layout, fonts, and colors come
        from components/dialogs/wardrive_submenu.json — themes
        override via that file."""
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)

        cfg = _wardrive_submenu_cfg()
        tcfg = cfg.get('title') or {}
        icfg = cfg.get('items') or {}

        title_font = FONT_TITLE if tcfg.get('font') == 'title' else FONT_MENU
        title_fs = _size_or_px(tcfg.get('font_size', 28), 'large')
        title_y = int(tcfg.get('y', 20))
        title_c = theme_utils.get_color(p, tcfg.get('color', 'info_accent'))

        item_font = FONT_TITLE if icfg.get('font') == 'title' else FONT_MENU
        item_fs = _size_or_px(icfg.get('font_size', 'medium'), 'medium')
        start_y = int(icfg.get('start_y', 58))
        row_h = int(icfg.get('row_height', 22))
        sel_c = theme_utils.get_color(p, icfg.get('color_selected', 'green'))
        norm_c = theme_utils.get_color(p, icfg.get('color_unselected', 'white'))

        tw = p.ttf_width(title, title_font, title_fs)
        p.draw_ttf((SCREEN_W - tw) // 2, title_y, title, title_c,
                   title_font, title_fs)

        for i, (label, _) in enumerate(items):
            y = start_y + i * row_h
            color = sel_c if i == selected else norm_c
            tw = p.ttf_width(label, item_font, item_fs)
            p.draw_ttf((SCREEN_W - tw) // 2, y, label, color,
                       item_font, item_fs)

        p.flip()

    # -- Session management --

    def _archive_session(self):
        from datetime import datetime
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        self.db.close()
        if os.path.isfile(DB_PATH):
            os.rename(DB_PATH, DB_PATH.replace('.db', f'_{timestamp}.db'))
        latest_csv = os.path.join(EXPORT_DIR, 'wardrive_latest.csv')
        if os.path.isfile(latest_csv):
            os.rename(latest_csv, os.path.join(EXPORT_DIR, f'wardrive_{timestamp}.csv'))
        self.db = Database(DB_PATH)

    # -- Thread management --

    def _get_channels(self):
        channels = []
        if self.config['scan_2_4ghz']:
            channels.extend(CHANNELS_2_4)
        if self.config['scan_5ghz']:
            channels.extend(CHANNELS_5)
        if self.config['scan_6ghz']:
            channels.extend(CHANNELS_6)
        return channels or CHANNELS_2_4

    def _start_threads(self):
        channels = self._get_channels()
        if self.config.get('scan_mode') == 'stealth':
            self.scanner = PassiveScanner(
                self.config['capture_interface'], channels,
                self.config.get('hop_speed', 0.5),
                self.scan_queue, self.stop_event)
        else:
            self.scanner = Scanner(
                self.config['scan_interface'], channels,
                self.config['scan_interval'],
                self.scan_queue, self.stop_event)
        self.scanner.start()

        if self.config['gps_enabled']:
            gps_dev = self.config.get('gps_device', '')
            if not gps_dev or not os.path.exists(gps_dev):
                gps_dev = self._auto_detect_gps()
                if gps_dev:
                    self.config['gps_device'] = gps_dev
                    save_config(self.config)
            self.gps_reader = GpsReader(
                gps_dev or '/dev/ttyACM0', self.config['gps_baud'],
                self.gps_state, self.stop_event)
            self.gps_reader.start()

        self._start_bg_worker()

    def _stop_threads(self):
        self.stop_event.set()
        self._stop_bg_worker()
        if self.scanner:
            self.scanner.join(timeout=3)
            self.scanner = None
        if self.gps_reader:
            self.gps_reader.stop()
            self.gps_reader.join(timeout=3)
            self.gps_reader = None

    def _start_bg_worker(self):
        """Start the background scan-processing worker thread."""
        if self._bg_worker and self._bg_worker.is_alive():
            return
        self._bg_stop.clear()
        self._bg_worker = threading.Thread(target=self._bg_loop, daemon=True)
        self._bg_worker.start()

    def _stop_bg_worker(self):
        self._bg_stop.set()
        t = self._bg_worker
        self._bg_worker = None
        if t:
            try:
                t.join(timeout=2)
            except Exception:
                pass

    def _bg_loop(self):
        """Worker thread that drains scan_queue, upserts APs, and
        periodically runs the heavy ops (correlate + wigle CSV
        rewrite). Runs out-of-band so the main loop never blocks
        on scan processing — fixes the 'power menu unresponsive'
        bug where main thread would stall for seconds processing a
        big batch of accumulated APs."""
        last_correlate = 0.0
        last_wigle = 0.0
        while not self._bg_stop.is_set():
            try:
                if self.scan_state != 'scanning':
                    time.sleep(0.5)
                    continue
                new_aps = self._process_scan_results()
                if new_aps > 0:
                    with self._bg_lock:
                        self._bg_new_aps += new_aps
                now = time.time()
                # Correlate every 5 seconds (cleans up false-Open beacons)
                if now - last_correlate > 5.0:
                    try:
                        self.db.correlate_open_bssids()
                    except Exception:
                        pass
                    last_correlate = now
                # Rewrite the wigle CSV at most every 3 seconds when
                # there's been any new data — never per-frame.
                if new_aps > 0 and now - last_wigle > 3.0:
                    try:
                        all_aps = self.db.get_all_aps()
                        self.wigle_writer.append_aps(all_aps)
                    except Exception:
                        pass
                    last_wigle = now
            except Exception:
                pass
            time.sleep(0.2)

    def _drain_bg_count(self):
        """Atomically read + clear the new-APs counter the worker has
        been incrementing. Used by the main loop / foreground render
        to know when to play the geiger click."""
        with self._bg_lock:
            cnt = self._bg_new_aps
            self._bg_new_aps = 0
        return cnt

    def _process_scan_results(self):
        new_count = 0
        gps = self.gps_state.copy()
        while not self.scan_queue.empty():
            try:
                aps = self.scan_queue.get_nowait()
            except queue.Empty:
                break
            before = self.db.get_stats()['total']
            for ap in aps:
                self.db.upsert_ap(ap, gps)
                if ap.get('channel'):
                    self.current_channel = ap['channel']
            after = self.db.get_stats()['total']
            new_count += after - before
        return new_count

    # -- Sounds --

    def _geiger_sound(self, new_count):
        if not self.config.get('geiger_sound', True) or new_count <= 0:
            return
        for i in range(min(new_count, 10)):
            try:
                self.pager.beep(600 + i * 50, 15)
                time.sleep(0.05)
            except:
                break

    # -- Helpers --

    def _get_battery(self):
        try:
            for p in glob.glob('/sys/class/power_supply/*/capacity'):
                with open(p) as f:
                    return int(f.read().strip())
        except:
            pass
        return None

    def _auto_detect_gps(self):
        exclude = ['uart', 'jtag', 'spi', 'i2c', 'debug', 'ehci', 'hub',
                   'wireless_device', 'csr8510', 'bluetooth']
        for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
            for dev in sorted(glob.glob(pattern)):
                try:
                    dev_name = os.path.basename(dev)
                    d = os.path.realpath(f'/sys/class/tty/{dev_name}/device')
                    for _ in range(5):
                        d = os.path.dirname(d)
                        pf = os.path.join(d, 'product')
                        if os.path.isfile(pf):
                            product = open(pf).read().strip().lower()
                            if not any(kw in product for kw in exclude):
                                return dev
                            break
                except:
                    pass
        return None


# Singleton — persists across dashboard visits
_instance = None

def get_wardrive():
    global _instance
    if _instance is None:
        _instance = WardriveUI()
    return _instance

def cleanup():
    global _instance
    if _instance:
        _instance.stop_all()
        _instance = None
