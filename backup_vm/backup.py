#!/usr/bin/env python3

import os.path
import sys
import subprocess
import libvirt
from . import parse
from . import multi
from . import builder
from . import snapshot


def main():
    args = parse.BVMArgumentParser()
    conn = libvirt.open()
    if conn is None:
        print("Failed to open connection to libvirt", file=sys.stderr)
        sys.exit(1)
    try:
        dom = conn.lookupByName(args.domain)
    except libvirt.libvirtError:
        print("Domain '{}' not found".format(args.domain))
        sys.exit(1)

    all_disks = set(parse.Disk.get_disks(dom))
    if len(all_disks) == 0:
        print("Domain has no disks(!)", file=sys.stderr)
        sys.exit(1)

    disks_to_backup = args.disks and {x for x in all_disks if x.target in args.disks} or all_disks
    if len(disks_to_backup) != len(args.disks or all_disks):
        print("Some disks to be backed up don't exist on the domain:",
              *sorted(x.target for x in all_disks if x.target not in args.disks), file=sys.stderr)
        sys.exit(1)

    for disk in all_disks:
        filename = args.domain + "-" + disk.target + "-tempsnap.qcow2"
        if disk not in disks_to_backup:
            disk.snapshot_path = None
        elif disk.type == "dev":
            # we probably can't write the temporary snapshot to the same directory
            # as the original disk, so use the default libvirt images directory
            disk.snapshot_path = os.path.join("/var/lib/libvirt/images", filename)
        else:
            disk.snapshot_path = os.path.join(os.path.dirname(disk.path), filename)

    for archive in args.archives:
        archive.extra_args.append("--read-special")

    # dump the xml before the snapshot so that we have the original disk devices
    dom_xml = dom.XMLDesc(0)

    with snapshot.Snapshot(dom, all_disks, args.progress), \
            builder.ArchiveBuilder(disks_to_backup) as archive_dir:

        f = open("origin.txt", "w")
        f.write(platform.node())
        f.close()

        f = open(args.domain + ".xml", "w")
        f.write(dom_xml)
        f.close()

        for disk in all_disks:
            if disk in disks_to_backup and disk.type == "dev":
                lv = subprocess.check_output(["lvdisplay", disk.path], text=True)
                f = open( (disk.path + ".lv")[1:].replace("/", "--"), "w" )
                f.write(lv)
                f.close()

        if args.progress:
            borg_failed = multi.assimilate(args.archives, archive_dir.total_size)
        else:
            borg_failed = multi.assimilate(args.archives)

    # bug in libvirt python wrapper(?): sometimes it tries to delete
    # the connection object before the domain, which references it
    del dom
    del conn

    sys.exit(borg_failed or any(disk.failed for disk in disks_to_backup))
