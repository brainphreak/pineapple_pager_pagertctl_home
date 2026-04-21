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

- **Home / Wargames dashboard** — `components/dashboards/wargames_dashboard.json`
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
  battery / time / brightness / status icon widgets.

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
