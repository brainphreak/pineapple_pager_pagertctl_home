# Writing a Payload for Pagerctl Home

Pagerctl Home lists every payload under
`/root/payloads/user/<category>/<payload>/` that ships an entry
script.

**Supported today:** a `pagerctl.sh` entry script that launches a
**Python** program using the `pagerctl` library (`libpagerctl.so` +
`pagerctl.py`) to draw the screen and read input.

Other languages (C/C++, Lua, bash) can technically load
`libpagerctl.so` too, but only the Python path is currently
documented and tested. Treat non-Python payloads as unsupported for
now.

Duckyscript / classic `payload.sh`-only payloads are **not
supported** (see below).

## Supported entry script

| File | Status |
|------|--------|
| `pagerctl.sh` | **Supported.** Shell launcher that sets environment and `exec`s into your Python entry program. |
| `payload.sh` (classic duckyscript) | **Not currently supported.** Hidden by default in the menu. |

If both exist, `pagerctl.sh` wins.

### Why classic / duckyscript isn't supported

Duckyscript commands (`LOG`, `ALERT`, `TEXT_PICKER`, `SPINNER`, etc.)
are provided by the `pineapplepager` service. Pagerctl Home stops
that service while it's running, otherwise the two processes fight
over the framebuffer and the `/tmp/api.sock` input queue — you get
corrupted drawing and lost keypresses. With `pineapplepager` stopped
the duckyscript commands can't be relied on.

So classic payloads are hidden from the menu by default. A user can
opt in via **Settings → General → Show Classic Payloads**, but the
payloads won't run correctly until we ship a pagerctl_home-native
duckyscript shim. That's on the roadmap, not in today's release.

**Bottom line: ship a `pagerctl.sh` that runs a Python program against
the `pagerctl` library.**

## Directory layout

```
my_payload/
├── pagerctl.sh        # required — the launcher
├── my_entry.py        # your Python program
└── lib/               # optional — bundled Python deps
    ├── libpagerctl.so # the pager display/input library
    └── pagerctl.py    # ctypes wrapper
```

`libpagerctl.so` and `pagerctl.py` are the canonical pagerctl
library. If your payload doesn't bundle them, the `pagerctl.sh`
example below shows how to pull them from a system-wide install.

## Minimum `pagerctl.sh`

```sh
#!/bin/sh
# Title: My Payload
# Description: Short description shown on the launch screen
# Author: your_name
# Version: 1.0
# Category: <your category>

PAYLOAD_DIR="/root/payloads/user/<category>/<my_payload>"
cd "$PAYLOAD_DIR" || exit 1

export PATH="/mmc/usr/bin:$PAYLOAD_DIR/bin:$PATH"
export PYTHONPATH="$PAYLOAD_DIR/lib:$PAYLOAD_DIR:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$PAYLOAD_DIR/lib:$LD_LIBRARY_PATH"

python3 my_entry.py
exit 0
```

Pagerctl Home has already torn the pager down before running your
script — do **not** call `/etc/init.d/pineapplepager start|stop`
yourself. Home handles the surrounding lifecycle.

## Minimum Python entry program

```python
# my_entry.py
from pagerctl import Pager

def main():
    p = Pager()
    p.init()
    p.set_rotation(270)

    try:
        while True:
            p.clear(p.BLACK)
            p.draw_ttf(20, 20, "Hello from my payload",
                       p.rgb(255, 220, 50),
                       "/root/payloads/user/general/pagerctl_home/fonts/menu.ttf",
                       18)
            p.flip()

            btn = p.wait_button()
            if btn & p.BTN_B:
                return
    finally:
        p.cleanup()

if __name__ == "__main__":
    main()
```

The `Pager` object gives you:

- **Display:** `clear`, `fill_rect`, `rect`, `draw_ttf`,
  `draw_image_file_scaled`, `load_image`, `draw_image`, `flip`.
- **Input:** `wait_button` (blocking), `poll_input`,
  `get_input_event` / `has_input_events` / `peek_buttons`
  (non-blocking), `clear_input_events`.
- **Feedback:** `beep`, `vibrate`, `play_rtttl`, `led_rgb`, `led_set`.
- **System:** `set_brightness`, `set_rotation`, `rgb`, `random`,
  `seed_random`.
- **Button constants:** `BTN_A`, `BTN_B`, `BTN_UP`, `BTN_DOWN`,
  `BTN_LEFT`, `BTN_RIGHT`, `BTN_POWER`.

See `pineapple_pager_loki/payloads/user/reconnaissance/loki/pagerctl.py`
for the library source.

## Header metadata

Pagerctl Home reads these lines from the top of your `pagerctl.sh`
for the payload browser and launch dialog:

- `# Title:` — display name in the menu.
- `# Description:` — short description on the launch dialog.
- `# Author:` — shown on the launch dialog.
- `# Version:` — shown on the launch dialog.
- `# Category:` — informational; the actual menu category is derived
  from the parent directory name under `/root/payloads/user/`.

## Exit codes

Return `0` on normal exit. Any non-zero exit is logged; Pagerctl
Home just rebuilds the pager and returns to the menu either way.

## Getting listed

Pagerctl Home scans `/root/payloads/user/` every time the payloads
screen is opened. There is nothing to register — as soon as your
directory contains a `pagerctl.sh`, it shows up.

Two names are blacklisted and won't appear even if installed:
`pagerctl_home` itself and `pagerctl_bootloader`. They're already
running when pagerctl_home is the UI.

### Bootloader compatibility

The **Pagerctl Bootloader** uses the same discovery rule — it scans
`/mmc/root/payloads/user/` for directories containing a `pagerctl.sh`
and shows the union of installed payloads. A single `pagerctl.sh`
therefore makes your payload launchable from both the Pagerctl Home
Payloads menu and the bootloader's pre-boot menu, with no separate
`scripts/launch_*.sh` shim to maintain.

The bootloader's blacklist excludes only `pagerctl_bootloader` (not
pagerctl_home), so users can auto-boot straight into Pagerctl Home
from the bootloader if they want.

## Real-world example

See `pineapple_pager_loki/payloads/user/reconnaissance/loki/pagerctl.sh`
— a production pagerctl-native launcher that sets up bundled
binaries, libraries, checks python3 availability, creates a data
directory, and runs a Python menu in an exit-code-driven handoff
loop.
