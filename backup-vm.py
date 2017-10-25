#!/usr/bin/env python3

from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from base64 import b64encode
from textwrap import dedent
from getpass import getpass
from pty import openpty
from time import sleep
import subprocess
import selectors
import termios
import fcntl
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

    def __init__(self, text=""):
        self.orig = text
        self.extra_args = []
        if not self.parse(self.orig):
            raise ValueError("Location: parse failed: %s" % self.orig)

    def parse(self, text):
        # text = replace_placeholders(text)
        valid = self._parse(text)
        if valid:
            return True
        m = self.env_re.match(text)
        if not m:
            return False
        repo = os.environ.get("BORG_REPO")
        if repo is None:
            return False
        valid = self._parse(repo)
        if not valid:
            return False
        self.archive = m.group("archive")
        return True

    def _parse(self, text):
        def normpath_special(p):
            # avoid that normpath strips away our relative path hack and even makes p absolute
            relative = p.startswith("/./")
            p = os.path.normpath(p)
            return ("/." + p) if relative else p

        m = self.ssh_re.match(text)
        if m:
            self.proto = m.group("proto")
            self.user = m.group("user")
            self._host = m.group("host")
            self.port = m.group("port") and int(m.group("port")) or None
            self.path = normpath_special(m.group("path"))
            self.archive = m.group("archive")
            return True
        m = self.file_re.match(text)
        if m:
            self.proto = m.group("proto")
            self.path = normpath_special(m.group("path"))
            self.archive = m.group("archive")
            return True
        m = self.scp_re.match(text)
        if m:
            self.user = m.group("user")
            self._host = m.group("host")
            self.path = normpath_special(m.group("path"))
            self.archive = m.group("archive")
            self.proto = self._host and "ssh" or "file"
            return True
        return False

    @classmethod
    def try_location(cls, text):
        try:
            return Location(text)
        except ValueError:
            return None
        return loc.path is not None and loc.archive is not None and (loc.proto == "file" or loc._host is not None)

    def canonicalize_path(self, cwd=None):
        if self.proto == "ssh" or os.path.isabs(self.path):
            return
        if cwd is None:
            cwd = os.getcwd()
        self.path = os.path.normpath(os.path.join(cwd, self.path))

    def __str__(self):
        # http://borgbackup.readthedocs.io/en/stable/usage/general.html#repository-urls
        # re-creation needs to be done dynamically instead of returning self.orig because
        # we change values to make paths absolute, etc.
        if self.proto == "file":
            repo = self.path
        elif self.proto == "ssh":
            _user = self.user + "@" if self.user is not None else ""
            if self.port is not None:
                # URI form needs "./" prepended to relative dirs
                _path = os.path.join(".", self.path) if not os.path.isabs(self.path) else self.path
                repo = "ssh://{}{}:{}/{}".format(_user, self._host, self.port, _path)
            else:
                repo = "{}{}:{}".format(_user, self._host, self.path)
        if self.archive is not None:
            return repo + "::" + self.archive
        else:
            return repo

    def __hash__(self):
        return hash(str(self))


class ArgumentParser:

    def __init__(self, args):
        self.prog = os.path.basename(args[0]) if len(args) > 0 else "backup-vm"
        self.domain = None
        self.memory = False
        self.progress = sys.stdout.isatty()
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
            l = Location.try_location(arg)
            if [l, l.path, l.archive].count(None) == 0 and (l.proto == "file" or l._host is not None):
                parsing_borg_args = False
                l.canonicalize_path()
                self.archives.append(l)
            elif arg == "--borg-args":
                if len(self.archives) == 0:
                    self.error("--borg-args must come after an archive path")
                else:
                    parsing_borg_args = True
            elif parsing_borg_args:
                self.archives[-1].extra_args.append(arg)
            elif arg in {"-m", "--memory"}:
                self.memory = True
            elif arg in {"-p", "--progress"}:
                self.progress = True
            elif arg.startswith("-"):
                # handle multiple flags in one arg (e.g. -hmp)
                for c in arg[1:]:
                    if c == "h":
                        self.help()
                        sys.exit()
                    elif c == "m":
                        self.memory = True
                    elif c == "p":
                        self.progress = True
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
            usage: {} [-hmp] domain [disk [disk ...]] archive
                [--borg-args ...] [archive [--borg-args ...] ...]
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
              -p, --progress   force progress display even if stdout isn't a tty
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

    def __init__(self, dom, disks, memory=None, progress=True):
        self.dom = dom
        self.disks = disks
        self.memory = memory
        self.progress = progress
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
                            if self.progress:
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
                    if self.progress:
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
            if self.progress:
                print("image commit progress: 0%".ljust(65), end="\u001b[65D")
            else:
                print("committing disk images")
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
                    if self.progress:
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
            if self.progress:
                print()
        return False


def error_handler(ctx, err):
    if err[0] not in libvirt.ignored_errors:
        print("libvirt: error code {}: {}".format(err[0], err[2]), file=sys.stderr)


def log(p, msg, end="\n"):
    if isinstance(p, subprocess.Popen):
        name = p.archive.orig
    elif isinstance(p, Location):
        name = p.orig
    else:
        name = p
    for l in msg[:-1]:
        print("[{}] {}".format(name, l), file=sys.stderr)
    print("[{}] {}".format(name, msg[-1]), file=sys.stderr, end=end)


prompt_answers = {}


