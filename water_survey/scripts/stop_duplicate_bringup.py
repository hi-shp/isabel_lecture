"""Stop only same-user duplicate mechaship bringup launch trees outside our service."""
import json
import os
from pathlib import Path
import signal
import time


def matches(argv):
    return any(argv[i:i+3]==['launch','mechaship_bringup','mechaship_bringup.launch.py'] for i in range(len(argv)-2))


def targets(processes,uid):
    chosen={pid for pid,p in processes.items() if pid>1 and p['uid']==uid and
        'water-survey-base.service' not in p['cgroup'] and matches(p['argv'])}
    while True:
        more={pid for pid,p in processes.items() if p['uid']==uid and p['ppid'] in chosen}
        if more<=chosen:return chosen
        chosen|=more


def read(pid):
    folder=Path('/proc')/str(pid)
    try:
        stat=(folder/'stat').read_text().rsplit(') ',1)[1].split()
        return dict(uid=folder.stat().st_uid,ppid=int(stat[1]),started=stat[19],
            argv=[v.decode(errors='replace') for v in (folder/'cmdline').read_bytes().split(b'\0') if v],
            cgroup=(folder/'cgroup').read_text())
    except (OSError,ValueError,IndexError):return None


def main():
    processes={int(p.name):read(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()}
    processes={pid:p for pid,p in processes.items() if p}
    chosen=targets(processes,os.getuid())
    def send(sig):
        for pid in chosen:
            current=read(pid)
            if current and current['uid']==os.getuid() and current['started']==processes[pid]['started']:
                try:os.kill(pid,sig)
                except ProcessLookupError:pass
    send(signal.SIGINT)
    if chosen:
        end=time.monotonic()+2
        while time.monotonic()<end and any(read(pid) for pid in chosen):time.sleep(.1)
        send(signal.SIGTERM)
    print(json.dumps({'duplicate_mechaship_processes_stopped':sorted(chosen)}),flush=True)


if __name__=='__main__':main()
