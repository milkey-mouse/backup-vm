#!/bin/bash
set -euo pipefail

if (( $# == 1 )); then
    # 1 Mbps = 125000 bytes/sec
    RATE=$(( 125000*$1 ))
    echo "Setting limit to $RATE bytes/sec"

    # Find existing pv process if a backup is currently running
    PV=$(pidof pv)
    if (( $? == 0 )); then
        pv -R $PV -L $RATE
    fi

    # Store the ratelimit for future backups
    echo "$RATE" > ~/.borg-ratelimit
else
    echo "usage: borg-ratelimit <mbps>"
    exit 2
fi