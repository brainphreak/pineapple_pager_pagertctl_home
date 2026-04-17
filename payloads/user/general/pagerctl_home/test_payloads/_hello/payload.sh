#!/bin/bash
# Title:       Hello World
# Author:      pagerctl
# Description: Smoke test for the duckyscript compatibility layer. Blinks the LED, writes two log lines, toggles the DPAD LEDs, beeps once. If this runs to completion the API server and the duckyctl runner are both alive.
# Category:    examples
# Version:     1.0

LOG "hello from pagerctl runner"
LED G SOLID
DPADLED green
sleep 1

LOG "blinking red"
LED R SOLID
DPADLED red
sleep 1

LOG "goodbye"
LED OFF
DPADLED off

echo "exit ok"
