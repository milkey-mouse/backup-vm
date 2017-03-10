#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

DOMAIN="$1"
UUID=$(sudo virsh dumpxml $DOMAIN | grep uuid | cut -d">" -f2 | cut -d"<" -f1)
DISK_LOCATION=$(sudo virsh domblklist "$DOMAIN" | grep sda | tr -s " " | cut -d" " -f2-)

# Export Borg encryption key
export BORG_PASSPHRASE="<your key>"

# Export the rate-limited remote shell
#export BORG_RSH="/usr/local/bin/pv-wrapper ssh"

# Check if the disk has already been snapshotted from a previous failed run
if [ "$DISK_LOCATION" != "/img/$DOMAIN-tempsnap.qcow2" ]; then
    # Create the snapshot to a temp file on the Linux drive
    sudo virsh snapshot-create-as --domain $DOMAIN \
    tempsnap "Temporary snapshot used while backing up $DOMAIN" \
    --disk-only --diskspec sda,file="/img/$DOMAIN-tempsnap.qcow2" \
    --quiesce --atomic
else
    DISK_LOCATION=$(sudo qemu-img info "$DISK_LOCATION" | grep "backing file:" | cut -d":" -f2 | sed -e 's/^[ \t]*//')
fi

function commitdisk {
    DISK_LOCATION=$(sudo virsh domblklist "$DOMAIN" | grep sda | tr -s " " | cut -d" " -f2-)
    if [ "$DISK_LOCATION" == "/img/$DOMAIN-tempsnap.qcow2" ]; then
        # Commit the changes that took place in the Windows drive while
        # the backup was running back into the original image (merge)
        sudo virsh blockcommit "$DOMAIN" sda --active --pivot --verbose

        # Sync the virsh metadata with reality by deleting the metadata
        # for the temporary snapshot (it should be able to delete the
        # external image right now but that is not implemented yet)
        sudo virsh snapshot-delete "$DOMAIN" tempsnap --metadata

        # Remove the copy-on-write file created for temporary changes
        # (they have already been merged back into the original image)
        sudo rm -f "/img/$DOMAIN-tempsnap.qcow2"
    fi
}

# Force commitdisk to run even if the script exits abnormally
trap commitdisk EXIT

# Stupid hack: make a list of all the files we *don't* want to save so we can exclude them from the backup
find $(dirname "$DISK_LOCATION") ! -type d ! -wholename "$DISK_LOCATION" | awk '{print "sh:" $0;}' > iso-exclusions

# Do the backup
echo "Saving backup as $DOMAIN-$(date +%Y-%m-%d)"

# the --read-special flag tells borg to follow the symlink & read the block device directly
# (see https://borgbackup.readthedocs.io/en/stable/usage.html#read-special)
sudo -E borg create -v --stats --compression lz4 \
"<backup-location>::$DOMAIN-$(date +%Y-%m-%d)" \
$(dirname "$DISK_LOCATION") --exclude-from iso-exclusions --read-special &

wait || /bin/true

# Remove the temp file for the exclusions
rm iso-exclusions || /bin/true

# Even though this should be exit-trapped, it won't try to merge again
# if it already succeeded and this ensures all guest disk access is on
# the faster NVMe SSD (with the full image)
commitdisk

# Prune the backups on the server that are older than a certain date
echo "Pruning old backups"
borg prune -v --list <backup-location> --prefix "$DOMAIN-" --keep-within=1m &

wait || /bin/true
