#!/bin/sh
# Title: Pagerctl Home
# Description: Custom home screen with theme engine
# Author: brAinphreAk
# Version: 1.0
# Category: General
# Library: libpagerctl.so (pagerctl)
#
# Pagerctl-native launcher. Lets the Pagerctl Bootloader launch us
# directly from its payloads menu (it scans for pagerctl.sh). The
# Pineapple Pager UI still uses payload.sh.

PAYLOAD_DIR="/root/payloads/user/general/pagerctl_home"

cd "$PAYLOAD_DIR" || exit 1

export PATH="/mmc/usr/bin:$PAYLOAD_DIR/bin:$PATH"
export PYTHONPATH="$PAYLOAD_DIR/lib:$PAYLOAD_DIR:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$PAYLOAD_DIR/lib:$LD_LIBRARY_PATH"

command -v python3 >/dev/null 2>&1 || exit 1
python3 -c "import ctypes" 2>/dev/null || exit 1

# Idempotent — bootloader already stopped pineapplepager on its own
# boot path, but this keeps the script safe if launched elsewhere.
/etc/init.d/pineapplepager stop 2>/dev/null
sleep 0.3

python3 pagerctl_home.py
exit 0