def process_line(p, line, total_size):
    global prompt_answers
    if len(p.json_buf) > 0 or line.startswith("{"):
        p.json_buf.append(line)
    if len(p.json_buf) > 0 and line.endswith("}"):
        try:
            msg = json.loads("\n".join(p.json_buf))
            p.json_buf = []
            if msg["type"] == "archive_progress":
                p.progress = msg["original_size"] / total_size
            elif msg["type"] == "log_message":
                log(p, msg["message"].split("\n"))
            elif msg["type"].startswith("question"):
                if "msgid" in msg:
                    prompt_id = msg["msgid"]
                elif "message" in msg:
                    prompt_id = msg["message"]
                else:
                    raise ValueError("No msgid or message for prompt")
                if msg.get("is_prompt", False) or msg["type"].startswith("question_prompt"):
                    if prompt_id not in prompt_answers:
                        log(p, msg["message"].split("\n"), end="")
                        prompt_answers[prompt_id] = input()
                    print(prompt_answers[prompt_id], file=p.stdin, flush=True)
                elif not msg["type"].startswith("question_accepted"):
                    log(p, msg["message"].split("\n"))
        except json.decoder.JSONDecodeError:
            log(p, p.json_buf)
            p.json_buf = []
    elif line.startswith("Enter passphrase for key "):
        log(p, [line], end="")
        passphrase = getpass("")
        print(passphrase, file=p.stdin, flush=True)
        print("", file=sys.stderr)
    elif line != "":
        # line is not json?
        log(p, [line])
    # TODO: process password here for efficiency & simplicity


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

    passphrases = {}
    if sys.stdout.isatty():
        for archive in args.archives:
            repo = str(archive).split("::")[0]
            # check if we need a password as recommended by the docs:
            # https://borgbackup.readthedocs.io/en/stable/internals/frontends.html#passphrase-prompts
            env = os.environ.copy()
            if len({"BORG_PASSPHRASE", "BORG_PASSCOMMAND", "BORG_NEWPASSPHRASE"} - set(env)) == 3:
                env["BORG_PASSPHRASE"] = b64encode(os.urandom(16)).decode("utf-8")
            with subprocess.Popen(["borg", "list", repo], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env) as proc:
                proc.stdin.close()  # manually close stdin instead of /dev/null so it knows it won't get input
                proc.stdin = None
                err = proc.communicate(input)[1].decode("utf-8").rstrip("\n").split("\n")
                if proc.poll() != 0:
                    # exact error message changes between borg versions
                    if err[-1].startswith("passphrase supplied") and err[-1].endswith("is incorrect."):
                        passphrases[archive] = getpass("Enter passphrase for key {}: ".format(repo.split(":")[0]))
                    else:
                        # the error will re-manifest later (with better I/O formatting), so just ignore it
                        pass

    with TemporaryDirectory() as tmpdir, Snapshot(dom, disks, memory, args.progress):
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
            # borg <1.1 doesn't support --log-json for the progress display
            version_bytes = subprocess.run(["borg", "--version"], stdout=subprocess.PIPE, check=True).stdout
            borg_version = [*map(int, version_bytes.decode("utf-8").split(" ")[1].split("."))]
            if borg_version[0] < 1 or borg_version[1] < 1:
                print("You are using an old version of borg, progress indication is disabled", file=sys.stderr)
                old_borg = True
                args.progress = False
            else:
                old_borg = False

            os.chdir(tmpdir)
            borg_processes = []
            try:
                with selectors.DefaultSelector() as sel:
                    for idx, archive in enumerate(args.archives):
                        if args.progress:
                            archive.extra_args.append("--progress")
                        if not old_borg:
                            archive.extra_args.append("--log-json")
                        env = os.environ.copy()
                        passphrase = passphrases.get(archive, os.environ.get("BORG_PASSPHRASE"))
                        if passphrase is not None:
                            env["BORG_PASSPHRASE"] = passphrase
                        master, slave = openpty()
                        settings = termios.tcgetattr(master)
                        settings[3] &= ~termios.ECHO
                        termios.tcsetattr(master, termios.TCSADRAIN, settings)
                        proc = subprocess.Popen(["borg", "create", str(archive), ".", "--read-special", *archive.extra_args],
                                                stdout=slave, stderr=slave, stdin=slave, close_fds=True, env=env, start_new_session=True)
                        fl = fcntl.fcntl(master, fcntl.F_GETFL)
                        fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                        proc.stdin = os.fdopen(master, "w")
                        proc.stdout = os.fdopen(master, "r")
                        proc.archive = archive
                        proc.json_buf = []
                        proc.progress = 0
                        borg_processes.append(proc)
                        sel.register(proc.stdout, selectors.EVENT_READ, data=proc)

                    borg_failed = False
                    if args.progress:
                        print("backup progress: 0%".ljust(25), end="\u001b[25D", flush=True)
                    else:
                        # give the user some feedback so the program doesn't look frozen
                        print("starting backup", flush=True)
                    while len(sel.get_map()) > 0:
                        for key, mask in sel.select(1):
                            for line in iter(key.fileobj.readline, ""):
                                process_line(key.data, line.rstrip("\n"), total_size)
                        for key in [*sel.get_map().values()]:
                            if key.data.poll() is not None:
                                key.data.wait()
                                key.data.progress = 1
                                if key.data.returncode != 0:
                                    borg_failed = True
                                sel.unregister(key.fileobj)
                        if args.progress:
                            progress = int(sum(p.progress for p in borg_processes) / len(borg_processes) * 100)
                            print("backup progress: {}%".format(progress).ljust(25), end="\u001b[25D")
                    if args.progress:
                        print()
            finally:
                for p in borg_processes:
                    if p.poll() is not None:
                        p.kill()
                        try:
                            p.communicate()
                        except (ValueError, OSError):
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
