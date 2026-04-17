#!/usr/bin/env python3
"""pagerctl_home.py - Theme-driven home screen for WiFi Pineapple Pager.

Tight main loop: drain input -> handle -> render (if dirty) -> frame_sync.
No blocking calls. No heavy work in the render path.
"""

import os
import sys
import time
import json
import subprocess
import traceback


# Uncaught-exception hook: write the full traceback to a crash log so we
# can diagnose failures that kill the process (which otherwise vanish
# because stderr is discarded by the launcher script).
def _crash_hook(exc_type, exc_value, exc_tb):
    try:
        with open('/tmp/pagerctl_home_crash.log', 'a') as f:
            f.write('=' * 60 + '\n')
            f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write('\n')
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _crash_hook
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))

from pagerctl import Pager, PAGER_EVENT_PRESS
from theme_engine import ThemeEngine
from variables import VariableResolver
import api_server

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PAYLOAD_DIR, 'settings.json')

# Default theme — override with PAGERCTL_THEME env var
THEME_DIR = os.environ.get('PAGERCTL_THEME',
    os.path.join(PAYLOAD_DIR, 'themes', 'Circuitry'))

# Button mask -> name (matches button_map keys in screen JSON)
_BTN_NAMES = {
    Pager.BTN_UP: 'up',
    Pager.BTN_DOWN: 'down',
    Pager.BTN_LEFT: 'left',
    Pager.BTN_RIGHT: 'right',
    Pager.BTN_A: 'a',
    Pager.BTN_B: 'b',
    Pager.BTN_POWER: 'power',
}


def load_settings():
    """Merge app defaults, wardrive/settings.json (where settings_ui writes),
    and pagerctl_home/settings.json (own overrides)."""
    defaults = {
        'brightness': 80,
        'dim_brightness': 10,
        'dim_timeout': 0,        # 0 = never dim
        'screen_timeout': 60,    # 0 = never off
        'font_path': None,
    }
    # Shared settings live in wardrive/settings.json — settings_ui writes here.
    try:
        from wardrive.config import load_config as _wc
        defaults.update(_wc())
    except Exception:
        pass
    # Local overrides from pagerctl_home's own settings.json.
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults


def _play_boot_sound(pager):
    """Play the configured boot RTTTL melody once the pager is init'd.

    Reads wardrive/settings.json (shared config) for:
      - sound_enabled (master gate)
      - boot_sound    (ringtone filename, e.g. 'tetris.rtttl', or 'None')
    Silently does nothing if either is disabled/unset or the file
    is missing.
    """
    try:
        from wardrive.config import load_config as _wc
        cfg = _wc()
        if not cfg.get('sound_enabled', True):
            return
        name = cfg.get('boot_sound') or ''
        if not name or name == 'None':
            return
        path = os.path.join('/lib/pager/ringtones', name)
        if not os.path.isfile(path):
            return
        with open(path) as f:
            melody = f.read().strip()
        if melody:
            pager.play_rtttl(melody)
    except Exception:
        pass


def _install_wifi_safety_net():
    """Install boot-time recovery for wireless / network / firewall
    config and snapshot baseline configs.

    Two pieces:
      1. /etc/init.d/pagerctl_wifi_safety — runs at every boot to
         disable the hotspot AP and re-enable the client interface,
         so a broken hotspot config never persists across a reboot.
      2. /etc/pagerctl_baseline/{wireless,network,firewall,dhcp} —
         snapshot of /etc/config/* taken on first run, used by the
         "Fix Net" button to roll back manual changes.

    Both are idempotent — no-op once installed/snapshotted.
    """
    # 1. Boot-time init.d wireless rollback
    script_path = '/etc/init.d/pagerctl_wifi_safety'
    script = (
        "#!/bin/sh /etc/rc.common\n"
        "# Pagerctl Home wireless safety rollback.\n"
        "# Runs on every boot to disable the AP hotspot interfaces so\n"
        "# a broken hotspot config never persists across a reboot.\n"
        "START=95\n"
        "boot() {\n"
        "    uci set wireless.wlan0mgmt.disabled='1' 2>/dev/null\n"
        "    uci set wireless.wlan0wpa.disabled='1' 2>/dev/null\n"
        "    uci set wireless.wlan0cli.disabled='0' 2>/dev/null\n"
        "    uci commit wireless 2>/dev/null\n"
        "    wifi reload 2>/dev/null\n"
        "}\n"
    )
    try:
        need_install = True
        if os.path.isfile(script_path):
            with open(script_path) as f:
                if f.read() == script:
                    need_install = False
        if need_install:
            with open(script_path, 'w') as f:
                f.write(script)
            os.chmod(script_path, 0o755)
            subprocess.run([script_path, 'enable'], capture_output=True, timeout=5)
    except Exception:
        pass

    # 2. Baseline snapshot of /etc/config/* for the Fix Net button.
    # Only writes files that DON'T already exist — we never want to
    # overwrite a known-good baseline with a possibly-broken current
    # state.
    baseline_dir = '/etc/pagerctl_baseline'
    try:
        os.makedirs(baseline_dir, exist_ok=True)
        for cfg in ('wireless', 'network', 'firewall', 'dhcp'):
            src = os.path.join('/etc/config', cfg)
            dst = os.path.join(baseline_dir, cfg)
            if not os.path.isfile(src) or os.path.isfile(dst):
                continue
            try:
                with open(src, 'rb') as fs, open(dst, 'wb') as fd:
                    fd.write(fs.read())
            except Exception:
                pass
    except Exception:
        pass


