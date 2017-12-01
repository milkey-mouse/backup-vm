from xml.etree import ElementTree
import subprocess
import time
import sys
import os
import libvirt


def error_handler(ctx, err):
    if err[0] not in libvirt.ignored_errors:
        print("libvirt: error code {0}: {2}".format(*err), file=sys.stderr)


libvirt.ignored_errors = []
libvirt.registerErrorHandler(error_handler, None)


class Snapshot:

    def __init__(self, dom, disks, memory=None, progress=True):
        self.dom = dom
        self.disks = disks
        self.memory = memory
        self.progress = progress
        self.snapshotted = False
        self._do_snapshot()

    def _do_snapshot(self):
        snapshot_flags = libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_NO_METADATA \
            | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
        if self.memory is None:
            snapshot_flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
        else:
            snapshot_flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_LIVE
        libvirt.ignored_errors = [
            libvirt.VIR_ERR_OPERATION_INVALID,
            libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED
        ]
        try:
            self.dom.fsFreeze()
            guest_agent_installed = True
        except libvirt.libvirtError:
            guest_agent_installed = False
        libvirt.ignored_errors = []
        try:
            snapshot_xml = self.generate_snapshot_xml()
            self.dom.snapshotCreateXML(snapshot_xml, snapshot_flags)
        except libvirt.libvirtError:
            print("Failed to create domain snapshot", file=sys.stderr)
            sys.exit(1)
        finally:
            if guest_agent_installed:
                self.dom.fsThaw()
        self.snapshotted = True

    def generate_snapshot_xml(self):
        root_xml = ElementTree.Element("domainsnapshot")
        name_xml = ElementTree.SubElement(root_xml, "name")
        name_xml.text = self.dom.name() + "-tempsnap"
        desc_xml = ElementTree.SubElement(root_xml, "description")
        desc_xml.text = "Temporary snapshot used while backing up " + self.dom.name()
        memory_xml = ElementTree.SubElement(root_xml, "memory")
        if self.memory is not None:
            memory_xml.attrib["snapshot"] = "external"
            memory_xml.attrib["file"] = self.memory
        else:
            memory_xml.attrib["snapshot"] = "no"
        disks_xml = ElementTree.SubElement(root_xml, "disks")
        for disk in self.disks:
            disk_xml = ElementTree.SubElement(disks_xml, "disk")
            if disk.snapshot_path is not None:
                disk_xml.attrib["name"] = disk.path
                source_xml = ElementTree.SubElement(disk_xml, "source")
                source_xml.attrib["file"] = disk.snapshot_path
                driver_xml = ElementTree.SubElement(disk_xml, "driver")
                driver_xml.attrib["type"] = "qcow2"
            else:
                disk_xml.attrib["name"] = disk.target
                disk_xml.attrib["snapshot"] = "no"
        return ElementTree.tostring(root_xml).decode("utf-8")

    def blockcommit(self, disks):
        for idx, disk in enumerate(disks):
            for commit_try in range(3):
                disk.failed = False
                if self.dom.blockCommit(
                    disk.target, None, None,
                    flags=libvirt.VIR_DOMAIN_BLOCK_COMMIT_ACTIVE
                        | libvirt.VIR_DOMAIN_BLOCK_COMMIT_SHALLOW) < 0:
                    print("Failed to start block commit for disk '{}'".format(
                        disk.target).ljust(65), file=sys.stderr)
                    disk.failed = True
                try:
                    while True:
                        info = self.dom.blockJobInfo(disk.target, 0)
                        if info is not None and self.progress:
                            progress = (idx + info["cur"] / info["end"]) / len(disks)
                            print("block commit progress ({}): {}%".format(
                                disk.target, int(100 * progress)).ljust(65), end="\u001b[65D")
                        elif info is None:
                            print("Failed to query block jobs for disk '{}'".format(
                                disk.target).ljust(65), file=sys.stderr)
                            disk.failed = True
                            break
                        if info["cur"] == info["end"]:
                            break
                        time.sleep(1)
                    if not disk.failed:
                        break
                finally:
                    if self.progress:
                        print("...pivoting...".ljust(65), end="\u001b[65D")
                    if self.dom.blockJobAbort(disk.target, libvirt.VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT) < 0:
                        suffix = "retrying..." if commit_try != 2 else "it may be in an inconsistent state"
                        print("Pivot failed for disk '{}', {}".format(disk.target, suffix).ljust(65), file=sys.stderr)
                        disk.failed = True
                        time.sleep(5)
                    else:
                        try:
                            os.remove(disk.snapshot_path)
                        except PermissionError:
                            print("Couldn't delete snapshot image '{}', please run as root".format(
                                disk.snapshot_path).ljust(65), file=sys.stderr)
                        break

    def offline_commit(self, disks):
        if self.progress:
            print("image commit progress: 0%".ljust(65), end="\u001b[65D")
        else:
            print("committing disk images")
        for idx, disk in enumerate(disks):
            for commit_try in range(3):
                disk.failed = False
                try:
                    subprocess.run(["qemu-img", "commit", disk.snapshot_path],
                                   stdout=subprocess.DEVNULL, check=True)
                    # restore the original image in domain definition
                    # this is done automatically when pivoting for live commit
                    new_xml = ElementTree.tostring(disk.xml).decode("utf-8")
                    try:
                        self.dom.updateDeviceFlags(new_xml)
                    except libvirt.libvirtError:
                        print("Device flags update failed for disk '{}'".format(
                            disk.target).ljust(65), file=sys.stderr)
                        print("Try replacing the path manually with 'virsh edit'", file=sys.stderr)
                        disk.failed = True
                        continue
                    try:
                        os.remove(disk.snapshot_path)
                    except PermissionError:
                        print("Couldn't delete snapshot image '{}', please run as root".format(
                            disk.snapshot_path).ljust(65), file=sys.stderr)
                    if self.progress:
                        progress = (idx + 1) / len(disks)
                        print("image commit progress ({}): {}%".format(
                            disk.target, int(100 * progress)).ljust(65), end="\u001b[65D")
                    break
                except FileNotFoundError:
                    # not very likely as the qemu-img tool is normally installed
                    # along with the libvirt/virsh stuff
                    print("Install qemu-img to commit changes offline".ljust(65), file=sys.stderr)
                    disk.failed = True
                    return
                except subprocess.CalledProcessError:
                    print("Commit failed for disk '{}'{}".format(disk.target,
                        ", retrying..." if commit_try != 2 else "").ljust(65), file=sys.stderr)
                    disk.failed = True
                    time.sleep(5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.snapshotted:
            return False
        disks_to_backup = [x for x in self.disks if x.snapshot_path is not None]
        if self.dom.isActive():
            # the domain is online. we can use libvirt's blockcommit feature
            # to commit the contents & automatically pivot afterwards
            self.blockcommit(disks_to_backup)
        else:
            # the domain is offline, use qemu-img for offline commit instead.
            # libvirt doesn't support external snapshots as well as internal,
            # hence this workaround
            self.offline_commit(disks_to_backup)
        if self.progress:
            print()
        return False
