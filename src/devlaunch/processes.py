"""Start, stop and watch projects: our own processes, Docker containers and docker compose projects.

Processes start in their own session (setsid) with output going to ~/.cache/devlaunch/logs/<id>.log, so they keep
running after devlaunch exits. When it starts again it recognizes them by their saved pid or by the port they listen on.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import STATE_DIR, Project

LOGS = STATE_DIR / "logs"
PIDS = STATE_DIR / "pids"


@dataclass
class Status:
    running: bool
    origin: str = ""  # devlaunch · external · docker
    pid: int | None = None
    listening: str | None = None  # "network" (0.0.0.0/::), "local" (127.0.0.1) or None if the port isn't open
    detail: str = ""
    partial: bool = False  # compose project with live containers but the service not up (e.g. only the database)


# --- network ---

def lan_ip() -> str | None:
    """This machine's address on the local network, ignoring Docker's internal networks."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.254.254.254", 9))  # UDP connect sends nothing: it only picks the outgoing interface
            ip = s.getsockname()[0]
            if not ip.startswith(("127.", "172.17.")):
                return ip
    except OSError:
        pass
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr", "tailscale")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                return a.address
    return None


def listeners() -> dict[int, list[tuple[str, int | None]]]:
    """port → [(address it listens on, pid, or None if owned by another user, e.g. docker-proxy)]."""
    out: dict[int, list[tuple[str, int | None]]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return out
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr:
            out.setdefault(c.laddr.port, []).append((c.laddr.ip, c.pid))
    return out


def reach(ips: list[str]) -> str:
    return "network" if any(ip in ("0.0.0.0", "::", "") for ip in ips) else "local"


def pid_name(pid: int | None) -> str:
    if not pid:
        return "another user (Docker?)"
    try:
        return f"{psutil.Process(pid).name()} (pid {pid})"
    except psutil.Error:
        return f"pid {pid}"


# --- docker ---

def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def containers() -> dict[str, dict]:
    """Every container: name → {running, ports (published), compose (project or ""), service, dir}."""
    try:
        r = _docker("ps", "-a", "--format", "{{json .}}", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        labels = dict(kv.split("=", 1) for kv in d.get("Labels", "").split(",") if "=" in kv)
        ports = []
        for part in d.get("Ports", "").split(","):
            part = part.strip()
            if "->" in part:  # 0.0.0.0:8188->8188/tcp
                host = part.split("->")[0]
                try:
                    ports.append((host.rsplit(":", 1)[0] or "0.0.0.0", int(host.rsplit(":", 1)[1])))
                except ValueError:
                    pass
        out[d["Names"]] = {"running": d.get("State") == "running", "ports": ports,
                           "compose": labels.get("com.docker.compose.project", ""),
                           "service": labels.get("com.docker.compose.service", ""),
                           "dir": labels.get("com.docker.compose.project.working_dir", "")}
    return out


# --- status ---

def _pidfile(p: Project) -> Path:
    return PIDS / f"{p.id}.json"


def own_pid(p: Project) -> int | None:
    """pid of the process devlaunch started, if it's still alive (and is still the same process)."""
    try:
        d = json.loads(_pidfile(p).read_text())
        proc = psutil.Process(d["pid"])
        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE and abs(proc.create_time() - d["created"]) < 2:
            return d["pid"]
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return None


def project_pids(projects: list[Project]) -> dict[int, str]:
    """pid → project id for the processes devlaunch started, children included (npm → node…)."""
    out = {}
    for q in projects:
        pid = own_pid(q) if q.kind == "process" else None
        if not pid:
            continue
        out[pid] = q.id
        try:
            for ch in psutil.Process(pid).children(recursive=True):
                out[ch.pid] = q.id
        except psutil.Error:
            pass
    return out


def docker_ports(cont: dict[str, dict] | None) -> set[int]:
    return {port for c in (cont or {}).values() if c["running"] for _, port in c["ports"]}


def status(p: Project, cont: dict[str, dict] | None = None, listen_map: dict | None = None,
           owners: dict[int, str] | None = None) -> Status:
    """owners (from project_pids): if another devlaunch project holds the port, this one isn't running."""
    listen_map = listeners() if listen_map is None else listen_map
    cont = containers() if cont is None else cont
    listen = listen_map.get(p.port, []) if p.port else []
    listening = reach([ip for ip, _ in listen]) if listen else None
    if p.kind in ("docker", "compose"):
        mine = {n: c for n, c in cont.items() if (n == p.command if p.kind == "docker" else c["compose"] == p.command)}
        alive = [n for n, c in mine.items() if c["running"]]
        # Docker publishes through docker-proxy (owned by root): the port counts if one of ITS live containers publishes it
        pub = [ip for n in alive for ip, port in mine[n]["ports"] if port == p.port]
        listening = reach(pub) if pub else None
        if p.kind == "docker":
            return Status(bool(alive), "docker", None, listening if alive else None, "" if mine else "no such container")
        count = f"{len(alive)}/{len(mine)} containers"
        # Running only if the service answers. With a port: a live container publishes it (or it listens, for
        # network_mode: host). Without a port: every container is up.
        if p.port:
            complete = bool(pub) or bool(listen and p.port not in docker_ports(cont))
            listening = listening or (reach([ip for ip, _ in listen]) if complete else None)
        else:
            complete = len(alive) == len(mine)
        if not alive or complete:
            return Status(bool(alive), "docker", None, listening if alive else None, count)
        up = "all up" if len(alive) == len(mine) else \
            "only " + ", ".join(sorted(mine[n].get("service") or n for n in alive))
        missing = f" · port {p.port} closed" if p.port else ""
        return Status(False, "docker", None, None, f"{up}{missing} · {count}", partial=True)
    pid = own_pid(p)
    if pid:
        return Status(True, "devlaunch", pid, listening)
    if listen and p.port in docker_ports(cont):  # a container publishes it (docker-proxy): not this project
        return Status(False)
    other = (owners or {}).get(listen[0][1]) if listen else None
    if listen and not (other and other != p.id):  # started outside devlaunch, e.g. by hand in another terminal
        return Status(True, "external", listen[0][1], listening, f"started elsewhere: {pid_name(listen[0][1])}")
    return Status(False)


def clash(p: Project, everything: list[Project], listen_map: dict | None = None, cont: dict | None = None) -> str | None:
    """If p's port is already taken by something else, who has it (to warn before starting)."""
    if not p.port:
        return None
    listen_map = listeners() if listen_map is None else listen_map
    cont = containers() if cont is None else cont
    for c_name, c in cont.items():
        if c["running"] and any(port == p.port for _, port in c["ports"]):
            if (p.kind == "docker" and c_name == p.command) or (p.kind == "compose" and c["compose"] == p.command):
                return None
            return f"the container {c_name}"
    listen = listen_map.get(p.port)
    if not listen:
        return None
    pid = listen[0][1]
    if p.kind == "process" and pid and pid == own_pid(p):
        return None
    owners = project_pids(everything)
    if pid in owners and owners[pid] != p.id:
        other = next(q for q in everything if q.id == owners[pid])
        return f"the project “{other.name}”"
    other = next((q for q in everything
                  if q.id != p.id and q.port == p.port and status(q, cont, listen_map, owners).running), None)
    return f"the project “{other.name}”" if other else pid_name(pid)


# --- usage: RAM, VRAM and CPU per project (its process tree, or its containers' processes) ---

_PROCS: dict[int, psutil.Process] = {}  # reused so cpu_percent measures between one refresh and the next


def vram_by_pid() -> dict[int, int]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    res = {}
    for line in out.strip().splitlines():
        try:
            pid, mb = (int(x) for x in line.split(","))
            res[pid] = res.get(pid, 0) + mb
        except ValueError:
            pass
    return res


def container_pids(names: list[str]) -> dict[str, int]:
    """container → pid of its main process, as seen from the host."""
    if not names:
        return {}
    try:
        r = _docker("inspect", "-f", "{{.Name}} {{.State.Pid}}", *names, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        name, _, pid = line.strip().lstrip("/").partition(" ")
        if pid.isdigit() and int(pid):
            out[name] = int(pid)
    return out


def _tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def usage_of(roots: list[int], vram: dict[int, int]) -> dict:
    """{ram_mb, vram_mb, cpu, since} summed over the process trees of `roots`."""
    ram = cpu = gpu = 0.0
    since = None
    seen = set()
    for r in roots:
        for pr in _tree(r):
            if pr.pid in seen:
                continue
            seen.add(pr.pid)
            pr = _PROCS.setdefault(pr.pid, pr)
            try:
                ram += pr.memory_info().rss
                cpu += pr.cpu_percent(None)
                since = min(since or pr.create_time(), pr.create_time())
            except psutil.Error:
                continue
            gpu += vram.get(pr.pid, 0)
    for pid in list(_PROCS):
        if pid not in seen and not psutil.pid_exists(pid):
            _PROCS.pop(pid, None)
    return {"ram_mb": ram / 2**20, "vram_mb": gpu, "cpu": cpu, "since": since}


def usage(projects: list[Project], statuses: dict[str, Status], cont: dict[str, dict]) -> dict[str, dict]:
    vram = vram_by_pid()
    cpids = container_pids([n for n, c in cont.items() if c["running"]])
    out = {}
    for p in projects:
        st = statuses.get(p.id)
        if not st or not (st.running or st.partial):
            continue
        if p.kind == "docker":
            roots = [cpids[p.command]] if p.command in cpids else []
        elif p.kind == "compose":
            roots = [pid for n, pid in cpids.items() if cont.get(n, {}).get("compose") == p.command]
        else:
            roots = [st.pid] if st.pid else []
        out[p.id] = usage_of(roots, vram)
    return out


def git(p: Project) -> str | None:
    """“branch · N uncommitted changes” (None if it isn't a repository)."""
    if not p.dir or not (p.folder / ".git").exists() and not (p.folder.parent / ".git").exists():
        return None
    try:
        branch = subprocess.run(["git", "-C", str(p.folder), "branch", "--show-current"], capture_output=True, text=True,
                                timeout=3).stdout.strip()
        changes = subprocess.run(["git", "-C", str(p.folder), "status", "--porcelain"], capture_output=True, text=True,
                                 timeout=3).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return f"{branch or '(no branch)'} · " + (f"{len(changes)} uncommitted change(s)" if changes else "clean")


def start_order(p: Project, everything: list[Project]) -> list[Project]:
    """Dependencies first (no repeats, cycles ignored), then the project itself."""
    by_id = {q.id: q for q in everything}
    out, seen = [], set()

    def visit(q: Project) -> None:
        if q.id in seen:
            return
        seen.add(q.id)
        for d in q.depends:
            if d in by_id:
                visit(by_id[d])
        out.append(q)

    visit(p)
    return out


# --- start / stop ---

def start(p: Project) -> str:
    if p.kind == "docker":
        r = _docker("start", p.command, timeout=60)
        return r.stderr.strip() or f"Container {p.command} started"
    if p.kind == "compose":
        r = subprocess.run(["docker", "compose", "-p", p.command, "up", "-d"], cwd=p.folder, capture_output=True,
                           text=True, timeout=300)
        return (r.stderr.strip().splitlines() or ["Compose project started"])[-1]
    if own_pid(p):
        return "Already running"
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{p.id}.log", "ab") as log:  # the child keeps its own copy of the descriptor
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {p.command} =====\n".encode())
        log.flush()
        proc = subprocess.Popen(["bash", "-lc", p.command], cwd=p.folder, env={**os.environ, **p.env},
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _pidfile(p).write_text(json.dumps({"pid": proc.pid, "created": psutil.Process(proc.pid).create_time()}))
    return f"Started (pid {proc.pid})"


def stop(p: Project, timeout: float = 8) -> str:
    if p.kind == "docker":
        r = _docker("stop", p.command, timeout=60)
        return r.stderr.strip() or f"Container {p.command} stopped"
    if p.kind == "compose":
        r = subprocess.run(["docker", "compose", "-p", p.command, "stop"], capture_output=True, text=True, timeout=120)
        return (r.stderr.strip().splitlines() or ["Compose project stopped"])[-1]
    st = status(p)
    if not st.running or not st.pid:
        _pidfile(p).unlink(missing_ok=True)
        return "Wasn't running" if not st.running else "Started by another user: can't stop it from here"
    try:
        procs = [psutil.Process(st.pid), *psutil.Process(st.pid).children(recursive=True)]
    except psutil.Error:
        procs = []
    if st.origin == "devlaunch":
        try:
            os.killpg(st.pid, signal.SIGTERM)  # the whole group (npm → node → next…)
        except OSError:
            pass
    for pr in procs:
        try:
            pr.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for pr in alive:
        try:
            pr.kill()
        except psutil.Error:
            pass
    _pidfile(p).unlink(missing_ok=True)
    return "Stopped" + (" (killed)" if alive else "")


def logs(p: Project, lines: int = 300) -> str:
    try:
        if p.kind == "docker":
            r = _docker("logs", "--tail", str(lines), p.command, timeout=10)
            return r.stdout + r.stderr
        if p.kind == "compose":
            r = subprocess.run(["docker", "compose", "-p", p.command, "logs", "--tail", str(lines // 3), "--no-color"],
                               capture_output=True, text=True, timeout=10)
            return r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Couldn't read the logs: {e}"
    f = LOGS / f"{p.id}.log"
    if not f.exists():
        return "No logs yet: they're saved when you start the project from devlaunch."
    with open(f, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 200_000))
        return "\n".join(fh.read().decode(errors="replace").splitlines()[-lines:])
