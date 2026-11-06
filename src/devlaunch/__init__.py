"""devlaunch: start and stop your local projects, see their URLs (including the LAN one) and catch port clashes.
Without arguments it opens the terminal UI; with arguments it works as a CLI."""

from __future__ import annotations

import argparse
import sys

STATUS_ICON = {"running": "●", "partial": "◐", "stopped": "○"}


def _find(projects, ident: str):
    exact = [p for p in projects if p.id == ident]
    similar = exact or [p for p in projects if ident.lower() in (p.id + p.name).lower()]
    if len(similar) == 1:
        return similar[0]
    if not similar:
        sys.exit(f"No project matches “{ident}”. See the list with: devlaunch list")
    sys.exit(f"“{ident}” matches several projects: {', '.join(p.id for p in similar)}")


def _urls(p, st, ip) -> list[str]:
    open_ = st.running and p.port and (st.listening or p.kind == "process")
    urls = [f"http://localhost:{p.port}{p.path}"] if open_ else []
    if open_ and st.listening != "local" and ip:
        urls.append(f"http://{ip}:{p.port}{p.path}")
    return urls


def _list_json(projects, statuses, ip, cont) -> None:
    """One object per project. usage = {ram_mb, vram_mb, cpu, since} (since = start time, epoch seconds) for running
    or partial projects; null for stopped ones."""
    import json
    import time

    from . import processes

    use = processes.usage(projects, statuses, cont)
    if use:  # psutil's first CPU reading is always 0: measure over a short interval
        time.sleep(0.3)
        use = processes.usage(projects, statuses, cont)
    out = [{"id": p.id, "name": p.name, "kind": p.kind, "port": p.port, "running": statuses[p.id].running,
            "partial": statuses[p.id].partial, "detail": statuses[p.id].detail, "urls": _urls(p, statuses[p.id], ip),
            "usage": use.get(p.id)} for p in projects]
    print(json.dumps(out, ensure_ascii=False, indent=2))


def _list_text(projects, statuses, ip) -> None:
    """One line per project: `<icon> <id> <port|—> [url] [notes]`. Kept stable so scripts can parse it."""
    for p in projects:
        st = statuses[p.id]
        urls = _urls(p, st, ip)
        icon = STATUS_ICON["running" if st.running else "partial" if st.partial else "stopped"]
        lan = ""
        if urls:
            lan = "  (this machine only)" if st.listening == "local" else f"  lan: {urls[1]}" if len(urls) > 1 else ""
        note = f"  partial: {st.detail}" if st.partial else ""
        print(f"{icon} {p.id:28} {str(p.port or '—'):6}{f' {urls[0]}' if urls else ''}{lan}{note}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="devlaunch", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="projects with their status and URLs")
    ls.add_argument("--json", action="store_true", help="JSON output (with RAM/VRAM/CPU) instead of text")
    lg = sub.add_parser("logs", help="last lines of a project's log")
    lg.add_argument("project")
    lg.add_argument("-n", "--lines", type=int, default=100, help="how many lines (default 100)")
    for c, h in (("up", "start a project (and what it depends on)"), ("down", "stop a project"),
                 ("url", "show its local and LAN URLs")):
        sub.add_parser(c, help=h).add_argument("project")
    d = sub.add_parser("detect", help="find new projects and add them to the list")
    d.add_argument("folders", nargs="*", help="folders to scan (default: [settings] roots, else the current directory)")
    args = ap.parse_args(argv)

    if not args.cmd:
        from .app import main as tui

        tui()
        return

    from . import config, processes

    projects = config.load()
    if args.cmd == "detect":
        from . import detect

        everything, new = detect.detect(config.scan_roots(args.folders), projects)
        config.save(everything)
        for p in new:
            print(f"+ {p.id:28} {p.kind:8} port {p.port or '—':<6} {p.command}")
        print(f"{len(new)} new. List saved to {config.CONFIG_FILE}")
        return
    if args.cmd == "list":
        ip = processes.lan_ip()
        cont, listen = processes.containers(), processes.listeners()
        owners = processes.project_pids(projects)
        statuses = {p.id: processes.status(p, cont, listen, owners) for p in projects}
        (_list_json(projects, statuses, ip, cont) if args.json else _list_text(projects, statuses, ip))
        return
    p = _find(projects, args.project)
    if args.cmd == "up":
        for q in processes.start_order(p, projects):
            if q.id != p.id and processes.status(q).running:
                continue
            who = processes.clash(q, projects)
            if who:
                print(f"⚠ port {q.port} is in use by {who}")
            print(f"{q.id}: {processes.start(q)}")
    elif args.cmd == "logs":
        print(processes.logs(p, args.lines))
    elif args.cmd == "down":
        print(processes.stop(p))
    elif args.cmd == "url":
        ip = processes.lan_ip()
        print(f"local: http://localhost:{p.port}{p.path}\nlan:   http://{ip}:{p.port}{p.path}" if p.port
              else "No port configured")
