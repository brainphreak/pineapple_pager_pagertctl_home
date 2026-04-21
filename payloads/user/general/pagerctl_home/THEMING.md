# Pagerctl Home Theming

This document describes how the pagerctl_home UI is themed, how to
create a new theme, and which UI sections are fully theme-driven vs.
still hardcoded in Python.

## Overview

Pagerctl Home uses a JSON-driven theme engine. Nearly every screen
is built from theme component files that describe background images,
element layout, fonts, and colors. Python code reads the JSON at
runtime and renders it through the pagerctl library, so a new theme
can re-skin the UI without touching any Python.

Themes live under:

```
pagerctl_home/themes/<ThemeName>/
├── theme.json                    # master manifest
├── components/                   # per-screen JSON layouts
│   ├── dashboards/
│   ├── dialogs/
│   ├── keyboards/
│   ├── settings/
│   ├── alerts/
│   ├── pineap/
│   ├── recon/
│   ├── status_bars/
│   ├── templates/
│   └── failsafe/
├── assets/                       # PNG backgrounds, icons, indicators
└── unused/                       # parked/deprecated components
```

The reference theme is `themes/Circuitry/` — copy it as a starting
point for a new theme.

## Selecting a Theme

The active theme is resolved from the `PAGERCTL_THEME` environment
variable, falling back to `themes/Circuitry` if unset. See
`pagerctl_home.py` at the top of the file:

```python
THEME_DIR = os.environ.get('PAGERCTL_THEME',
    os.path.join(PAYLOAD_DIR, 'themes', 'Circuitry'))
```

To run with a custom theme:

```sh
export PAGERCTL_THEME=/root/payloads/user/general/pagerctl_home/themes/MyTheme
python3 pagerctl_home.py
```

## theme.json Manifest

`theme.json` is the master index. It maps logical names the engine
knows about (e.g. `power_menu`, `settings_general_submenu`) to the
relative path of the component JSON that implements that screen.

Top-level keys in `theme.json`:

- `theme_name` / `theme_version` / `theme_framework_version` —
  metadata.
- `status_bars` — named status bar layouts (`default`, `min`, `gps`,
  `network`, `recon`).
- `generic_menus` — screens and submenus that the engine navigates
  to by name. Targets like `power_menu`, `alerts_dashboard`,
  `settings_<x>_submenu`, and `recon_<x>` live here.
- `option_dialogs` — modal popup dialogs used by the theme engine's
  option-picker flow.
- `duckyscript_list_picker` — path to the list-picker component used
  by duckyscript `LIST_PICKER`.

Missing entries fall back gracefully (screen omitted or hardcoded
fallback rendered), so you can start small and extend as you go.

## Component JSON Shape

A component file describes one screen. Minimal skeleton:

```json
{
  "bg_image": "assets/dashboard/my_bg.png",
  "elements": [
    {"type": "text", "x": 12, "y": 8, "text": "Title",
     "color": [255, 220, 50], "font_size": 18},
    {"type": "image", "x": 0, "y": 32,
     "path": "assets/icons/foo.png"}
  ]
}
```

Common element types rendered by `renderer.py` and `elements.py`:

- `text` — TTF text with position, color, font, size.
- `image` — static PNG.
- `rect` / `fill_rect` — primitives.
- Dashboard widgets — battery, time, brightness, status icons; these
  are driven by the status-bar JSON and pull live values from the
  pager hardware at render time.
- Menu items — list items that can reference a `target` string
  dispatched by `theme_engine.navigate_to`.

Coordinate system is the physical screen after rotation:
`480x222` landscape (rotation 270). The top-left is `(0, 0)`.

## Assets

Put PNGs under `assets/` in subfolders that mirror your components
(`assets/dashboard/`, `assets/statusbar/`, `assets/alerts/`, etc.).
Paths in component JSON are relative to the theme directory. The
engine resolves them automatically.

## Creating a New Theme

