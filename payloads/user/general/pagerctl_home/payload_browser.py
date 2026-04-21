"""payload_browser.py - Scan /mmc/root/payloads/user/ for hak5 payloads.

Unlike the earlier version, this walks the system payload directory
convention `/mmc/root/payloads/user/<category>/<payload>/payload.sh`
and parses the hak5-style leading comment block for metadata
(# Title, # Author, # Description, # Version, # Category). This
matches how hak5 ships payloads in the official library, so any
payload the user drops under /mmc/root/payloads/user/ will show
up in the pagerctl_home payloads menu automatically.

Keeps the `scan_categories()` and `find_payload()` signatures from
the previous version so theme_engine._show_payloads_screen and the
main-loop `launch_<slug>` dispatcher both still work.
"""

import os

import duckyctl


PAYLOADS_ROOT = '/mmc/root/payloads/user'


def _show_classic_enabled():
    """Check the shared settings for show_classic_payloads. Defaults
    off — classic payloads are hidden until the user opts in via
    Settings → General → Show Classic Payloads. Read fresh each call
    because scan_categories is infrequent enough that caching isn't
    worth the invalidation complexity when the toggle flips."""
    try:
        from wardrive.config import load_config
        return bool(load_config().get('show_classic_payloads', False))
    except Exception:
        return False

# Payloads that would fight us for the display if launched from the
# payloads screen — pagerctl_home itself, and the bootloader which
# is already the process that launched us.
HIDDEN_PAYLOADS = frozenset({'pagerctl_home', 'pagerctl_bootloader'})


class PayloadInfo:
    """Parsed metadata from a payload.sh header."""
    __slots__ = ('title', 'author', 'description', 'version',
                 'category', 'script_path', 'payload_dir', 'requires',
                 'is_pagerctl')

    def __init__(self, title, author, description, version, category,
                 script_path, payload_dir, is_pagerctl=False):
        self.title = title
        self.author = author
        self.description = description
        self.version = version
        self.category = category
        self.script_path = script_path
        self.payload_dir = payload_dir
        # True if the payload ships a `pagerctl.sh` sibling — these
        # run natively under pagerctl_home and skip the pineapplepager
        # service management layer.
        self.is_pagerctl = is_pagerctl
        self.requires = payload_dir

    def is_installed(self):
        return os.path.isfile(self.script_path)


def _parse(payload_dir):
    """Return a PayloadInfo for <dir>/payload.sh (or pagerctl.sh)."""
    pagerctl_sh = os.path.join(payload_dir, 'pagerctl.sh')
    payload_sh = os.path.join(payload_dir, 'payload.sh')
    if os.path.isfile(pagerctl_sh):
        sh = pagerctl_sh
        is_pagerctl = True
    elif os.path.isfile(payload_sh):
        sh = payload_sh
        is_pagerctl = False
    else:
        return None
    meta = duckyctl.parse_header(payload_dir)
    title = meta.get('title') or os.path.basename(payload_dir)
    return PayloadInfo(
        title=title,
        author=meta.get('author', ''),
        description=meta.get('description', ''),
        version=meta.get('version', ''),
        category=meta.get('category', ''),
        script_path=sh,
        payload_dir=payload_dir,
        is_pagerctl=is_pagerctl,
    )


def scan_categories():
    """Return a list of (category_display_name, [PayloadInfo, ...]).

    Each top-level subdirectory of /mmc/root/payloads/user/ is treated
    as a category. Within a category, each subdirectory that contains
    a payload.sh is a payload. Category is sorted alphabetically; the
    display name is title-cased with underscores → spaces.
    """
    if not os.path.isdir(PAYLOADS_ROOT):
        return []
    categories = []
    for cat_name in sorted(os.listdir(PAYLOADS_ROOT)):
        cat_path = os.path.join(PAYLOADS_ROOT, cat_name)
        if not os.path.isdir(cat_path):
            continue
        payloads = []
        show_classic = _show_classic_enabled()
        for entry in sorted(os.listdir(cat_path)):
            if entry in HIDDEN_PAYLOADS:
                continue
            entry_path = os.path.join(cat_path, entry)
            if not os.path.isdir(entry_path):
                continue
            info = _parse(entry_path)
            if info:
                if not show_classic and not info.is_pagerctl:
                    continue
                payloads.append(info)
        if payloads:
            display = cat_name.replace('_', ' ').title()
            categories.append((display, payloads))
    return categories


def _normalize(s):
    """Collapse underscores and spaces into a single form for
    slug-safe comparison. Titles like "CONFIRMATION_DIALOG Example"
    get mangled to "confirmation_dialog_example" by the theme target
    system (spaces → underscores) and then back to
    "confirmation dialog example" (underscores → spaces) in
    navigate_to — losing the distinction between original underscores
    and original spaces. Normalizing both sides to all-spaces-lowercase
    makes the round-trip work regardless of which characters the
    title contained."""
    return s.lower().replace('_', ' ').strip()


def find_payload(name):
    """Find a payload by title (case-insensitive, underscore-tolerant)
    or by directory name. Used by the main-loop launch_<slug>
    dispatcher, which receives the theme-mangled slug of the title.
    Returns PayloadInfo or None.
    """
    if not name:
        return None
    target = _normalize(name)
    for _, payloads in scan_categories():
        for info in payloads:
            if _normalize(info.title) == target:
                return info
            # Also match the payload directory basename in case the
            # title got slug-mangled past recognition by the theme.
            if _normalize(os.path.basename(info.payload_dir)) == target:
                return info
    return None
