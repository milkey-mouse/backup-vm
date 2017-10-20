#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

if (( ${#@} == 0 )); then
    echo "usage: $0 domain [disks]"
    exit 1
elif (( ${#@} == 1 )); then
    DOMAIN="$1"
    DISKS_TO_BACKUP=('sda')
else
    DOMAIN="$1"
    readarray -t DISKS_TO_BACKUP <<<"$(tr ' ' '\n'<<<"${@:2}")"
fi

if (( $(id -u) != 0 )); then
    echo "WARNING: This script probably needs to be run as root."
fi

readarray -t DISK_LOCATIONS <<<"$(virsh domblklist $DOMAIN | head -n-1 | tail -n+3 | grep -E "$(printf '%s\n' "${DISKS_TO_BACKUP[@]}")" | while read -r line; do xargs<<<$(cut -d' ' -f2-<<<"$line"); done)"
readarray -t OTHER_DISKS <<<"$(virsh domblklist $DOMAIN | head -n-1 | tail -n+3 | grep -Ev "$(printf '%s\n' "${DISKS_TO_BACKUP[@]}")" | while read -r line; do cut -d' ' -f1<<<"$line"; done)"

# check if the VM is running so we know if we need to skip snapshotting
VMSTATE=$(virsh list --all | grep "$DOMAIN" | awk '{ print $3}')
if [ "$VMSTATE" != "running" ]; then
    echo "WARNING: VM is currently not running. Starting the VM during the backup may corrupt its disks."
fi

# Export Borg encryption key
export BORG_PASSPHRASE="<your key>"

# Check if the disk has already been snapshotted from a previous failed run
CREATE_SNAPSHOT=false
if [ "$VMSTATE" = "running" ]; then
    for DISK_LOCATION in "${DISK_LOCATIONS[@]}"; do
        if ! grep -q "/img/$DOMAIN-.*-tempsnap.qcow2"<<<"$DISK_LOCATION"; then
            CREATE_SNAPSHOT=true
        fi
    done
fi

# If any disks need a snapshot, run the command
if [ "$CREATE_SNAPSHOT" = true ]; then
    # Create the snapshot to a temp file on the Linux drive
    virsh snapshot-create-as --domain $DOMAIN \
    tempsnap "Temporary snapshot used while backing up $DOMAIN" --disk-only \
    $(for DISK in "${DISKS_TO_BACKUP[@]}"; do [ -f "/img/$DOMAIN-$DISK-tempsnap.qcow2" ] || echo -n "--diskspec=$DISK,file=/img/$DOMAIN-$DISK-tempsnap.qcow2"; done) \
    $(for DISK in "${OTHER_DISKS[@]}"; do echo -n "--diskspec=$DISK,snapshot=no"; done) \
    --quiesce --atomic
elif [ "$VMSTATE" != "running" ]; then
    echo "Skipping snapshot (VM not running)"
else
    echo "Skipping snapshot (tempsnap already created)"
fi

function commitdisk {
    # Update the reported locations of the disk images
    readarray -t DISK_LOCATIONS <<<"$(virsh domblklist $DOMAIN | head -n-1 | tail -n+3 | grep -E "$(printf '%s\n' "${DISKS_TO_BACKUP[@]}")" | while read -r line; do xargs<<<$(cut -d' ' -f2-<<<"$line"); done)"

    # Check if any of the disks are still snapshotted
    DELETE_SNAPSHOT=false
    for DISK_LOCATION in "${DISK_LOCATIONS[@]}"; do
        if grep -q "/img/$DOMAIN-.*-tempsnap.qcow2"<<<"$DISK_LOCATION"; then
            DELETE_SNAPSHOT=true
        fi
    done

    # If so, commit the blocks and delete the snapshot
    if [ "$DELETE_SNAPSHOT" = true ]; then
        # Commit the changes that took place in the Windows drive while
        # the backup was running back into the original image (merge)
        virsh blockcommit "$DOMAIN" --active --pivot --verbose \
        $(for DISK in "${DISKS_TO_BACKUP[@]}"; do [ -f "/img/$DOMAIN-$DISK-tempsnap.qcow2" ] && echo -n "$DISK"; done)

        # Sync the virsh metadata with reality by deleting the metadata
        # for the temporary snapshot (it should be able to delete the
        # external image right now but that is not implemented yet)
        virsh snapshot-delete "$DOMAIN" tempsnap --metadata

        # Remove the copy-on-write file created for temporary changes
        # (they have already been merged back into the original image)
        find /img ! -type d | grep "/img/$DOMAIN-.*-tempsnap.qcow2" | xargs rm -f
    fi
}

# Force commitdisk to run even if the script exits abnormally
trap commitdisk EXIT

# Stupid hack: make a list of all the files we don't want so we can exclude them
readarray -t BACKING_DISK_LOCATIONS <<<$(for DISK_LOCATION in "${DISK_LOCATIONS[@]}"; do (qemu-img info "$DISK_LOCATION" | grep "backing file:" | cut -d":" -f2 | sed -e 's/^[ \t]*//' | xargs realpath 2>/dev/null) || realpath "$DISK_LOCATION"; done)
while read -r IMG; do (realpath "$IMG" | grep -Eq "$(printf '%s\n' "${BACKING_DISK_LOCATIONS[@]}")") || echo "sh:$IMG"; done > iso-exclusions <<< "$(find /img ! -type d)"

# Do the backup
DATE="$(date +%Y-%m-%d)"
echo "Saving backup as $DOMAIN-$DATE"

# the --read-special flag tells borg to follow the symlink & read the block devices directly
# (see https://borgbackup.readthedocs.io/en/stable/usage.html#read-special)

# Export the rate-limited shell for remote backups (for borg >1.0 use --remote-ratelimit instead)
# (see https://borgbackup.readthedocs.io/en/stable/faq.html#is-there-a-way-to-limit-bandwidth-with-project-name)
export BORG_RSH="pv-wrapper.sh ssh"
borg create -v --stats --compression zlib,9 \
"<offsite location>::$DOMAIN-$DATE" \
/img --exclude-from iso-exclusions --read-special &
unset BORG_RSH

borg create -v --stats --compression lz4 \
"<onsite location>::$DOMAIN-$DATE" \
/img --exclude-from iso-exclusions --read-special &

wait || /bin/true

# Remove the temp file for the exclusions
rm iso-exclusions || /bin/true

# Even though this should be exit-trapped, it won't try to merge again
# if it already succeeded and this puts more guest disk accesses on the
# original image, speeding up pivoting
commitdisk

# Prune the backups on the server that are older than a certain date
echo "Pruning old backups"

export BORG_RSH="pv-wrapper.sh ssh"
borg prune -v --list "<offsite location>" --prefix "$DOMAIN-" --keep-within=1m &
unset BORG_RSH

borg prune -v --list "<onsite location>" --prefix "$DOMAIN-" --keep-within=3m &

wait || /bin/true
