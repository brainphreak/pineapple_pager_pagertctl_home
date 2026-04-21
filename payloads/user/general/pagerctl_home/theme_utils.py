"""theme_utils.py - Central helpers for reading color_palette and
text_sizes out of the active theme's theme.json.

Lets modules that draw UI pull colors and sizes from the theme
instead of hardcoding RGB tuples and font-size numbers. A new theme
that ships a different color_palette or text_sizes block will reskin
every caller automatically.
"""

import json
import os
import threading

_cache = {}
_lock = threading.Lock()

_SIZE_FALLBACK = {'small': 14, 'medium': 18, 'large': 24}

_RGB_FALLBACK = {
    'black':          (0, 0, 0),
    'white':          (255, 255, 255),
    'red':            (255, 60, 60),
    'green':          (0, 255, 0),
    'yellow':         (255, 220, 50),
    'cyan':           (100, 200, 255),
    'orange':         (255, 160, 40),
    'selection_bg':   (40, 40, 40),
    'highlight_bg':   (30, 50, 80),
    'warning_accent': (255, 220, 50),
    'info_accent':    (100, 200, 255),
    'dim':            (120, 120, 120),
    'light_gray':     (180, 180, 180),
    'pale_blue':      (200, 220, 255),
    'modal_body':     (220, 220, 220),
    'modal_bg':       (0, 0, 0),
}


def _default_theme_dir():
    return os.environ.get('PAGERCTL_THEME',
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     'themes', 'Circuitry'))


def _load(theme_dir):
    with _lock:
        cached = _cache.get(theme_dir)
        if cached is not None:
            return cached
        try:
            with open(os.path.join(theme_dir, 'theme.json')) as f:
                data = json.load(f)
        except Exception:
            data = {}
        _cache[theme_dir] = data
        return data


def invalidate(theme_dir=None):
    """Drop the cached theme.json so the next call re-reads it.
    Useful after a theme switch."""
    with _lock:
        if theme_dir is None:
            _cache.clear()
        else:
            _cache.pop(theme_dir, None)


def get_size(name, theme_dir=None):
    """Return a font size (int) for a named slot. Defaults:
    small=14, medium=18, large=24. Reads from theme.json -> text_sizes."""
    data = _load(theme_dir or _default_theme_dir())
    sizes = data.get('text_sizes') or {}
    v = sizes.get(name)
    if v is None:
        return _SIZE_FALLBACK.get(name, _SIZE_FALLBACK['medium'])
    try:
        return int(v)
    except (TypeError, ValueError):
        return _SIZE_FALLBACK.get(name, _SIZE_FALLBACK['medium'])


def get_rgb(name, theme_dir=None):
    """Return an (r, g, b) tuple for a palette entry."""
    data = _load(theme_dir or _default_theme_dir())
    palette = data.get('color_palette') or {}
    entry = palette.get(name)
    if isinstance(entry, dict):
        return (int(entry.get('r', 255)),
                int(entry.get('g', 255)),
                int(entry.get('b', 255)))
    return _RGB_FALLBACK.get(name, (200, 200, 200))


def get_color(pager, name, theme_dir=None):
    """Return a pager-native color value (already through pager.rgb)."""
    r, g, b = get_rgb(name, theme_dir)
    return pager.rgb(r, g, b)
