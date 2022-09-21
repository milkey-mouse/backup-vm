from distutils.version import LooseVersion
from base64 import b64encode
from getpass import getpass
from pty import openpty
from copy import copy
import subprocess
import selectors
import termios
import fcntl
import json
import sys
import pty
import os
from . import parse


def get_passphrases(archives):
    """Prompts the user for their archive passphrases.

    Checks for archives that won't open without a (non-blank, non-random)
    BORG_PASSPHRASE and prompts the user for their passphrases.

    Args:
        archives: A list of Location objects to check the repositories of.

    Returns:
        A dictionary mapping archives to their (purported) passphrases. The
        entered passphrases are not checked to actually open the archives.
    """
    passphrases = {}
    env = os.environ.copy()
    for archive in archives:
        repo = copy(archive)
        repo.archive = None
        # check if we need a password as recommended by the docs:
        # https://borgbackup.readthedocs.io/en/stable/internals/frontends.html#passphrase-prompts
        if len({"BORG_PASSPHRASE", "BORG_PASSCOMMAND", "BORG_NEWPASSPHRASE"} - set(env)) == 3:
            # generate random password that would be incorrect were it needed
            env["BORG_PASSPHRASE"] = b64encode(os.urandom(16)).decode("utf-8")
        with subprocess.Popen(["borg", "list", str(repo)], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env) as proc:
            # manually close stdin instead of /dev/null so borg knows it won't get input
            proc.stdin.close()
            proc.stdin = None
            err = proc.communicate(input)[1].decode("utf-8").rstrip("\n").split("\n")[-1]
            if proc.poll() != 0:
                # exact error message changes between borg versions
                if err.startswith("passphrase supplied") and err.endswith("is incorrect."):
                    passphrases[archive] = getpass("Enter passphrase for key {!s}: ".format(repo))
    return passphrases


def log(name, msg, *args, file=sys.stderr, end="\n", **kwargs):
    """Logs a string to a file, prepending a "tag" to each line.

    Logs a string to a file (by default stderr), with a "tag" added to the
    beginning of each line, in the format of this example::

        [repo::archive] Hello world!

    Args:
        name: The text to be put in the "tag" part of each line.
        msg: The string to be tagged & logged.
        end: The ending of the last line printed.

    Any other arguments passed will be passed onto print().
    """
    for l in msg[:-1]:
        print("[{}] {}".format(name, l), file=file, **kwargs)
    print("[{}] {}".format(name, msg[-1]), file=file, end=end, **kwargs)


def process_line(p, line, total_size=None, prompt_answers={}):
    """Process a line coming from a borg process.

    Processes JSON emitted by a borg process with --log-json turned on. The
    lines are cached, so 1 line does not have to equal 1 JSON message.

    Args:
        p: The process the line came from (with some extra properties added to
            the Popen object).
        line: The line read from the process's stdout or stderr. If it contains
            progress information, update the stored progress value. If it is a
            prompt for the user, ask for and return the answer (& cache it for
            later.) If it is a log message or some other non-JSON, print it out.
        total_size: The total size of all files being backed up. This can be set
            to None to disable progress calculation.
        prompt_answers: A dictionary of previous answers from users' prompts.
            Prompts with msgids in the dictionary will be automatically answered
            with the value given (ostensibly from an earlier prompt).
    """
    if len(p.json_buf) > 0 or line.startswith("{"):
        p.json_buf.append(line)
    if len(p.json_buf) > 0 and line.endswith("}"):
        try:
            msg = json.loads("\n".join(p.json_buf))
            p.json_buf = []
            if msg["type"] == "archive_progress" and msg["finished"]:
                log(p.archive.orig, str("borg finished as " + str(msg["finished"])).split("\n"))
            elif msg["type"] == "archive_progress" and total_size is not None:
                p.progress = msg["original_size"] / total_size
            elif msg["type"] == "log_message":
                log(p.archive.orig, msg["message"].split("\n"))
            elif msg["type"].startswith("question"):
                if "msgid" in msg:
                    prompt_id = msg["msgid"]
                elif "message" in msg:
                    prompt_id = msg["message"]
                else:
                    raise ValueError("No msgid or message for prompt")
                if msg.get("is_prompt", False) or msg["type"].startswith("question_prompt"):
                    if prompt_id not in prompt_answers:
                        log(p.archive.orig, msg["message"].split("\n"), end="")
                        try:
                            prompt_answers[prompt_id] = input()
                            print(prompt_answers[prompt_id], file=p.stdin, flush=True)
                        except EOFError:
                            p.stdin.close()
                elif not msg["type"].startswith("question_accepted"):
                    log(p.archive.orig, msg["message"].split("\n"))
        except json.decoder.JSONDecodeError:
            log(p.archive.orig, p.json_buf)
            p.json_buf = []
    elif line.startswith("Enter passphrase for key "):
        log(p.archive.orig, [line], end="")
        passphrase = getpass("")
        print(passphrase, file=p.stdin, flush=True)
        print("", file=sys.stderr)
    elif line != "":
        # line is not json?
        log(p.archive.orig, [line])
    # TODO: process password here for efficiency & simplicity


