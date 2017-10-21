#!/usr/bin/env python3

from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from itertools import chain
from textwrap import dedent
from select import select
from time import sleep
import subprocess
import codecs
import json
import sys
import os
import re
import libvirt


class Location:
    # https://github.com/borgbackup/borg/blob/5e2de8b/src/borg/helpers/parseformat.py#L277
    proto = user = _host = port = path = archive = None
    optional_user_re = r"""(?:(?P<user>[^@:/]+)@)?"""
    scp_path_re = r"""(?!(:|//|ssh://))(?P<path>([^:]|(:(?!:)))+)"""
    file_path_re = r"""(?P<path>(([^/]*)/([^:]|(:(?!:)))+))"""
    abs_path_re = r"""(?P<path>(/([^:]|(:(?!:)))+))"""
    optional_archive_re = r"""(?:::(?P<archive>[^/]+))?$"""
    ssh_re = re.compile(r"""(?P<proto>ssh)://""" + optional_user_re +
                        r"""(?P<host>([^:/]+|\[[0-9a-fA-F:.]+\]))(?::(?P<port>\d+))?""" + abs_path_re + optional_archive_re, re.VERBOSE)
    file_re = re.compile(r"""(?P<proto>file)://""" + file_path_re + optional_archive_re, re.VERBOSE)
    scp_re = re.compile(r"""(""" + optional_user_re +
                        r"""(?P<host>([^:/]+|\[[0-9a-fA-F:.]+\])):)?""" + scp_path_re + optional_archive_re, re.VERBOSE)
    env_re = re.compile(r"""(?:::$)|""" + optional_archive_re, re.VERBOSE)

    def __init__(self, text=''):
        self.orig = text
        if not self.parse(self.orig):
            raise ValueError('Location: parse failed: %s' % self.orig)

    def parse(self, text):
        # text = replace_placeholders(text)
        valid = self._parse(text)
        if valid:
            return True
        m = self.env_re.match(text)
        if not m:
            return False
        repo = os.environ.get('BORG_REPO')
        if repo is None:
            return False
        valid = self._parse(repo)
        if not valid:
            return False
        self.archives = m.group('archive')
        return True

    def _parse(self, text):
        def normpath_special(p):
            # avoid that normpath strips away our relative path hack and even makes p absolute
            relative = p.startswith('/./')
            p = os.path.normpath(p)
            return ('/.' + p) if relative else p

        m = self.ssh_re.match(text)
        if m:
            self.proto = m.group('proto')
            self.user = m.group('user')
            self._host = m.group('host')
            self.port = m.group('port') and int(m.group('port')) or None
            self.path = normpath_special(m.group('path'))
            self.archives = m.group('archive')
            return True
        m = self.file_re.match(text)
        if m:
            self.proto = m.group('proto')
            self.path = normpath_special(m.group('path'))
            self.archives = m.group('archive')
            return True
        m = self.scp_re.match(text)
        if m:
            self.user = m.group('user')
            self._host = m.group('host')
            self.path = normpath_special(m.group('path'))
            self.archives = m.group('archive')
            self.proto = self._host and 'ssh' or 'file'
            return True
        return False

    @classmethod
    def is_location(cls, text):
        try:
            loc = Location(text)
        except ValueError:
            return False
        return loc.path is not None and loc.archives is not None and (loc.proto == "file" or loc._host is not None)


