"""Captive portal subsystem for pagerctl_home.

Modules:
- server:     HTTP server that serves portal/spoof/mitm pages and
              captures POST form data
- dns_hijack: writes/removes a dnsmasq drop-in that resolves all
              queries to the pager IP while the attack is running
- ap_control: brings up the wlan0mgmt or wlan0wpa interface in the
              configured AP mode (open or clone of a target SSID)
- captures:   credential storage on disk
- mirror:     stage 2 — fetch and rewrite a real site for MITM mode
"""
