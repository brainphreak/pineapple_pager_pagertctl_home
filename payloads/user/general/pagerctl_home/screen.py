"""screen.py - Screen and MenuItem state management.

Loads Circuitry-format JSON screen definitions and manages
navigation state (selected item, current page).
"""

import json


class MenuItem:
    """A selectable item within a screen page."""
    __slots__ = ('id', 'x', 'y', 'layers', 'selected_layers', 'target',
                 'variable', 'animation', '_anim_frame')

    def __init__(self, data):
        self.id = data.get('id', '')
        self.x = data.get('x', 0)
        self.y = data.get('y', 0)
        self.layers = data.get('layers', [])
        self.selected_layers = data.get('selected_layers', [])
        self.target = data.get('target')
        self.variable = data.get('variable')
        self.animation = data.get('animation') or []
        self._anim_frame = 0


class Screen:
    """Loaded screen with navigation state."""

    def __init__(self, config):
        self.name = config.get('screen_name', 'unknown')
        self.status_bar = config.get('status_bar')
        self.windowed = config.get('windowed_canvas', False)
        self.nav_memory = config.get('has_navigation_memory', False)

        self.button_map = config.get('button_map', {
            'a': 'select', 'b': 'back',
            'up': 'previous', 'down': 'next',
            'left': 'noop', 'right': 'noop'
        })

        bg = config.get('background', {})
        self.bg_color = bg.get('background_color')
        self.bg_layers = bg.get('layers', [])

        # Parse pages -> list of MenuItem lists.
        # Most Circuitry dashboards use `pages: [{menu_items: [...]}]`,
        # but dialog components (launch_payload_dialog, confirmation_dialog,
        # alert_*_dialog, etc.) put `menu_items` directly at the top level
        # with no pages wrapper. Support both by falling back to a single
        # synthetic page when `pages` is missing.
        self.pages = []
        pages_cfg = config.get('pages')
        if pages_cfg:
            for page_data in pages_cfg:
                self.pages.append(
                    [MenuItem(d) for d in page_data.get('menu_items', [])])
        else:
            top_items = config.get('menu_items', [])
            if top_items:
                self.pages.append([MenuItem(d) for d in top_items])

        self.current_page = 0
        self.selected_index = 0

    @property
    def items(self):
        """Menu items on the current page."""
        if 0 <= self.current_page < len(self.pages):
            return self.pages[self.current_page]
        return []

    @property
    def selected_item(self):
        """Currently highlighted menu item, or None."""
        items = self.items
        if 0 <= self.selected_index < len(items):
            return items[self.selected_index]
        return None

    def navigate(self, action):
        """Process a navigation action from the button map.

        Returns:
            (changed, target): changed=True if visual state changed and
            screen needs redraw. target is a string if the action triggers
            navigation ('__back__' for back, or a target screen name).
        """
        if action == 'previous':
            items = self.items
            if items:
                if self.selected_index > 0:
                    self.selected_index -= 1
                else:
                    self.selected_index = len(items) - 1  # wrap to bottom
                return True, None
        elif action == 'next':
            items = self.items
            if items:
                if self.selected_index < len(items) - 1:
                    self.selected_index += 1
                else:
                    self.selected_index = 0  # wrap to top
                return True, None
        elif action == 'previous_page':
            if self.current_page > 0:
                self.current_page -= 1
                # Keep same position, clamp to page size
                max_idx = len(self.items) - 1
                if self.selected_index > max_idx:
                    self.selected_index = max(max_idx, 0)
                return True, None
        elif action == 'next_page':
            if self.current_page < len(self.pages) - 1:
                self.current_page += 1
                max_idx = len(self.items) - 1
                if self.selected_index > max_idx:
                    self.selected_index = max(max_idx, 0)
                return True, None
        elif action == 'select':
            item = self.selected_item
            if item and item.target:
                return False, item.target
        elif action == 'back':
            return False, '__back__'
        return False, None


def load_screen(filepath):
    """Load a Screen from a JSON file."""
    with open(filepath) as f:
        config = json.load(f)
    # Some screens wrap content in a "template" key (e.g., payloads)
    if 'template' in config:
        config = config['template']
    return Screen(config)
