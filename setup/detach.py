#!/usr/bin/env python3
"""Daemonize a command so it survives archon's node process-group reaping.

Default: double-fork + setsid. `--supervise` keeps the session/group leader
alive until the command exits, preventing PGID reuse in guarded workflows.
Prints child PID and exact PGID.
Usage: detach.py [--supervise] <logfile> <cwd> <cmd> [args...]
"""
import os
import signal
import subprocess
import sys
import time


def group_has_other_processes(pgid, supervisor_pid):
    try:
        rows = subprocess.check_output(
            ["ps", "-axo", "pid=,pgid="], text=True,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        # Fail safe: keep the stable group leader alive rather than permit PGID
        # reuse when membership cannot be established.
        return True
    for row in rows.splitlines():
        fields = row.split()
        if len(fields) == 2 and int(fields[1]) == pgid and int(fields[0]) != supervisor_pid:
            return True
    return False

args = sys.argv[1:]
supervise = bool(args and args[0] == "--supervise")
if supervise:
    args = args[1:]
log, cwd = args[0], args[1]
cmd = args[2:]
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(r)
    os.setsid()
    pid2 = os.fork()
    if pid2 == 0:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.chdir(cwd)
        os.execvp(cmd[0], cmd)
    if supervise:
        # Install before publishing the PGID to close the detach-to-TERM race.
        # The command child was already forked and retains default handlers.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.write(w, f"{pid2}|{os.getpgrp()}".encode())
    os.close(w)
    if supervise:
        # The group supervisor must survive the graceful TERM sent to the
        # command group. It keeps the original PGID occupied while the caller
        # waits and, when necessary, escalates surviving descendants to KILL.
        # Do not keep the invoking subprocess.run() capture pipes open while
        # supervising. The actual command already owns the requested log.
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        _, status = os.waitpid(pid2, 0)
        # The direct Archon/watchdog child can exit on TERM before one of its
        # descendants. Keep owning the PGID until every original member is
        # gone so the controller can safely revalidate and escalate the group.
        while group_has_other_processes(os.getpgrp(), os.getpid()):
            time.sleep(0.1)
        if os.WIFEXITED(status):
            os._exit(os.WEXITSTATUS(status))
        os._exit(128 + os.WTERMSIG(status))
    os._exit(0)
os.close(w)
child, pgid = os.read(r, 64).decode().split("|", 1)
if not supervise:
    os.waitpid(pid, 0)
print(f"DETACHED_PID={child}")
print(f"DETACHED_PGID={pgid}")