def get_borg_version():
    """
    Get the version of the system borg.

    Returns:
        The version of the system borg as a distutils.version.LooseVersion (for
        easy comparison with other versions).
    """
    version_bytes = subprocess.run(["borg", "--version"], stdout=subprocess.PIPE, check=True).stdout
    return LooseVersion(version_bytes.decode("utf-8").split(" ")[1])


def assimilate(archives, total_size=None, dir_to_archive=".", passphrases=None, verb="create"):
    """
    Run and manage multiple `borg create` commands.

    Args:
        archives: A list containing Location objects for the archives to create.
        total_size: The total size of all files being backed up. As borg
            normally only makes one pass over the data, it can't calculate
            percentages on its own. Setting this to None disables progress
            calculation.
        dir_to_archive: The directory to archive. Defaults to the current
            directory.

    Returns:
        A boolean indicating if any borg processes failed (True = failed).
    """

    if dir_to_archive is None:
        dir_to_archive = []
    else:
        dir_to_archive = [dir_to_archive]

    if passphrases is None:
        passphrases = get_passphrases(archives) if sys.stdout.isatty() else {}

    if get_borg_version() < LooseVersion("1.1.0"):
        # borg <1.1 doesn't support --log-json for the progress display
        print("You are using an old version of borg, progress indication is disabled", file=sys.stderr)
        recent_borg = False
        progress = False
    else:
        recent_borg = True
        progress = total_size is not None

    borg_processes = []
    borg_failed = False
    try:
        with selectors.DefaultSelector() as sel:
            for idx, archive in enumerate(archives):
                if progress:
                    archive.extra_args.append("--progress")
                if recent_borg:
                    archive.extra_args.append("--log-json")
                env = os.environ.copy()
                passphrase = passphrases.get(archive, os.environ.get("BORG_PASSPHRASE"))
                if passphrase is not None:
                    env["BORG_PASSPHRASE"] = passphrase
                master, slave = openpty()
                settings = termios.tcgetattr(master)
                settings[3] &= ~termios.ECHO
                termios.tcsetattr(master, termios.TCSADRAIN, settings)
                proc = subprocess.Popen(["borg", verb, str(archive), *dir_to_archive, *archive.extra_args], env=env,
                                        stdout=slave, stderr=slave, stdin=slave, close_fds=True, start_new_session=True)
                fl = fcntl.fcntl(master, fcntl.F_GETFL)
                fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                proc.stdin = os.fdopen(master, "w")
                proc.stdout = os.fdopen(master, "r")
                proc.archive = archive
                proc.json_buf = []
                proc.progress = 0
                borg_processes.append(proc)
                sel.register(proc.stdout, selectors.EVENT_READ, data=proc)

            if progress:
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
                if progress:
                    total_progress = sum(p.progress for p in borg_processes)
                    print("backup progress: {}%".format(
                        int(total_progress / len(borg_processes) * 100)).ljust(25), end="\u001b[25D")
            if progress:
                print()
    finally:
        for p in borg_processes:
            if p.poll() is not None:
                p.kill()
                try:
                    p.communicate()
                except (ValueError, OSError):
                    p.wait()
    return borg_failed


def main():
    args = parse.MultiArgumentParser()
    if args.command != "create" and "--path" not in sys.argv[1:]:
        # path needs to be explicitly specified to be included in command
        # if the verb is not the default
        args.dir = None
    return assimilate(args.archives, dir_to_archive=args.dir, verb=args.command)