def _mark_wardrive_stopped():
    """Before a full exit (shutdown or bootloader), persist
    was_scanning=False and stop the scanner threads so the next
    pagerctl_home boot comes up in a clean stopped state.

    Only called for exits that are NOT a UI re-exec — the 'reboot'
    action intentionally preserves was_scanning so an active scan
    continues after the restart.
    """
    try:
        import wardrive_ui
        if wardrive_ui._instance is not None:
            try:
                wardrive_ui._instance.stop_all()
            except Exception:
                pass
    except Exception:
        pass
    try:
        from wardrive.config import load_config, save_config
        cfg = load_config()
        if cfg.get('was_scanning'):
            cfg['was_scanning'] = False
            save_config(cfg)
    except Exception:
        pass


def handle_action(action, pager, config, engine):
    """Handle a system action string from the theme engine.

    Returns:
        'exit' to break main loop, None to continue.
    """
    # Tuple actions — currently only ('run_payload', PayloadInfo) which
    # is emitted by the theme engine when the Launch button is tapped
    # on the launch_payload_dialog component.
    if isinstance(action, tuple):
        kind = action[0] if action else None
        if kind == 'run_payload' and len(action) >= 2:
            info = action[1]
            if info is not None:
                # Drop back to the payload list first so on return
                # we're not sitting on the dialog anymore.
                try:
                    engine.go_back()
                except Exception:
                    pass
                launch_payload(info, pager, config)
        return None

    if action == 'shutdown':
        _mark_wardrive_stopped()
        pager.clear(0)
        pager.flip()
        pager.cleanup()
        os.system('poweroff')
        os._exit(0)  # skip finally block, pager already cleaned up
    elif action == 'reboot':
        # Restart UI — re-exec ourselves. was_scanning is preserved
        # so auto_resume() picks up where we left off.
        pager.cleanup()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    elif action == 'bootloader':
        # Exit back to bootloader — user is leaving the app entirely,
        # not just restarting it, so kill the scan state.
        _mark_wardrive_stopped()
        return 'exit'
    elif action == 'sleep_screen':
        pager.screen_off()
        pager.clear_input_events()
        pager.wait_button()
        pager.screen_on()
        pager.set_brightness(config.get('brightness', 80))
        pager.clear_input_events()
    elif action == 'lock_buttons':
        pager.clear_input_events()
        while True:
            event = pager.get_input_event()
            if event and event[0] == Pager.BTN_POWER and event[1] == PAGER_EVENT_PRESS:
                break
            if not pager.has_input_events():
                pager.frame_sync()
        pager.clear_input_events()
    elif action == 'wardrive':
        from wardrive_ui import get_wardrive
        wd = get_wardrive()
        result = wd.run(pager)
        pager.clear_input_events()
        if result == 'power':
            engine.navigate_to('power_menu')
            engine.dirty = True
    elif action == 'sysinfo':
        from sysinfo_ui import get_sysinfo
        si = get_sysinfo()
        result = si.run(pager, engine)
        pager.clear_input_events()
        if result == 'power':
            engine.navigate_to('power_menu')
            engine.dirty = True
    elif action == 'settings':
        from settings_ui import get_settings
        st = get_settings()
        result = st.run(pager, engine)
        pager.clear_input_events()
        if result == 'power':
            engine.navigate_to('power_menu')
            engine.dirty = True
    elif action == 'captive':
        from captive_ui import get_captive
        cu = get_captive()
        result = cu.run(pager, engine)
        pager.clear_input_events()
        if result == 'power':
            engine.navigate_to('power_menu')
            engine.dirty = True
    elif action.startswith('launch_'):
        # Find and launch a payload by name (slug from the theme
        # engine's target string, e.g. launch_alert_example).
        from payload_browser import find_payload
        name = action[7:].replace('_', ' ')
        info = find_payload(name)
        if info and info.is_installed():
            launch_payload(info, pager, config)
    return None