class ArgumentParser:

    def __init__(self, args):
        self.prog = os.path.basename(args[0]) if len(args) > 0 else "backup-vm"
        self.domain = None
        self.memory = False
        self.disks = set()
        self.archives = []
        self.parse_args(args)

    def parse_args(self, args):
        if len(args) == 1:
            self.help()
            sys.exit(2)
        parsing_borg_args = False
        for arg in args[1:]:
            if arg in {"-h", "--help"}:
                self.help()
                sys.exit()
            elif Location.is_location(arg):
                parsing_borg_args = False
                if Location(arg).proto == "file":
                    # make relative archive paths absolute beacuse we
                    # will change directories before starting borg
                    parts = arg.split(":")
                    parts[0] = os.path.abspath(parts[0])
                    self.archives.append([":".join(parts)])
                else:
                    self.archives.append([arg])
            elif arg == "--borg-args":
                if len(self.archives) == 0:
                    self.error("--borg-args must come after an archive path")
                else:
                    parsing_borg_args = True
            elif parsing_borg_args:
                self.archives[-1].append(arg)
            elif arg in {"-m", "--memory"}:
                self.memory = True
            elif self.domain is None:
                self.domain = arg
            else:
                self.disks.add(arg)
        if self.domain is None or len(self.archives) == 0:
            self.error("the following arguments are required: domain, archive")
            sys.exit(2)

    def error(self, msg):
        self.help()
        print(self.prog + ": error: " + msg, file=sys.stderr)
        sys.exit(2)

    def help(self, short=False):
        print(dedent("""
            usage: {} [-h] [-m] domain [disk [disk ...]]
                archive [--borg-args ...] [archive [--borg-args ...] ...]
        """.format(self.prog).lstrip("\n")))
        if not short:
            print(dedent("""
            Back up a libvirt-based VM using borg.

            positional arguments:
              domain           libvirt domain to back up
              disk             a domain block device to back up (default: all disks)
              archive          a borg archive path (same format as borg create)

            optional arguments:
              -h, --help       show this help message and exit
              -m, --memory     (experimental) snapshot the memory state as well
              --borg-args ...  extra arguments passed straight to borg
            """).strip("\n"))


class Disk:

    def __init__(self, xml):
        self.xml = xml
        self.format = xml.find("driver").attrib["type"]
        self.target = xml.find("target").get("dev")
        # sometimes there won't be a source entry, e.g. a cd drive without a virtual cd in it
        self.type, self.path = next(iter(xml.find("source").attrib.items()), (None, None))
        self.snapshot_path = None
        self.failed = False

    def __repr__(self):
        if self.type == "file":
            return "<" + self.path + " (device)>"
        elif self.type == "dev":
            return "<" + self.path + " (block device)>"
        else:
            return "<" + self.path + " (unknown type)>"


