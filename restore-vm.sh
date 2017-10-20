#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

if (( ${#@} == 1 )); then
    DOMAIN="$1"
elif (( ${#@} == 2 )); then
    DOMAIN="$1"
    LATEST_ARCHIVE="$2"
else
    echo "usage: restore-vm domain [archive-name]"
    echo ""
    echo "(if archive-name is not specified, it will default to the most recent archive)"
    exit 1
fi

echo "Finding VM..."
readarray -t DISK_LOCATIONS <<<"$(virsh domblklist $DOMAIN | head -n-1 | tail -n+3 | grep -E "$(printf '%s\n' "${DISKS_TO_BACKUP[@]}")" | while read -r line; do xargs<<<$(cut -d' ' -f2-<<<"$line"); done)"

VMSTATE=$(sudo virsh list --all | grep "$DOMAIN" | awk '{ print $3}')
if [ "$VMSTATE" == "running" ]; then
    echo "This script does not work on running VMs (for obvious reasons)."
    echo "Run 'virsh destroy $DOMAIN' and try again."
    exit 1
fi

# Export Borg encryption key
export BORG_PASSPHRASE="<your key>"
export BORG_REPO='<onsite location>' # switch to offsite location in case of failure

if (( $# == 1 )); then
    echo "Finding image..."
    LATEST_ARCHIVE="$(borg list --short --prefix $DOMAIN | sort | tail -n1)"
fi

echo "Rolling back to $LATEST_ARCHIVE"
IMAGE_DATA="$(borg list "$BORG_REPO::$LATEST_ARCHIVE" --list-format '{mode} {size} {path}{NEWLINE}' | grep -v '^d' | head -n1)"
if [ -z "$IMAGE_DATA" ]; then
    echo "No image found in archive!"
    exit 1
fi

IMAGE_SIZE=$(echo "$IMAGE_DATA" | cut -d' ' -f2)
IMAGE_PATH="$(echo "$IMAGE_DATA" | cut -d' ' -f3-)"

echo "Extracting image $IMAGE_PATH to $DISK_LOCATION"
borg extract "$BORG_REPO::$LATEST_ARCHIVE" "$IMAGE_PATH" --stdout | pv -pae --size=$IMAGE_SIZE | sudo dd "of=$DISK_LOCATION"
