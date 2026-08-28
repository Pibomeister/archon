#!/usr/bin/env python3
"""Daemonize a command so it survives archon's node process-group reaping.
Double-fork + setsid; stdout/stderr to logfile. Prints DETACHED_PID=<pid>.
Usage: detach.py <logfile> <cwd> <cmd> [args...]"""
import os
import sys

log, cwd = sys.argv[1], sys.argv[2]
cmd = sys.argv[3:]
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
    os.write(w, str(pid2).encode())
    os._exit(0)
os.close(w)
child = os.read(r, 64).decode()
os.waitpid(pid, 0)
print(f"DETACHED_PID={child}")
