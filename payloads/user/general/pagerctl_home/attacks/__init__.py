"""WiFi attack backends for wifi_attacks_ui.

Each module exposes start(config) / stop() / is_running() / stats()
and runs the attack on a background thread so the UI stays
responsive.
"""
