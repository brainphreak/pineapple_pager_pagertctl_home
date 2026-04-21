"""screen_power.py - Shared screen dim / off state machine.

Used by every custom-render UI (wifi_attacks, captive, wardrive,
sysinfo) so the Settings > Display > Dim Timeout / Screen Timeout
values are honored everywhere — not just on the main dashboard.

Usage:

    import screen_power
    sp = screen_power.ScreenPower(config_getter=load_config)
    sp.register_activity()

    while True:
        sp.tick(pager)

        # Input:
        btn = ... # pager button press
        if btn:
            if sp.is_dormant():
                sp.wake(pager)
                continue  # absorb this press as wake-only
            sp.register_activity()
            # handle button normally

        if not sp.is_off():
            render(pager)
"""

import time


class ScreenPower:
    def __init__(self, config_getter):
        """config_getter: a callable returning a dict of settings
        (reread each tick so settings changes propagate) or a plain
        dict."""
        self._cfg = config_getter
        self.last_activity = time.time()
        self.state = 'normal'   # 'normal' | 'dim' | 'off'

    def _get(self, key, default):
        c = self._cfg() if callable(self._cfg) else self._cfg
        try:
            return int(c.get(key, default))
        except Exception:
            return default

    def register_activity(self):
        """Note that the user interacted. Does NOT wake from dim/off
        on its own — call wake() explicitly for that."""
        self.last_activity = time.time()

    def wake(self, pager):
        """Transition to normal brightness, back on if we were off.
        Returns True if state actually changed."""
        changed = self.state != 'normal'
        full_b = self._get('brightness', 80)
        if self.state == 'off':
            try:
                pager.screen_on()
            except Exception:
                pass
        try:
            pager.set_brightness(full_b)
        except Exception:
            pass
        self.state = 'normal'
        self.last_activity = time.time()
        return changed

    def tick(self, pager):
        """Advance the state machine based on elapsed time since
        last_activity. Returns True if the state transitioned this
        tick."""
        elapsed = time.time() - self.last_activity
        dim_secs = self._get('dim_timeout', 0)
        off_secs = self._get('screen_timeout', 0)
        dim_b = self._get('dim_brightness', 10)
        if self.state == 'normal':
            if dim_secs > 0 and elapsed > dim_secs:
                try:
                    pager.set_brightness(dim_b)
                except Exception:
                    pass
                self.state = 'dim'
                return True
            if off_secs > 0 and elapsed > off_secs:
                try:
                    pager.screen_off()
                except Exception:
                    pass
                self.state = 'off'
                return True
        elif self.state == 'dim':
            if off_secs > 0 and elapsed > off_secs:
                try:
                    pager.screen_off()
                except Exception:
                    pass
                self.state = 'off'
                return True
        return False

    def is_dormant(self):
        """True if the screen is dim or off — any user input should
        wake instead of being dispatched."""
        return self.state != 'normal'

    def is_off(self):
        return self.state == 'off'
