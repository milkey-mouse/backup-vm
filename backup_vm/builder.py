import subprocess
import tempfile
import os.path


class ArchiveBuilder(tempfile.TemporaryDirectory):

    """Creates the folder to be turned into a VM backup.

    Creates a temporary folder populated with symlinks to each disk to backup.
    Essentially lays out the contents of the archive to be created.

    Attributes:
        name: The path of the temporary directory.
        total_size: The total size of every disk linked to in the directory.
    """

    def __init__(self, disks, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.total_size = 0
        self.disks = disks
        self.old_cwd = os.getcwd()
        os.chdir(self.name)

    def __enter__(self):
        for disk in self.disks:
            realpath = os.path.realpath(disk.path)
            if self.total_size is not None:
                try:
                    with open(realpath) as f:
                        # add size of disk to total
                        f.seek(0, os.SEEK_END)
                        self.total_size += f.tell()
                except (PermissionError, OSError):
                    self.total_size = None
            linkpath = disk.target + "." + disk.format
            with open(linkpath, "w") as f:
                # simulate 'touch'
                pass
            # following symlinks for --read-special is still broken :(
            # when issue gets fixed should switch to symlinks:
            # https://github.com/borgbackup/borg/issues/1215
            subprocess.run(["mount", "--bind", realpath, linkpath], check=True)
        return self

    def cleanup(self):
        for disk in self.disks:
            linkpath = disk.target + "." + disk.format
            subprocess.run(["umount", linkpath], check=True)
        os.chdir(self.old_cwd)
        return super().cleanup()