1. `cp -r themes/Circuitry themes/MyTheme`
2. Edit `themes/MyTheme/theme.json`: update `theme_name`. Optionally
   tweak the `color_palette` block (every UI-role entry like
   `warning_accent`, `info_accent`, `modal_body`, etc. — one edit
   reskins every caller) and the `text_sizes` block (three integers:
   small/medium/large).
3. Replace PNGs under `assets/` with your artwork. Dashboard
   backgrounds are 480x222 landscape (or 222x480 portrait); other
   assets can be any size that fits their component layout.
4. Edit component JSONs under `components/` to move elements,
   change fonts, or reference different palette entries.
5. Set `PAGERCTL_THEME=/root/payloads/user/general/pagerctl_home/themes/MyTheme`
   and launch pagerctl_home (or restart from Settings).

### Reskinning without writing any JSON

If you only want to recolor the UI, editing `color_palette` in
`theme.json` is enough — every module that uses
`theme_utils.get_color()` picks up the change. The same applies to
font sizes via `text_sizes`.

## Central Palette and Sizes

Most UI modules now read their colors and font sizes from two
top-level blocks in `theme.json`:

- **`color_palette`** — named RGB entries. Core semantic names used
  across the UI: `warning_accent` (alert yellow), `info_accent` (cyan
  accent), `dim`, `light_gray`, `pale_blue`, `modal_body`, `modal_bg`,
  `selection_bg`, `highlight_bg`. The stock palette also includes
  `black`, `white`, `red`, `green`, `yellow`, `orange`, etc. Override
  any entry to reskin every caller at once.
- **`text_sizes`** — a `{small, medium, large}` map of integer font
  sizes. Components may either reference a size name (`"font_size": "medium"`)
  or hardcode a pixel value (`"font_size": 18`). Names resolve via the
  theme.

The Python helper `theme_utils.get_color(pager, name)` and
`theme_utils.get_size(name)` are what UI code calls. Both read the
active theme (`PAGERCTL_THEME` env var or `themes/Circuitry`) with
safe fallbacks.

## Theme-Driven UI Surfaces

Every major surface is driven by a JSON component. A new theme that
swaps these JSONs + assets reskins the whole UI with no Python
changes.

### Dashboards

- **Main dashboard** (home screen with the 6 buttons) — `components/dashboards/main_dashboard.json`
- **Alerts dashboard** — `components/dashboards/dashboard_alerts.json`
- **PineAP dashboard** — `components/dashboards/dashboard_pineap.json`
- **Recon dashboard** — `components/dashboards/dashboard_recon.json`
- **Payload log** — `components/payload_log.json`
- **System info screen** — `components/dashboards/sysinfo_dashboard.json`
- **Settings dashboard** — `components/dashboards/settings_dashboard.json`
- **Power menu** — `components/dashboards/power_menu.json`
- **Wardrive dashboard** — `components/dashboards/wardrive_dashboard.json`
- **Captive portal dashboard** — `components/dashboards/captive_dashboard.json`
  (grid layout: col_x, col_widths, row_y_start, row_height, label_gap,
  counter_erase_width; text palette: label_selected/_unselected,
  value_default; bg_sample rgb)

### Dialogs

- **Launch payload dialog** — `components/dialogs/launch_payload_dialog.json`
- **Confirmation** — `components/dialogs/confirmation_dialog.json`
- **Alerts (info/warning/error)** — `components/alerts/alert_*.json`
- **Duckyscript LIST_PICKER** — `components/dialogs/duckyscript_list_picker.json`
- **Option dialogs** — `components/dialogs/ui_option_dialog*.json`
- **Generic popup menu** — `components/dialogs/popup_menu.json`
  (used by `pager_dialogs.popup_menu()`; box geometry, title block,
  items block with row_height / font_size / color_selected / color_unselected)
- **Simple modal fallback** — `components/dialogs/simple_modal.json`
  (used by `payload_run._DialogRunner._simple_modal` when no theme
  dialog matches the dialog kind; box + title + body + footer blocks)
- **Wardrive settings submenu** — `components/dialogs/wardrive_submenu.json`
- **Wardrive message box** — `components/dialogs/wardrive_message_box.json`

