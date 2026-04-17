"""renderer.py - Renders Circuitry theme layers onto the pager display.

All image handles are cached on first load for zero-cost redraws.
Supports bitmap font (fast, default) and TTF font (configurable).
"""

import json
import os


class Renderer:
    """Stateless layer renderer with image caching and palette resolution."""

    def __init__(self, pager, theme_dir, palette, font_path=None):
        self.pager = pager
        self.theme_dir = theme_dir
        self.palette = palette          # name -> {r, g, b}
        self.font_path = font_path      # None = bitmap font, else TTF path
        self._images = {}               # rel_path -> handle (persistent cache)
        self._colors = {}               # palette_name -> rgb565 (persistent cache)
        self._templates = {}            # template_name -> parsed JSON (persistent cache)
        self._bmp_sizes = {'small': 1, 'medium': 2, 'large': 3}
        self._ttf_sizes = {'small': 12.0, 'medium': 16.0, 'large': 22.0}

    # -- Color resolution --

    def color(self, name):
        """Resolve palette name to RGB565. Cached."""
        cached = self._colors.get(name)
        if cached is not None:
            return cached
        c = self.palette.get(name)
        val = self.pager.rgb(c['r'], c['g'], c['b']) if c else self.pager.WHITE
        self._colors[name] = val
        return val

    # -- Image management --

    def image(self, rel_path):
        """Get image handle, loading from disk on first access."""
        cached = self._images.get(rel_path)
        if cached is not None:
            return cached
        full = os.path.join(self.theme_dir, rel_path)
        handle = self.pager.load_image(full)
        self._images[rel_path] = handle  # cache even None to avoid retries
        return handle

    def preload(self, screen):
        """Pre-cache all images referenced by a screen's layers."""
        for layer in screen.bg_layers:
            if 'image_path' in layer:
                self.image(layer['image_path'])
        for page in screen.pages:
            for item in page:
                for layer in item.layers + item.selected_layers:
                    if 'image_path' in layer:
                        self.image(layer['image_path'])
                    # Preload template images too
                    if 'use_template' in layer:
                        tmpl = self._load_template(layer['use_template'])
                        if tmpl:
                            for state_layers in tmpl.values():
                                if isinstance(state_layers, list):
                                    for tl in state_layers:
                                        if 'image_path' in tl:
                                            self.image(tl['image_path'])

    # -- Template support --

    def set_template_paths(self, template_map):
        """Set the template name -> JSON path lookup from theme.json.

        Args:
            template_map: dict like {'toggle': 'components/templates/toggle.json', ...}
        """
        self._template_paths = template_map

    def _load_template(self, name):
        """Load and cache a template JSON by name."""
        if name in self._templates:
            return self._templates[name]
        path = getattr(self, '_template_paths', {}).get(name)
        if not path:
            self._templates[name] = None
            return None
        full = os.path.join(self.theme_dir, path)
        try:
            with open(full) as f:
                data = json.load(f)
            self._templates[name] = data
            return data
        except Exception:
            self._templates[name] = None
            return None

    # -- Layer drawing --

    def draw_layer(self, layer, ox=0, oy=0, variables=None):
        """Draw one layer dict at offset (ox, oy)."""
        x = layer.get('x', 0) + ox
        y = layer.get('y', 0) + oy

        # Image
        if 'image_path' in layer:
            handle = self.image(layer['image_path'])
            if handle:
                # Optional `w`/`h` fields scale the image — used by
                # the launch dialog to shrink the Launch/Cancel
                # button artwork so more room is left for the
                # description text above them.
                w = layer.get('w')
                h = layer.get('h')
                if w and h and hasattr(self.pager, 'draw_image_scaled'):
                    try:
                        self.pager.draw_image_scaled(x, y, int(w), int(h), handle)
                    except Exception:
                        self.pager.draw_image(x, y, handle)
                else:
                    self.pager.draw_image(x, y, handle)

        # Static text
        if 'text' in layer:
            text = str(layer['text'])
            if variables and '${' in text:
                text = variables.resolve(text)
            c = self._layer_color(layer)
            size_name = layer.get('text_size', 'medium')
            # LCD text gets spaced characters to match the LCD cell grid
            if layer.get('text_color_palette') == 'lcd_text' and size_name == 'large':
                self._draw_text_lcd(x, y, text, c)
            else:
                self._draw_text(x, y, text, c, size_name)

        # Variable reference (no static text key)
        if 'variable_name' in layer and 'text' not in layer:
            self._draw_variable(layer, x, y, variables)

    def draw_layers(self, layers, ox=0, oy=0, variables=None):
        """Draw a list of layers at offset."""
        for layer in layers:
            self.draw_layer(layer, ox, oy, variables)

    # -- Internal helpers --

    def _layer_color(self, layer):
        """Extract RGB565 color from a layer's color fields."""
        if 'text_color_palette' in layer:
            return self.color(layer['text_color_palette'])
        tc = layer.get('text_color')
        if tc:
            if isinstance(tc, str):
                return self.color(tc)
            if isinstance(tc, dict):
                return self.pager.rgb(tc['r'], tc['g'], tc['b'])
        return self.pager.WHITE

    def _draw_text_lcd(self, x, y, text, color, char_pitch=17):
        """Draw text with per-character spacing to match LCD cell grid.
        char_pitch: distance between character starts (cell width)."""
        cx = x
        y += 4  # nudge down to center in LCD cells
        for ch in text:
            self.pager.draw_text(cx, y, ch, color, 3)
            cx += char_pitch

    def _draw_text(self, x, y, text, color, size_name):
        """Draw text using the configured font."""
        if self.font_path:
            sz = self._ttf_sizes.get(size_name, 16.0)
            self.pager.draw_ttf(x, y, text, color, self.font_path, sz)
        else:
            sz = self._bmp_sizes.get(size_name, 2)
            self.pager.draw_text(x, y, text, color, sz)

    def _draw_variable(self, layer, x, y, variables):
        """Render a variable_name layer."""
        # Handle use_template (toggles, checkboxes, etc.)
        if 'use_template' in layer:
            self._draw_template(layer, x, y, variables)
            return

        if not variables:
            return
        name = layer['variable_name']
        if name.startswith('$_'):
            return  # firmware-only variable without template, skip

        val = variables.resolve(name) if '${' in name else str(name)
        tmpl = layer.get('string_template', {})
        size_name = tmpl.get('text_size', layer.get('text_size', 'medium'))
        if 'text_color_palette' in tmpl:
            c = self.color(tmpl['text_color_palette'])
        else:
            c = self._layer_color(layer)
        self._draw_text(x, y, str(val), c, size_name)

    def _draw_template(self, layer, x, y, variables):
        """Render a use_template layer (toggles, checkboxes, etc.)."""
        tmpl_data = self._load_template(layer['use_template'])
        if not tmpl_data:
            return

        # Determine state: check variable value, default to 'disabled'
        state = 'disabled'
        if variables:
            name = layer.get('variable_name', '')
            if name and not name.startswith('$_'):
                val = variables.resolve(name) if '${' in name else None
                if val and str(val).lower() in ('1', 'true', 'yes', 'on'):
                    state = 'enabled'

        # Get layers for this state, fall back to disabled
        state_layers = tmpl_data.get(state, tmpl_data.get('disabled', []))
        if isinstance(state_layers, list):
            self.draw_layers(state_layers, x, y, variables)

    def free_all(self):
        """Release all cached image handles."""
        for handle in self._images.values():
            if handle:
                self.pager.free_image(handle)
        self._images.clear()
        self._colors.clear()