class Snapshot:

    def __init__(self, dom, disks, memory=None):
        self.dom = dom
        self.disks = disks
        self.memory = memory
        self.snapshotted = False
        self._do_snapshot()

    def _do_snapshot(self):
        snapshot_flags = libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_NO_METADATA | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
        if self.memory is None:
            snapshot_flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
        else:
            snapshot_flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_LIVE
        guest_agent_installed = False
        libvirt.ignored_errors = [libvirt.VIR_ERR_OPERATION_INVALID, libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED]
        try:

            self.dom.fsFreeze()
            guest_agent_installed = True
        except libvirt.libvirtError:
            # qemu guest agent is not installed
            pass
        libvirt.ignored_errors = []
        try:
            self.dom.snapshotCreateXML(self._generate_snapshot_xml(), snapshot_flags)
        except libvirt.libvirtError:
            print("Failed to create domain snapshot", file=sys.stderr)
            sys.exit(1)
        finally:
            if guest_agent_installed:
                self.dom.fsThaw()
        self.snapshotted = True

    def _generate_snapshot_xml(self):
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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.snapshotted:
            return False
        disks_to_backup = [x for x in self.disks if x.snapshot_path is not None]
        if self.dom.isActive():
            # the domain is online. we can use libvirt's blockcommit feature
            # to commit the contents & automatically pivot afterwards
            blockcommit_flags = libvirt.VIR_DOMAIN_BLOCK_COMMIT_ACTIVE | libvirt.VIR_DOMAIN_BLOCK_COMMIT_SHALLOW
            for idx, disk in enumerate(disks_to_backup):
                if self.dom.blockCommit(disk.target, None, None, flags=blockcommit_flags) < 0:
                    print("Failed to start block commit for disk '{}'".format(disk.target).ljust(65), file=sys.stderr)
                    disk.failed = True
                try:
                    while True:
                        info = self.dom.blockJobInfo(disk.target, 0)
                        if info is not None:
                            progress = (idx + info["cur"] / info["end"]) / len(disks_to_backup)
                            print("block commit progress ({}): {}%".format(
                                disk.target, int(100 * progress)).ljust(65), end="\u001b[65D")
                        else:
                            print("Failed to query block jobs for disk '{}'".format(
                                disk.target).ljust(65), file=sys.stderr)
                            disk.failed = True
                            break
                        if info["cur"] == info["end"]:
                            break
                        sleep(0.5)
                finally:
                    print("...pivoting...".ljust(65), end="\u001b[65D")
                    if self.dom.blockJobAbort(disk.target, libvirt.VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT) < 0:
                        print("Pivot failed for disk '{}', it may be in an inconsistent state".format(
                            disk.target).ljust(65), file=sys.stderr)
                        disk.failed = True
                    else:
                        os.remove(disk.snapshot_path)
        else:
            # the domain is offline, use qemu-img for offline commit instead.
            # libvirt doesn't support external snapshots as well as internal,
            # hence this workaround
            print("image commit progress: 0%".ljust(65), end="\u001b[65D")
            for idx, disk in enumerate(disks_to_backup):
                try:
                    subprocess.run(["qemu-img", "commit", disk.snapshot_path], stdout=subprocess.DEVNULL, check=True)
                    # restore the original image in domain definition
                    # this is done automatically when pivoting for live commit
                    new_xml = ElementTree.tostring(disk.xml).decode("utf-8")
                    try:
                        self.dom.updateDeviceFlags(new_xml)
                    except libvirt.libvirtError:
                        print("Device flags update failed for disk '{}'".format(disk.target).ljust(65), file=sys.stderr)
                        print("Try replacing the path manually with 'virsh edit'", file=sys.stderr)
                        disk.failed = True
                        continue
                    os.remove(disk.snapshot_path)
                    progress = (idx + 1) / len(disks_to_backup)
                    print("image commit progress ({}): {}%".format(
                        disk.target, int(100 * progress)).ljust(65), end="\u001b[65D")
                except FileNotFoundError:
                    # not very likely as the qemu-img tool is normally installed
                    # along with the libvirt/virsh stuff
                    print("Please install qemu-img to commit changes offline".ljust(65), file=sys.stderr)
                    disk.failed = True
                    break
                except subprocess.CalledProcessError:
                    print("Commit failed for disk '{}'".format(disk.target).ljust(65))
                    disk.failed = True
            print()
        return False


def error_handler(ctx, err):
    if err[0] not in libvirt.ignored_errors:
        print("libvirt: error code {}: {}".format(err[0], err[2]), file=sys.stderr)