### Keyboards

- **Text / numeric / hex / IP / MAC variants** — `components/keyboards/ui_keyboard*.json`.
  `pager_dialogs.edit_string()` auto-delegates to
  `pager_dialogs.themed_keyboard()` with the matching variant JSON, so
  every text input — including WiFi passwords, wardrive API keys, and
  duckyscript `TEXT_PICKER` — renders through the theme. Password
  masking (`secret=True`) is supported.

### Spinner

- `components/spinner.json` — frame image paths + `animation_interval`
  + a `text_area` block that defines the overlay text region
  (`x, y, w, h, font, font_size, color, max_chars, max_lines, line_height`).

### Status bars

- `components/status_bars/status_bar*.json` — per-mode layouts with
  battery / time / brightness / status icon widgets. See
  [Status-bar widgets](#status-bar-widgets) below for how to
  show/hide per screen and how to theme them.

## Button Targets

Every selectable menu item in a dashboard JSON has a `target` string
that determines what pressing **A** does. A button can do any of:

- **Launch a payload** — `target: "launch_<payload_title>"`. The
  engine finds an installed payload by name and runs it. No Python
  needed; works for any payload under `/mmc/root/payloads/user/`.
- **Navigate to a built-in screen** — `target: "captive_dashboard"`,
  `"wardrive_dashboard"`, `"settings_menu"`, etc.
- **Navigate to a custom dashboard you write** — drop a new
  `components/dashboards/<your>.json`, register it in `theme.json`,
  point a button at it. Your dashboard can have its own sub-pages
  navigable by left/right, its own buttons that jump to further
  sub-screens, and so on — all in JSON.
- **Trigger a special action** — `back`, `power`, `noop`,
  `inline_toggle`.

Details for each below.

### Built-in screen targets

Point `target` at one of these to navigate to that screen:

| Target | What it opens |
|--------|---------------|
| `main_dashboard` | Home screen (the 6-button grid) |
| `payloads_dashboard` | Payloads category list |
| `captive_dashboard` | Captive Portal (rogue AP) |
| `wifi_attacks_dashboard` | WiFi Attacks toolkit |
| `wardrive_dashboard` | Wardrive |
| `sysinfo_dashboard` | System info |
| `settings_menu` | Settings |
| `power_menu` | Shutdown / restart / sleep |
| `alerts_dashboard`, `dashboard_pineap`, `dashboard_recon`, etc. | The corresponding dashboard listed in `theme.json`'s `generic_menus` block |
| Any key under `generic_menus` in `theme.json` | Engine navigates there by name — so you can add new screens purely by registering their JSON in `theme.json` and pointing a button at the key |

### Payload launcher targets

Any `target` that starts with `launch_` is treated as a payload
launcher. The string after `launch_` is a slug; the engine normalizes
underscores ↔ spaces and case-folds it, then looks up an installed
payload with a matching `# Title:` (or payload directory name).

```json
{
  "id": "LOKI",
  "selected_layers": [
    { "image_path": "assets/dashboard/pushbutton-down.png", "x": 0, "y": 0 },
    { "text": "LOKI", "x": 101, "y": 17, "text_size": "large", "text_color_palette": "lcd_text" }
  ],
  "x": 5, "y": 44,
  "target": "launch_loki"
}
```

`launch_loki` → finds the payload whose `# Title:` is `Loki` under
`/mmc/root/payloads/user/*/loki/` and shows it in the themed launch
dialog. Works for any installed payload:

- `launch_pagergotchi` → PagerGotchi
- `launch_wardrive` → Wardrive (redundant with the built-in button
  but useful if you want a second shortcut elsewhere)
- `launch_my_payload` → any user payload titled "My Payload"

Two names are blacklisted: `pagerctl_home` and `pagerctl_bootloader`
are hidden from the payload browser (and can't be launched via
`launch_*`). Everything else is fair game.

### Special action targets

| Target | Effect |
|--------|--------|
| `back` | Pop one screen off the navigation stack |
| `noop` | No-op — use for placeholder buttons |
| `power` | Open the power menu |
| `inline_toggle` | Toggle the item's bound `variable` in-place without navigating (for ON/OFF switches) |

### Adding a new button to the main dashboard

`main_dashboard.json` has two `pages`, each with 3 `menu_items`. To
add a 7th/8th/Nth button, either add a new `menu_items` entry to an
existing page (beware of overlapping y-coordinates on the `pushbutton`
background images — adjust `y` per new row) or add a new page with
`"page_index": 2` and left/right button backgrounds under
`background.layers`.

Example: add a "LOKI" payload shortcut to the right page:

```json
{
  "id": "LOKI",
  "layers": [],
  "selected_layers": [
    { "image_path": "assets/dashboard/pushbutton-down-right.png", "x": 0, "y": 0 },
    { "text": "LOKI", "x": -331, "y": 17, "text_size": "large", "text_color_palette": "lcd_text" }
  ],
  "x": 437, "y": 44,
  "target": "launch_loki"
}
```

Plus a matching `{ "image_path": "pushbutton-up-right.png", "x": 437, "y": 44 }`
in `background.layers`. No Python changes needed — the engine
dispatches `launch_*` generically.

### Making an entirely new dashboard launchable from a button

1. Write `components/dashboards/my_panel.json` modeled on an existing
   one.
2. Register it in `theme.json`:
   ```json
   "generic_menus": {
     ...,
     "my_panel": "components/dashboards/my_panel.json"
   }
   ```
3. Add a button with `"target": "my_panel"` to any dashboard.

No Python changes needed — the engine navigates to any name listed in
`generic_menus` automatically. Only panels with bespoke interactive
render loops (captive, wardrive, wifi_attacks, sysinfo, settings) need
extra Python glue; a pure JSON menu/dialog does not.

### Custom dashboards with sub-pages

Dashboard JSONs have a `pages` array. Each page has a `page_index`
and a `menu_items` list. **Left / right** navigates between pages.
That's the mechanism the main dashboard uses for its two screens of
buttons — any theme author can do the same to fit more buttons.

Minimum multi-page dashboard (reusing the main dashboard's pushbutton
art for convenience):

```json
{
  "screen_name": "my_panel",
  "status_bar": "default",
  "button_map": {
    "a": "select",
    "b": "back",
    "up": "previous",
    "down": "next",
    "left": "previous_page",
    "right": "next_page"
  },
  "background": {
    "layers": [
      { "image_path": "assets/dashboard/circuit_bg.png", "x": 0, "y": 0 },
      { "image_path": "assets/dashboard/pushbutton-up.png", "x": 5, "y": 44 },
      { "image_path": "assets/dashboard/pushbutton-up.png", "x": 5, "y": 93 },
      { "image_path": "assets/dashboard/pushbutton-up.png", "x": 5, "y": 143 }
    ],
    "background_color": { "r": 0, "g": 0, "b": 0 }
  },
  "pages": [
    {
      "page_index": 0,
      "menu_items": [
        {
          "id": "MY_PAYLOAD", "x": 5, "y": 44, "target": "launch_my_payload",
          "selected_layers": [
            { "image_path": "assets/dashboard/pushbutton-down.png", "x": 0, "y": 0 },
            { "text": "MY PAYLOAD", "x": 101, "y": 17,
              "text_size": "large", "text_color_palette": "lcd_text" }
          ]
        },
        {
          "id": "SUBMENU_A", "x": 5, "y": 93, "target": "my_submenu_a",
          "selected_layers": [
            { "image_path": "assets/dashboard/pushbutton-down.png", "x": 0, "y": 0 },
            { "text": "SUBMENU A", "x": 101, "y": -32,
              "text_size": "large", "text_color_palette": "lcd_text" }
          ]
        },
        {
          "id": "BACK", "x": 5, "y": 143, "target": "back",
          "selected_layers": [
            { "image_path": "assets/dashboard/pushbutton-down.png", "x": 0, "y": 0 },
            { "text": "BACK", "x": 101, "y": -82,
              "text_size": "large", "text_color_palette": "lcd_text" }
          ]
        }
      ]
    },
    {
      "page_index": 1,
      "menu_items": [
        {
          "id": "OTHER_PAYLOAD", "x": 5, "y": 44, "target": "launch_another_payload",
          "selected_layers": [
            { "image_path": "assets/dashboard/pushbutton-down.png", "x": 0, "y": 0 },
            { "text": "OTHER", "x": 101, "y": 17,
              "text_size": "large", "text_color_palette": "lcd_text" }
          ]
        }
      ]
    }
  ]
}
```

That gives you a two-page custom panel: page 0 has a payload launcher,
a submenu jump, and a back button; page 1 has another payload. Left/
right switch pages. Register the file under `generic_menus` in
`theme.json` as above, and the `SUBMENU A` button would similarly
reference another JSON you add to `generic_menus`.

You can nest as deep as you want — every dashboard JSON in
`generic_menus` is reachable by name, so a button in panel A can
point at panel B, which has a button pointing at panel C, etc. The
engine maintains a navigation stack so `"target": "back"` / the **B**
button pops one level.

### Putting it together

A theme author who wants to add an 8-button main dashboard with
mixed "launch payload" and "open submenu" behaviors can do all of
it by editing JSON:

1. Edit `main_dashboard.json` to add the extra `menu_items` (and
   matching `pushbutton-*.png` entries in `background.layers`).
2. For each new button, set `target` to either `launch_<payload>`
   or the name of a dashboard registered under `generic_menus`.
3. For each new submenu dashboard, write its own JSON, add it to
   `generic_menus` in `theme.json`.

No Python, no rebuild. Theme packs can ship entirely new navigation
trees this way.

## Status-bar widgets

The "status bar" (battery, clock, brightness, WiFi / GPS / Bluetooth
icons) is a set of live widgets the engine can draw on top of any
dashboard. Visibility and layout are both theme-controlled — no
Python edits required.

### Showing or hiding the status bar per screen

Every dashboard component JSON has an optional top-level key:

```json
"status_bar": "default"
```

- Set it to a name (`"default"`, `"min"`, `"network"`, `"recon"`,
  `"gps"`) to render that status bar on the screen.
- Omit it or set it to `null` to hide the status bar entirely on
  that screen.

This works for the engine's own screens (main dashboard, settings,
alerts, pineap, recon) and for the custom-rendered ones
(`wardrive_dashboard`, `captive_dashboard`) — each custom renderer
reads the same field. So a theme that wants battery/clock on the
Wardrive screen just adds `"status_bar": "default"` to
`wardrive_dashboard.json`; a theme that wants the captive portal
cleaner just removes it from `captive_dashboard.json`.

