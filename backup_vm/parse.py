from abc import ABCMeta, abstractmethod
from xml.etree import ElementTree
from textwrap import dedent
import sys
import os
import re
from . import __version__


class Location:
    # see https://github.com/borgbackup/borg/blob/5e2de8b/src/borg/helpers/parseformat.py#L277
    proto = user = _host = port = path = archive = None
    optional_user_re = r"""
        (?:(?P<user>[^@:/]+)@)?
    """
    scp_path_re = r"""
        (?!(:|//|ssh://))
        (?P<path>([^:]|(:(?!:)))+)
        """
    file_path_re = r"""
        (?P<path>(([^/]*)/([^:]|(:(?!:)))+))
        """
    abs_path_re = r"""
        (?P<path>(/([^:]|(:(?!:)))+))
        """
    optional_archive_re = r"""
        (?:
            ::
            (?P<archive>[^/]+)
        )?$"""
    ssh_re = re.compile(r"""
        (?P<proto>ssh)://
        """ + optional_user_re + r"""
        (?P<host>([^:/]+|\[[0-9a-fA-F:.]+\]))(?::(?P<port>\d+))?
        """ + abs_path_re + optional_archive_re, re.VERBOSE)
    file_re = re.compile(r"""
        (?P<proto>file)://
        """ + file_path_re + optional_archive_re, re.VERBOSE)
    scp_re = re.compile(r"""
        (
            """ + optional_user_re + r"""
            (?P<host>([^:/]+|\[[0-9a-fA-F:.]+\])):
        )?
        """ + scp_path_re + optional_archive_re, re.VERBOSE)
    env_re = re.compile(r"""
        (?:::$)
        |
        """ + optional_archive_re, re.VERBOSE)

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
            # avoid that normpath strips away our relative path hack and even
            # makes p absolute
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

    def canonicalize_path(self, cwd=None):
        if self.proto == "file" and not os.path.isabs(self.path):
            if cwd is None:
                cwd = os.getcwd()
            self.path = os.path.normpath(os.path.join(cwd, self.path))

    def __str__(self):
        # https://borgbackup.readthedocs.io/en/stable/usage/general.html#repository-urls
        # the path needs to be re-created instead of returning self.orig because
        # we change values to make paths absolute, etc.
        if self.proto == "file":
            repo = self.path
        elif self.proto == "ssh":
            _user = self.user + "@" if self.user is not None else ""
            if self.port is not None:
                # URI form needs "./" prepended to relative dirs
                if os.path.isabs(self.path):
                    _path = self.path
                else:
                    _path = os.path.join(".", self.path)
                repo = "ssh://{}{}:{}/{}".format(_user, self._host, self.port, _path)
            else:
                repo = "{}{}:{}".format(_user, self._host, self.path)
        if self.archive is not None:
            return repo + "::" + self.archive
        else:
            return repo

    def __hash__(self):
        return hash(str(self))


class Disk:

    """Holds information about a single disk on a libvirt domain.

    Attributes:
        xml: The original XML element representing the disk.
        format: The format of the disk image (qcow2, raw, etc.)
        target: The block device name on the guest (sda, xvdb, etc.)
        type: The type of storage backing the disk (file, block, etc.)
        path: The location of the disk storage (image file, block device, etc.)
    """

    def __init__(self, xml):
        self.xml = xml
        self.target = xml.find("target").get("dev")
        # sometimes there won't be a source entry, e.g. a cd drive without a
        # virtual cd in it
        if xml.find("source") is not None:
            self.type, self.path = next(iter(xml.find("source").attrib.items()))
        else:
            self.type = self.path = None
        # apparently in some cd drives created by virt-manager, <driver> can
        # also be completely missing:
        # https://github.com/milkey-mouse/backup-vm/issues/11#issuecomment-351478233
        if xml.find("driver") is not None:
            self.format = xml.find("driver").attrib["type"]
        else:
            self.format = "unknown"

    def __repr__(self):
        if self.type == "file":
            type = "file"
        elif self.type == "dev":
            type = "block device"
        else:
            type = "unknown type"

        return "<{} ({}) ({} format)>".format(self.path, type, self.format)

    @classmethod
    def get_disks(cls, dom):
        """Generates a list of Disks representing the disks on a libvirt domain.

        Args:
            dom: A libvirt domain object.

        Yields:
            Disk objects representing each disk on the domain.
        """
        tree = ElementTree.fromstring(dom.XMLDesc(0))
        yield from {d for d in map(cls, tree.findall("devices/disk")) if d.type is not None}


