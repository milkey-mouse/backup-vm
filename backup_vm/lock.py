import fcntl


class DiskLock():

    def __init__(self, dom, *disks):
        self.disks = disks

    def __enter__(self):
        pass

    def __exit__(self, *args):
        return False