### Available status bar variants

Listed in `theme.json` under the `status_bars` block:

```json
"status_bars": {
  "default": "components/status_bars/status_bar.json",
  "network": "components/status_bars/status_bar_network.json",
  "recon":   "components/status_bars/status_bar_recon.json",
  "min":     "components/status_bars/status_bar_min.json",
  "gps":     "components/status_bars/status_bar_gps.json"
}
```

Each of those JSONs describes *what* renders and *where*. Change
them to reskin.

### Styling the widgets

A status bar JSON looks like:

```json
{
  "background": { "layers": [], "background_color": { "r": 0, "g": 0, "b": 0 } },
  "status_bar_items": {
    "Time":       { "x": 213, "y": 5, "text_size": 3, "recolor_palette": "lcd_text", "layers": {...} },
    "Battery":    { "x": 429, "y": 3, "icon_w": 46, "icon_h": 24, "text_size": 3, "text_gap": 4, "layers": {...} },
    "Brightness": { ... },
    "Wifi":       { ... },
    "Gps":        { ... },
    "Bluetooth":  { ... }
  }
}
```

- **`x`, `y`** — absolute position of the widget on the screen.
- **`text_size`** — font size for widgets that draw text (Time,
  Battery percentage). Use an integer or one of `small`/`medium`/
  `large` if you want it resolved through `theme_utils`.
