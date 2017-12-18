import os.path
import sys
import libvirt
from . import lock
from . import parse
from . import multi
from . import builder
from . import helpers
from . import snapshot


def main():
    args = parse.RestoreArgumentParser()
    conn = libvirt.open()
    if conn is None:
        print("Failed to open connection to libvirt", file=sys.stderr)
        sys.exit(1)
    try:
        dom = conn.lookupByName(args.domain)
    except libvirt.libvirtError:
        print("Domain '{}' not found".format(args.domain))
        sys.exit(1)

    dom_disks = set(parse.Disk.from_domain(dom))
    if len(dom_disks) == 0:
        print("Domain has no disks(!)", file=sys.stderr)
        sys.exit(1)

    disks_to_restore = args.disks and {x for x in dom_disks if x.target in args.disks} or dom_disks
    targets_to_restore = {x.target for x in disks_to_restore}
    if (len(args.disks) > 0 and args.disks != targets_to_restore
        and not helpers.yes("Some disks to be restored don't exist on the domain: " +
                    " ".join(sorted(args.disks - targets_to_restore)) +
                    "\nDo you want to recreate them on the target domain?", False)):
        sys.exit(1)

    archive_disks = {}
    passphrases = multi.get_passphrases([args.archive]) if sys.stdout.isatty() else None
    for entry in helpers.list_entries(args.archive, passphrases=passphrases):
        if entry["type"] == b"-":
            name = entry["bpath"].decode("utf-8")
            if name.split(".")[0] in targets_to_restore:
                if (entry["health"] != b"healthy"
                    and not helpers.yes("The backup copy of disk {} is marked as broken.".format(name.split(".")[0]) +
                                "\nDo you still want to replace it with a possibly corrupted version?", False)):
                    sys.exit(1)
                else:
                    archive_disks[name] = int(entry["size"])
    if len(disks_to_restore) != len(archive_disks):
        archive_tgts = {d.split(".")[0] for d in archive_disks.keys()}
        print("Some disks to be restored don't exist in the archive:",
              *sorted(targets_to_restore - archive_tgts), file=sys.stderr)
        sys.exit(1)

    with lock.DiskLock(disks_to_restore), builder.ArchiveBuilder(disks_to_restore) as archive_dir:
        args.archive.extra_args.extend(archive_disks.keys())
        borg_failed = multi.assimilate(
            archives=[args.archive],
            total_size=args.progress and archive_dir.total_size,
            dir_to_archive=None,
            passphrases=passphrases,
            verb="extract"
        )

    # bug in libvirt python wrapper(?): sometimes it tries to delete
    # the connection object before the domain, which references it
    del dom
    del conn

    sys.exit(borg_failed)
