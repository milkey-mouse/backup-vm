import subprocess
import os


def yes(question, default=None):
    """Ask the user a yes/no question, optionally with a default answer."""
    while True:
        print(question, " (Y/n): " if default else " (y/N): ", end="", file=sys.stderr)
        answer = input().upper().rstrip("\n")
        if answer in {"Y", "YES", "1"}:
            return True
        elif answer in {"N", "NO", "0"}:
            return False
        elif default is not None:
            return default


def grouper(iterable, n):
    """Collect data into fixed-length chunks or blocks, cutting off remaining elements"""
    args = [iter(iterable)] * n
    return zip(*args)


def list_entries(archive, properties=["type", "size", "health", "bpath"], passphrases=None):
    """Wrapper around 'borg list' that returns dicts with keys from '--format'."""
    env = os.environ.copy()
    if isinstance(passphrases, str):
        env["BORG_PASSPHRASE"] = passphrases
    elif passphrases is not None and archive in passphrases:
        env["BORG_PASSPHRASE"] = passphrases[archive]
    format_string = "{NUL}".join("{%s}" % p for p in properties) + "{NUL}"
    p = subprocess.run(["borg", "list", "--format", format_string, str(archive)],
                       env=env, stdout=subprocess.PIPE, check=True)
    for entry in grouper(p.stdout.split(b"\x00"), len(properties)):
        yield {key: entry[i] for i, key in enumerate(properties)}