- **`icon_w`, `icon_h`** — size of the widget's icon image (used by
  Battery, Bluetooth, etc.).
- **`text_gap`** — pixels between the icon and the text for widgets
  that combine both.
- **`recolor_palette`** — applies one of the `color_palette` entries
  from `theme.json` to a monochrome icon + text. Change to pick a
  different accent.
- **`layers`** — per-state icon image lists (e.g. Battery has
  `discharging`, `charging_25`, `charging_50`, ..., `charged`). Swap
  the PNG paths to re-skin an icon; add a new state and its icon if
  you want finer granularity.

### Adding a new status bar variant

1. Create `components/status_bars/status_bar_mine.json` modelled on
   one of the existing ones.
2. Register it in `theme.json`:
   ```json
   "status_bars": {
     ...,
     "mine": "components/status_bars/status_bar_mine.json"
   }
   ```
3. Reference it from any dashboard component: `"status_bar": "mine"`.

### Removing individual widgets without rewriting the status bar

Two options:

- Delete the widget's entry from `status_bar_items` (e.g. remove
  `"Bluetooth": { ... }` to drop the BT icon).
- Or set its position off-screen (e.g. `"x": -100`) if you want to
  keep the definition but hide it on this variant.

## Remaining Inline-Drawn Surfaces