def main():
    args = ArgumentParser(sys.argv)

    libvirt.ignored_errors = []
    libvirt.registerErrorHandler(error_handler, None)
    conn = libvirt.open()
    if conn is None:
        print("Failed to open connection to libvirt", file=sys.stderr)
        sys.exit(1)
    try:
        dom = conn.lookupByName(args.domain)
    except libvirt.libvirtError:
        print("Domain '{}' not found".format(args.domain))
        sys.exit(1)

    if args.memory and not dom.isActive():
        print("Domain is shut off, cannot save memory contents", file=sys.stderr)
        args.memory = False

    tree = ElementTree.fromstring(dom.XMLDesc(0))
    disks = [d for d in map(Disk, tree.findall("devices/disk")) if d.type is not None]
    if len(disks) == 0:
        print("Domain has no disks(!)", file=sys.stderr)
        sys.exit(1)

    if not args.disks:
        args.disks = {x.target for x in disks}
    else:
        nonexistent_disks = args.disks - {x.target for x in disks}
        if len(nonexistent_disks) > 0:
            print("Some disks to be backed up don't exist on the domain:", *sorted(nonexistent_disks), file=sys.stderr)
            sys.exit(1)

    for disk in disks:
        if disk.target not in args.disks:
            continue
        filename = args.domain + "-" + disk.target + "-tempsnap.qcow2"
        if disk.type == "dev":
            # we probably can't write the temporary snapshot to the same directory
            # as the original disk, so use the default libvirt images directory
            disk.snapshot_path = os.path.join("/var/lib/libvirt/images", filename)
        else:
            disk.snapshot_path = os.path.join(os.path.dirname(disk.path), filename)

    if args.memory:
        memory = os.path.join(tmpdir, "memory.bin")
    else:
        memory = None
    with TemporaryDirectory() as tmpdir, Snapshot(dom, disks, memory):
        total_size = 0
        for disk in disks:
            if disk.target in args.disks:
                realpath = os.path.realpath(disk.path)
                with open(realpath) as f:
                    f.seek(0, os.SEEK_END)
                    total_size += f.tell()
                linkpath = os.path.join(tmpdir, disk.target + "." + disk.format)
                # following symlinks for --read-special is still broken :(
                # when issue gets fixed should switch to symlinks:
                # https://github.com/borgbackup/borg/issues/1215
                # os.symlink(realpath, linkpath)
                with open(linkpath, "w") as f:
                    pass  # simulate 'touch'
                subprocess.run(["mount", "--bind", realpath, linkpath], check=True)

        try:
            if sys.stdout.isatty():
                # borg <1.1 doesn't support --json for the progress bar
                version_bytes = subprocess.run(["borg", "--version"], stdout=subprocess.PIPE, check=True).stdout
                borg_version = [*map(int, version_bytes.decode("utf-8").split(" ")[1].split("."))]
                if borg_version[0] < 1 or borg_version[1] < 1:
                    print("You are using an old version of borg, progress indication is disabled", file=sys.stderr)
                    check_progress = False
                else:
                    check_progress = True
            else:
                check_progress = False

            os.chdir(tmpdir)
            borg_processes = []
            for idx, _archive in enumerate(args.archives):
                archive, *extra_args = _archive
                if check_progress:
                    proc = subprocess.Popen(["borg", "create", archive, ".", "--read-special", "--progress",
                                             "--json", *extra_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    proc.stdout = codecs.getreader("utf-8")(proc.stdout)
                    proc.stderr = codecs.getreader("utf-8")(proc.stderr)
                    proc.ignore_stderr = False
                else:
                    proc = subprocess.Popen(["borg", "create", archive, ".", "--read-special", *args.borg_args])
                proc.progress = 0
                borg_processes.append(proc)

            borg_failed = False
            if check_progress:
                print("backup progress: 0%".ljust(25), end="\u001b[25D")
                _processes = borg_processes[:]
                while _processes:
                    rlist = select([*chain.from_iterable((p.stdout, p.stderr) for p in _processes)], [], [])[0]
                    for f in rlist:
                        try:
                            p = next(p for p in _processes if p.stdout == f)
                            try:
                                update = json.load(f)
                                p.progress = update["archive"]["stats"]["original_size"] / total_size
                            except json.decoder.JSONDecodeError:
                                # todo: buffer output and use some sort of streaming
                                # decoder (current code could read half a json object
                                # if the buffer gets full)
                                pass
                            except KeyError:
                                # if the user enables other flags that output json with
                                # --borg-args (such as --stats), it can read valid JSON
                                # that doesn't include the keys it's looking for
                                pass
                        except StopIteration:
                            p = next(p for p in _processes if p.stderr == f)
                            line = f.readline().rstrip("\n")
                            if line.startswith("0 B O 0 B C 0 B D 0 N"):
                                # "magic string" of first progress update: if
                                # we've made it this far we haven't errored out
                                # on something trivial like an invalid repo name
                                p.ignore_stderr = True
                            elif not p.ignore_stderr or p.poll() is not None:
                                print(line, file=sys.stderr)

                    for p in _processes[:]:
                        if p.poll() is not None:
                            p.stdout.read()
                            p.stdout.close()
                            if p.returncode != 0:
                                borg_failed = True
                            _processes.remove(p)

                    progress = int(sum(p.progress for p in borg_processes) / len(borg_processes) * 100)
                    print("backup progress: {}%".format(progress).ljust(25), end="\u001b[25D")
                print()
            else:
                for p in borg_processes:
                    p.wait()
        finally:
            for disk in disks:
                if disk.target in args.disks:
                    subprocess.run(["umount", os.path.join(tmpdir, disk.target + "." + disk.format)], check=True)

    # bug in libvirt python wrapper(?): sometimes it tries to delete
    # the connection object before the domain, which references it
    del dom
    del conn

    if borg_failed or any(disk.failed for disk in disks):
        sys.exit(1)
    else:
        sys.exit()


main()
