# backup-vm

**These scripts have been superseded by [`backup-vm.py`](https://github.com/milkey-mouse/backup-vm)**

Back up your libvirt-based VMs using [`borg`](https://github.com/borgbackup/borg)!

## Features

- Works with running VMs
    - Creates a COW snapshot of disks to avoid corruption
        - Automatically pivots the disks back afterwards
    - From the perspective of the VM, restoring from a backup is like a sudden power-off
- Can back up multiple VM disks
- Can back up to an onsite and offsite server
    - Rate-limits upload & download bandwidth
        - BW limit can be changed during backup

## Usage

Install backup scripts:

    sudo cp *.sh /usr/local/bin
    sudo mkdir -p /img  # used for storing temporary snapshots

Configure ratelimiting:

    borg-ratelimit.sh 10  # measured in Mbps

Add `backup-vm.sh` to `root`'s crontab:

    sudo sh -c "(crontab -l; echo '0 0 * * * backup-vm.sh my-vm sda sdb') | crontab -"

Later, to restore the VM:

    sudo restore-vm.sh