These are still drawn directly in Python and don't have a dedicated
component JSON yet. A new theme will still render them correctly —
they respect the central palette and sizes — but the layout can't be
moved without Python edits.

- **`theme_engine._show_payloads_screen()` / `_show_category_screen()`**
  — the dynamic Payloads list. Background image path
  `assets/payloads_dashboard/payloads_bg.png` is hardcoded, and row
  spacing / x positions are inline.
- **`theme_engine._show_launch_dialog()`** — reuses
  `launch_payload_dialog.json` but post-processes with hardcoded text
  positions and button-size tweaks.

## Already Fully Theme-Driven (Summary)

- All dashboards listed above.
- All dialogs (including fallback modal + generic popup menu).
- Keyboards (all 5 variants, plus password masking).
- Spinner (frames, animation, and text overlay).
- Status bars (all 5 modes).
- Launch payload dialog (content).
- Settings dashboard and every settings submenu.
- Alerts + confirmation.
- Wardrive main dashboard + submenus + message box.
- Captive portal dashboard grid.

## Structure of the Circuitry Theme

`themes/Circuitry/theme.json` is the most complete reference. Notable
groupings inside `components/`:

- `dashboards/` — top-level screens (`dashboard_alerts.json`,
  `dashboard_pineap.json`, `power_menu.json`, `wardrive_dashboard.json`,
  `sysinfo_dashboard.json`, etc.).
- `dialogs/` — modal dialogs (`confirmation_dialog.json`,
  `launch_payload_dialog.json`, `ui_option_dialog*.json`,
  `duckyscript_list_picker.json`).
- `keyboards/` — on-screen keyboards for prompt/IP/MAC/number
  pickers.
- `settings/` — one submenu file per Settings section.
- `alerts/` — info/warning/error single-button dialog templates.
- `status_bars/` — per-mode status bar layouts.
- `templates/` — shared layout fragments (buttons, common rows).
- `failsafe/` — last-resort screens when something goes wrong.

Anything under `unused/` is parked — the engine does not load it
unless `theme.json` references it.

## Contributing a Theme-Migration

If you want to push one of the hardcoded sections above into the
theme system, the rough pattern is:

1. Define a new component JSON under `themes/Circuitry/components/...`
   describing the layout.
2. Add a mapping in `theme.json` under the appropriate top-level key
   so the engine can find it.
3. Replace the hardcoded Python draw calls with
   `theme_engine.render_component(<target>, subs)` or
   `renderer.draw_layers(theme_dir, raw_json)`.
4. Delete the Python constants that are now redundant.

Keep the existing Python fallback intact for one release cycle so
themes that don't ship the new component still render.