def launch_payload(info, pager, config):
    """Launch a payload.

    Two flavours:
      * `pagerctl.sh` present → the payload is pagerctl-native. We
        tear the pager down, run the script raw with subprocess, and
        rebuild the pager on return. The payload owns the display
        directly and doesn't need our api_server or log screen.
      * `payload.sh` only → standard hak5 duckyscript flow via
        duckyctl + payload_run's on-screen log + dialog queue.

    Always stops pineapplepager on return — stock hak5 payloads tend
    to `service pineapplepager start` on exit as a cleanup step,
    which steals /tmp/api.sock from our api_server. We undo that.
    """
    if getattr(info, 'is_pagerctl', False):
        _launch_pagerctl_payload(info, pager, config)
    else:
        import payload_run
        payload_run.run(pager, info, theme_dir=THEME_DIR)
    # Post-run: stop pineapplepager if a payload restarted it,
    # reclaim the api socket for our server.
    _reclaim_api_socket(pager)
    try:
        pager.set_brightness(config.get('brightness', 80))
    except Exception:
        pass
    pager.clear_input_events()


def _launch_pagerctl_payload(info, pager, config):
    """Tear down pager, run pagerctl.sh, rebuild pager on return.
    The payload owns the framebuffer + input queue directly."""
    pager.clear(0)
    pager.flip()
    try:
        pager.cleanup()
    except Exception:
        pass
    try:
        subprocess.run(['/bin/bash', info.script_path],
                       cwd=info.payload_dir)
    except Exception:
        pass
    # Rebuild pager
    try:
        pager.init()
        pager.set_rotation(270)
    except Exception:
        pass