# TODO: reimplement this mess with getopt (argparse doesn't support --borg-args stuff)
class ArgumentParser(metaclass=ABCMeta):

    """Base class for backup-vm parsers.

    Parses arguments common to all scripts in the backup-vm package (with
    --borg-args, multiple archive locations, etc.).
    """

    def __init__(self, default_name, args=sys.argv):
        try:
            self.prog = os.path.basename(args[0])
        except Exception:
            self.prog = default_name
        self.progress = sys.stdout.isatty()
        self.disks = set()
        self.archives = []
        self.parse_args(args[1:])

    def parse_arg(self, arg):
        """Parses a single argument.

        Args:
            arg: A string representing a single argument.

        Returns:
            True if the argument was processed, False if it was not recognized
        """
        if arg in {"-h", "--help"}:
            self.help()
            sys.exit()
        elif arg in {"-v", "--version"}:
            self.version()
            sys.exit()
        l = Location.try_location(arg)
        if l is not None and l.path is not None and l.archive is not None and \
                (l.proto == "file" or l._host is not None):
            self.parsing_borg_args = False
            l.canonicalize_path()
            self.archives.append(l)
        elif arg == "--borg-args":
            if len(self.archives) == 0:
                self.error("--borg-args must come after an archive path")
            else:
                self.parsing_borg_args = True
        elif self.parsing_borg_args:
            self.archives[-1].extra_args.append(arg)
        elif arg in {"-p", "--progress"}:
            self.progress = True
        else:
            return False
        return True

    def parse_args(self, args):
        if len(args) == 0:
            self.help()
            sys.exit(2)
        self.parsing_borg_args = False
        for arg in args:
            if arg.startswith("-") and not arg.startswith("--"):
                for c in arg[1:]:
                    if not self.parse_arg("-" + c):
                        self.error("unrecognized argument: '-{}'".format(c))
            else:
                if not self.parse_arg(arg):
                    self.error("unrecognized argument: '{}'".format(arg))
        if len(self.archives) == 0:
            self.error("at least one archive path is required")

    def error(self, msg):
        self.help(short=True)
        print(self.prog + ": error: " + msg, file=sys.stderr)
        sys.exit(2)

    @abstractmethod
    def help(self, short=False):
        pass

    def version(self):
        print(self.prog, __version__)


class MultiArgumentParser(ArgumentParser):

    """Argument parser for borg-multi.

    Parses common arguments (--borg-args, multiple archive locations, etc.) as
    well as those of borg-multi (--borg-cmd).
    """

    def __init__(self, default_name="borg-multi", args=sys.argv):
        self.command = "create"
        self.dir = "."
        super().__init__(default_name, args)

    def parse_arg(self, arg):
        if self.command is None:
            self.command = arg
        elif self.dir is None:
            self.dir = arg
        elif super().parse_arg(arg):
            return True
        elif arg in {"-c", "--borg-cmd"}:
            self.command = None
        elif arg.startswith("-c"):
            self.command = arg[2:]
        elif arg.startswith("--borg-cmd="):
            try:
                self.command = arg.split("=")[1]
            except IndexError:
                self.command = None
        elif arg in {"-l", "--path"}:
            self.dir = None
        elif arg.startswith("-l"):
            self.dir = arg[2:]
        elif arg.startswith("--path="):
            try:
                self.dir = arg.split("=")[1]
            except IndexError:
                self.dir = None
        else:
            return False
        return True

    def parse_args(self, args):
        super().parse_args(args)
        if self.command is None:
            self.error("--borg-args must precede a borg subcommand")
        elif len(self.archives) == 0:
            self.error("the following arguments are required: archive")

    def help(self, short=False):
        print(dedent("""
            usage: {} [-hpv] [--path PATH] [--borg-cmd SUBCOMMAND]
                archive [--borg-args ...] [archive [--borg-args ...] ...]
        """.format(self.prog).lstrip("\n")))
        if not short:
            print(dedent("""
            Batch multiple borg commands into one.

            positional arguments:
              archive          a borg archive path (same format as borg create)

            optional arguments:
              -h, --help       show this help message and exit
              -v, --version    show version of the backup-vm package
              -l, --path       path for borg to archive (default: .)
              -p, --progress   force progress display even if stdout isn't a tty
              -c, --borg-cmd   alternate borg subcommand to run (default: create)
              --borg-args ...  extra arguments passed straight to borg
            """).strip("\n"))


class BackupArgumentParser(ArgumentParser):

    """Argument parser for backup-vm.

    Parses common arguments (--borg-args, multiple archive locations, etc.) as
    well as those of backup-vm (domain).
    """

    def __init__(self, default_name="backup-vm", args=sys.argv):
        self.domain = None
        super().__init__(default_name, args)

    def parse_arg(self, arg):
        if not super().parse_arg(arg):
            if self.domain is None:
                self.domain = arg
            else:
                self.disks.add(arg)
        return True

    def parse_args(self, args):
        super().parse_args(args)
        if self.domain is None or len(self.archives) == 0:
            self.error("the following arguments are required: domain, archive")

    def help(self, short=False):
        print(dedent("""
            usage: {} [-hpv] domain [disk [disk ...]] archive
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
              -v, --version    show version of the backup-vm package
              -p, --progress   force progress display even if stdout isn't a tty
              --borg-args ...  extra arguments passed straight to borg
            """).strip("\n"))
