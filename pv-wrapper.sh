#!/bin/bash
set -euo pipefail

if [ -f ~/.borg-ratelimit ]; then
    export RATE=$(cat ~/.borg-ratelimit | tr -d "\n")
    pv --quiet --buffer-size 64k --rate-limit $RATE | "$@"
else
    pv --quiet --buffer-size 64k | "$@"
fi