def _reclaim_api_socket(pager):
    """Ensure pineapplepager is stopped and /tmp/api.sock is owned
    by our api_server. If a payload started pineapplepager, it took
    the socket from us — stop it and re-create the server so we
    reclaim ownership."""
    try:
        subprocess.run(['/etc/init.d/pineapplepager', 'stop'],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(['killall', '-9', 'pineapple'],
                       capture_output=True, timeout=2)
    except Exception:
        pass
    # If our api_server's socket is no longer on disk, recreate it.
    if not os.path.exists('/tmp/api.sock'):
        try:
            api_server.start(pager, THEME_DIR)
        except Exception:
            pass


def main():
    pager = Pager()
    pager.init()
    pager.set_rotation(270)

    config = load_settings()

    try:
        pager.set_brightness(config.get('brightness', 80))
    except Exception:
        pass

    # Install the boot-time wireless safety rollback (idempotent)
    _install_wifi_safety_net()

    # Play the configured boot sound (non-blocking, skipped if unset)
    _play_boot_sound(pager)

    # Start DuckyScript API server on /tmp/api.sock
    api = api_server.start(pager, THEME_DIR)

    # Virtual button queue lives in web_server as a module-level singleton
    # so all UI loops (main + wardrive + sysinfo + settings) drain the
    # same queue and web Control works on every screen.
    from wardrive.web_server import virt_buttons, drain_virt_button

    # Start Web UI if enabled in wardrive config
    try:
        from wardrive.config import load_config as _wc
        from wardrive.web_server import start_web_ui
        _wcfg = _wc()
        if _wcfg.get('web_server', False):
            start_web_ui(port=_wcfg.get('web_port', 1337))
    except Exception:
        pass

    # Auto-resume wardrive scan if it was running before this UI restart,
    # or if the user has Wardrive Autostart enabled in Settings.
    try:
        from wardrive.config import load_config as _wc
        _wcfg = _wc()
        if _wcfg.get('was_scanning', False) or _wcfg.get('wardrive_autostart', False):
            from wardrive_ui import get_wardrive
            get_wardrive().auto_resume()
    except Exception:
        pass

    # Variable resolver for live system data
    variables = VariableResolver()

    # Theme engine
    engine = ThemeEngine(
        pager, THEME_DIR,
        variables=variables,
        font_path=config.get('font_path')
    )

    # Screen power state machine: 'normal' → 'dim' → 'off'.
    # dim_timeout and screen_timeout are measured from last_activity.
    # Any press wakes back to 'normal'.
    last_activity = time.time()
    screen_state = 'normal'

    def _reload_power_config():
        return (
            int(config.get('brightness', 80)),
            int(config.get('dim_brightness', 10)),
            int(config.get('dim_timeout', 0)),
            int(config.get('screen_timeout', 0)),
        )

    def _refresh_config():
        """Reload settings (e.g., after settings_ui writes them)."""
        nonlocal config
        config = load_settings()

    full_b, dim_b, dim_secs, off_secs = _reload_power_config()

    try:
        while True:
            # -- Widget refresh (battery, time, etc. on their own intervals) --
            engine.check_widgets()
            engine.check_animations()

            # -- Background wardrive click polling (when scan is running
            #    and wardrive UI is not on screen) --
            try:
                import wardrive_ui as _wu
                if _wu._instance is not None:
                    _wu._instance.poll_background(pager)
            except Exception:
                pass

            # -- Virtual button events from the web Control tab --
            # Wrapped in exception handling so a bad dispatch doesn't
            # kill the main loop. Logs to /tmp for debugging.
            try:
                while True:
                    vname = drain_virt_button()
                    if not vname:
                        break
                    last_activity = time.time()
                    if screen_state != 'normal':
                        if screen_state == 'off':
                            pager.screen_on()
                        try:
                            pager.set_brightness(full_b)
                        except Exception:
                            pass
                        screen_state = 'normal'
                        engine.dirty = True
                        continue
                    action = engine.handle_input(vname)
                    if action:
                        result = handle_action(action, pager, config, engine)
                        engine.dirty = True
                        _refresh_config()
                        full_b, dim_b, dim_secs, off_secs = _reload_power_config()
                        if result == 'exit':
                            return
            except Exception as e:
                try:
                    with open('/tmp/pagerctl_home_virt.log', 'a') as f:
                        import traceback
                        f.write(f'--- virt drain error ---\n')
                        f.write(traceback.format_exc())
                        f.write('\n')
                except Exception:
                    pass

            # -- Input phase: drain all pending events --
            had_input = False
            while pager.has_input_events():
                event = pager.get_input_event()
                if not event:
                    break
                button, event_type, _ = event
                if event_type != PAGER_EVENT_PRESS:
                    continue

                had_input = True
                last_activity = time.time()

                # Wake from dim or off on any press
                if screen_state != 'normal':
                    if screen_state == 'off':
                        pager.screen_on()
                    try:
                        pager.set_brightness(full_b)
                    except Exception:
                        pass
                    screen_state = 'normal'
                    engine.dirty = True
                    pager.clear_input_events()
                    break

                # Route button to engine
                name = _BTN_NAMES.get(button)
                if name:
                    try:
                        action = engine.handle_input(name)
                    except Exception:
                        import traceback as _tb
                        try:
                            with open('/tmp/pagerctl_home_crash.log', 'a') as _f:
                                _f.write('=' * 60 + '\n')
                                _f.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' handle_input crash\n')
                                _f.write(_tb.format_exc())
                        except Exception:
                            pass
                        action = None
                    if action:
                        try:
                            result = handle_action(action, pager, config, engine)
                        except Exception:
                            import traceback as _tb
                            try:
                                with open('/tmp/pagerctl_home_crash.log', 'a') as _f:
                                    _f.write('=' * 60 + '\n')
                                    _f.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' handle_action crash\n')
                                    _f.write(f'action={action!r}\n')
                                    _f.write(_tb.format_exc())
                            except Exception:
                                pass
                            result = None
                        engine.dirty = True
                        # Drain any stale events the subscreen left in
                        # the queue — fixes the "press B twice to go back"
                        # bug where poll_input and the event queue both
                        # recorded the same press.
                        pager.clear_input_events()
                        # Settings screen may have written new values.
                        _refresh_config()
                        full_b, dim_b, dim_secs, off_secs = _reload_power_config()
                        # Subscreens (wardrive/sysinfo) can run for longer
                        # than the screen timeout. Reset activity timer on
                        # return so we don't instantly blank the display.
                        last_activity = time.time()
                        if result == 'exit':
                            return
                        # Don't drain more events this iteration — let the
                        # next tick do it cleanly.
                        break

            # -- Screen power state machine --
            elapsed = time.time() - last_activity
            if screen_state == 'normal':
                if dim_secs > 0 and elapsed > dim_secs:
                    try:
                        pager.set_brightness(dim_b)
                    except Exception:
                        pass
                    screen_state = 'dim'
                elif off_secs > 0 and elapsed > off_secs:
                    pager.screen_off()
                    screen_state = 'off'
            elif screen_state == 'dim':
                if off_secs > 0 and elapsed > off_secs:
                    pager.screen_off()
                    screen_state = 'off'

            # -- Render phase: only if dirty --
            engine.render()

            # -- Frame sync (~30fps) --
            pager.frame_sync()

    except KeyboardInterrupt:
        pass
    finally:
        try:
            from wardrive_ui import cleanup as wd_cleanup
            wd_cleanup()
        except:
            pass
        api_server.stop(api)
        engine.cleanup()
        pager.clear(0)
        pager.flip()
        pager.cleanup()


if __name__ == '__main__':
    main()
