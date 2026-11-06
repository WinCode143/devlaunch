"""Terminal UI (Textual): projects with status, URLs, usage and logs; start, stop and switch between them."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import webbrowser

import psutil
import qrcode
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from . import config, processes
from .config import Project

LAN_HINT = {  # how to make a server listen on the network when it only listens on 127.0.0.1
    "next": "add -H 0.0.0.0 to the command (npm run dev -- -H 0.0.0.0)",
    "vite": "add --host (npm run dev -- --host)",
    "uv run": "start it with --host 0.0.0.0 if the program supports it",
    "streamlit": "add --server.address 0.0.0.0",
}


def url(p: Project, host: str) -> str:
    return f"http://{host}:{p.port}{p.path}" if p.port else ""


def mb(v: float) -> str:
    return "" if v < 1 else f"{v / 1024:.1f} GB" if v >= 1024 else f"{v:.0f} MB"


def uptime(since: float | None) -> str:
    if not since:
        return ""
    s = int(time.time() - since)
    return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 \
        else f"{s // 60}m" if s >= 60 else f"{s}s"


def totals() -> str:
    ram = psutil.virtual_memory()
    txt = f"RAM {ram.used / 2**30:.1f}/{ram.total / 2**30:.0f} GB"
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.split("\n")[0]
        used, total = (int(x) for x in out.split(","))
        txt += f" · VRAM {used / 1024:.1f}/{total / 1024:.0f} GB"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return txt


def lan_hint(p: Project) -> str:
    return next((v for k, v in LAN_HINT.items() if k in p.command), "configure it to listen on 0.0.0.0")


def qr_text(data: str) -> str:
    """A QR code drawn with half blocks (two rows of modules per line), scannable from the terminal."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    m = qr.get_matrix()
    if len(m) % 2:
        m.append([False] * len(m[0]))
    chars = {(False, False): "█", (True, True): " ", (True, False): "▄", (False, True): "▀"}
    return "\n".join("".join(chars[(m[y][x], m[y + 1][x])] for x in range(len(m[0]))) for y in range(0, len(m), 2))


class Confirm(ModalScreen[bool]):
    def __init__(self, text: str, yes: str, no: str = "Cancel"):
        super().__init__()
        self.text, self.yes, self.no = text, yes, no

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.text, id="question")
            with Horizontal(id="buttons"):
                yield Button(self.yes, variant="warning", id="yes")
                yield Button(self.no, id="no")

    @on(Button.Pressed)
    def answer(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class ShowQR(ModalScreen[None]):
    def __init__(self, title: str, data: str):
        super().__init__()
        self.title_text, self.data = title, data

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{self.title_text}\n{self.data}", id="question")
            yield Static(qr_text(self.data), id="qr")
            yield Button("Close", id="no")

    @on(Button.Pressed)
    def close(self) -> None:
        self.dismiss()

    def key_escape(self) -> None:
        self.dismiss()


class DevLaunch(App):
    TITLE = "devlaunch"
    CSS = """
    #body { height: 1fr; }
    #table { width: 66%; }
    #side { width: 34%; border-left: solid $primary-darken-2; }
    #detail { height: auto; padding: 0 1; }
    #logs { height: 1fr; border-top: solid $primary-darken-2; }
    #filter { display: none; }
    #filter.visible { display: block; }
    Confirm, ShowQR { align: center middle; }
    #dialog { width: auto; max-width: 90; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #question { margin-bottom: 1; }
    #buttons { height: auto; width: auto; }
    #buttons Button { margin-right: 2; }
    #qr { width: auto; color: white; background: black; margin-bottom: 1; }
    """
    BINDINGS = [
        Binding("enter", "toggle", "Start/stop"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("r", "restart", "Restart"),
        Binding("o", "open", "Open"),
        Binding("c", "copy", "Copy URL"),
        Binding("m", "qr", "Phone QR"),
        Binding("a", "only_active", "Active only"),
        Binding("slash", "search", "Search"),
        # some keyboard layouts (e.g. Spanish) type “/” as Shift+7, and some terminals report it that way
        Binding("b,shift+7", "search", "Search", show=False),
        Binding("v", "vscode", "VS Code"),
        Binding("e", "edit", "Edit list"),
        Binding("d", "detect", "Detect"),
        Binding("p", "command_palette", "Commands"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+p", "command_palette", "Commands", show=False, priority=True),
    ]

    def __init__(self, refresh_every: float = 2.5):
        super().__init__()
        self.refresh_every = refresh_every
        self.projects: list[Project] = []
        self.statuses: dict[str, processes.Status] = {}
        self.clashes: dict[str, str | None] = {}
        self.usage: dict[str, dict] = {}
        self.stopped_by_user: set[str] = set()  # so stopping a project by hand isn't reported as a crash
        self.gits: dict[str, str | None] = {}
        self.totals = ""
        self.ip = processes.lan_ip()
        self.only_active = False
        self.filter = ""
        self._log_of: str | None = None
        self._last_log: str | None = None

    # --- layout ---
    def compose(self) -> ComposeResult:
        yield Header(icon="🚀")
        yield Input(placeholder="Search projects… (Enter to go back to the list, Esc to clear)", id="filter", disabled=True)
        with Horizontal(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="detail")
                yield RichLog(id="logs", wrap=True, max_lines=2000)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("", "Project", "Port", "RAM", "VRAM", "CPU", "Up", "URL")
        self.reload()
        if not self.projects:
            self.notify("No projects yet: press “d” to detect them in your project folders.", timeout=10)
        t.focus()
        self.set_interval(self.refresh_every, self.refresh_status)
        self.set_interval(2, self.refresh_logs)
        self.refresh_status()

    def reload(self) -> None:
        self.projects = config.load()

    # --- status (in a thread: psutil and docker are slow) ---
    @work(thread=True, exclusive=True, group="status")
    def refresh_status(self) -> None:
        cont, listen = processes.containers(), processes.listeners()
        owners = processes.project_pids(self.projects)
        statuses = {p.id: processes.status(p, cont, listen, owners) for p in self.projects}
        clashes = {p.id: (processes.clash(p, self.projects, listen, cont) if not statuses[p.id].running else None)
                   for p in self.projects}
        use = processes.usage(self.projects, statuses, cont)
        self.call_from_thread(self._paint, statuses, clashes, use, totals())

    def visible(self) -> list[Project]:
        def active(p: Project) -> bool:  # running or partial (a compose project with some container up)
            st = self.statuses.get(p.id, processes.Status(False))
            return st.running or st.partial

        out = sorted(self.projects, key=lambda p: not active(p))  # active ones first (stable order)
        if self.only_active:
            out = [p for p in out if active(p)]
        if self.filter:
            out = [p for p in out if self.filter.lower() in (p.name + p.id + p.command).lower()]
        return out

    def _paint(self, statuses, clashes, use=None, tot=None) -> None:
        self._report_crashes(self.statuses, statuses)
        self.statuses, self.clashes = statuses, clashes
        if use is not None:
            self.usage, self.totals = use, tot
        t = self.query_one("#table", DataTable)
        current = self.current()
        t.clear()
        for p in self.visible():
            st = statuses.get(p.id, processes.Status(False))
            icon = Text("●", style="bold green") if st.running else Text("○", style="dim")
            if st.running and st.origin == "external":
                icon = Text("●", style="bold yellow")
            elif st.partial:
                icon = Text("◐", style="bold yellow")
            name = Text(p.name, style="bold" if st.running else "")
            port = Text(str(p.port or "—"))
            if not st.running and clashes.get(p.id):
                port.stylize("bold red")
                port.append(" ⚠", style="red")
            u = url(p, "localhost") if st.running and (st.listening or p.kind == "process") else ""
            if st.running and p.port and p.kind == "process" and not st.listening:
                u = "starting…"
            c = self.usage.get(p.id, {})
            ram, vram, cpu = c.get("ram_mb", 0), c.get("vram_mb", 0), c.get("cpu", 0)
            t.add_row(icon, name, port,
                      Text(mb(ram), style="bold yellow" if ram > 4096 else ""),
                      Text(mb(vram), style="bold medium_purple1" if vram > 2048 else "medium_purple1"),
                      Text(f"{cpu:.0f} %" if (st.running or st.partial) and c else "", style="bold red" if cpu > 100 else ""),
                      Text(uptime(c.get("since")), style="dim"),
                      Text(u, style="yellow" if u == "starting…" else "cyan"), key=p.id)
        if current:
            ids = [p.id for p in self.visible()]
            if current.id in ids:
                t.move_cursor(row=ids.index(current.id))
        n = sum(1 for s in statuses.values() if s.running)
        self.sub_title = f"LAN {self.ip or '?'} · {n} of {len(self.projects)} running · {self.totals}"
        self.paint_detail()

    def _report_crashes(self, before: dict, now: dict) -> None:
        for pid, st in now.items():
            prev = before.get(pid)
            if prev and prev.running and prev.origin == "devlaunch" and not st.running and pid not in self.stopped_by_user:
                p = next((q for q in self.projects if q.id == pid), None)
                if p:
                    tail = "\n".join(processes.logs(p, 4).splitlines()[-4:])
                    self.notify(f"{p.name} crashed.\n{tail}", severity="error", timeout=20)
        self.stopped_by_user &= {k for k, v in now.items() if v.running}

    def current(self) -> Project | None:
        t = self.query_one("#table", DataTable)
        if not t.row_count:
            return None
        try:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((p for p in self.projects if p.id == key), None)

    def paint_detail(self) -> None:
        p = self.current()
        d = self.query_one("#detail", Static)
        if not p:
            d.update("No projects. Press “d” to detect them or “e” to edit the list.")
            return
        st = self.statuses.get(p.id, processes.Status(False))
        txt = Text()
        txt.append(f"{p.name}\n", style="bold")
        state = ("● running" + (" (started elsewhere)" if st.origin == "external" else "")) if st.running else "○ stopped"
        if st.partial:
            state = "◐ partial (the service isn't answering)"
        txt.append(state, style="green" if st.running else "yellow" if st.partial else "dim")
        if st.detail:
            txt.append(f" · {st.detail}", style="dim")
        c = self.usage.get(p.id)
        if (st.running or st.partial) and c:
            parts = [f"RAM {mb(c['ram_mb']) or '0 MB'}", c["vram_mb"] and f"VRAM {mb(c['vram_mb'])}",
                     f"CPU {c['cpu']:.0f} %", c["since"] and f"up {uptime(c['since'])}"]
            txt.append("\n" + " · ".join(x for x in parts if x), style="bold")
        txt.append(f"\n{p.kind} · ", style="dim")
        txt.append(f"{p.command}\n", style="dim")
        if p.dir:
            txt.append(f"{p.dir}\n", style="dim")
        if p.port:
            txt.append("Local: ")
            txt.append(url(p, "localhost") + "\n", style="cyan")
            txt.append("LAN:   ")
            if st.running and st.listening == "local":
                txt.append("listens on this machine only", style="yellow")
                txt.append(f" — to open it from your phone, {lan_hint(p)}\n", style="dim")
            elif self.ip:
                txt.append(url(p, self.ip) + "\n", style="cyan" if st.running else "dim")
        if st.running and p.port and p.kind != "process" and not st.listening:
            txt.append(f"Port {p.port} isn't open: a container in the project is stopped.\n", style="yellow")
        if not st.running and self.clashes.get(p.id):
            txt.append(f"⚠ Port {p.port} is in use by {self.clashes[p.id]}: starting will offer to stop it.\n", style="red")
        if p.depends:
            txt.append(f"Starts first: {', '.join(p.depends)}\n", style="dim")
        g = self.gits.get(p.id)
        if g:
            txt.append(f"git: {g}\n", style="dim")
        if p.notes:
            txt.append(f"Note: {p.notes}\n", style="italic dim")
        d.update(txt)

    @on(DataTable.RowHighlighted)
    def on_move(self) -> None:
        self.paint_detail()
        self.refresh_logs(force=True)
        self.read_git()

    @work(thread=True, exclusive=True, group="git")
    def read_git(self) -> None:
        p = self.call_from_thread(self.current)
        if p and p.id not in self.gits:
            self.gits[p.id] = processes.git(p)
            self.call_from_thread(self.paint_detail)

    @on(DataTable.RowSelected)
    def on_select(self) -> None:
        self.action_toggle()

    @work(thread=True, exclusive=True, group="logs")
    def refresh_logs(self, force: bool = False) -> None:
        p = self.call_from_thread(self.current)
        if not p:
            return
        text = processes.logs(p, 200)
        self.call_from_thread(self._paint_logs, p.id, text, force)

    def _paint_logs(self, pid: str, text: str, force: bool) -> None:
        log = self.query_one("#logs", RichLog)
        if force or self._log_of != pid or self._last_log != text:
            log.clear()
            log.write(Text.from_ansi(text[-60_000:]))
            self._log_of, self._last_log = pid, text

    # --- actions ---
    def action_toggle(self) -> None:
        p = self.current()
        if p:
            (self.action_stop if self.statuses.get(p.id, processes.Status(False)).running else self.action_start)()

    def action_start(self) -> None:
        p = self.current()
        if not p:
            return
        who = processes.clash(p, self.projects)
        if not who:
            self._start(p, None)
            return
        other = next((q for q in self.projects if who == f"the project “{q.name}”"), None)
        if other:
            self.push_screen(Confirm(f"Port {p.port} is in use by {other.name}.\nStop {other.name} and start {p.name}?",
                                     f"Stop {other.name} and start"),
                             lambda ok: ok and self._start(p, other))
        else:
            self.push_screen(Confirm(f"Port {p.port} is taken by {who}.\n"
                                     f"{p.name} will probably fail to start or pick another port.", "Start anyway"),
                             lambda ok: ok and self._start(p, None))

    @work(thread=True)
    def _start(self, p: Project, stop_first: Project | None) -> None:
        if stop_first:
            self.call_from_thread(self.notify, f"Stopping {stop_first.name}…")
            self.stopped_by_user.add(stop_first.id)
            processes.stop(stop_first)
        for q in processes.start_order(p, self.projects):
            if q.id != p.id and processes.status(q).running:
                continue  # the dependency was already up
            self.call_from_thread(self.notify, f"Starting {q.name}…" + (" (dependency)" if q.id != p.id else ""))
            try:
                msg = processes.start(q)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.call_from_thread(self.notify, f"Couldn't start {q.name}: {e}", severity="error", timeout=10)
                return
            self.call_from_thread(self.notify, f"{q.name}: {msg}")
        self.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_stop(self) -> None:
        p = self.call_from_thread(self.current)
        if not p:
            return
        self.call_from_thread(self.notify, f"Stopping {p.name}…")
        self.stopped_by_user.add(p.id)
        try:
            msg = processes.stop(p)
        except Exception as e:  # noqa: BLE001
            msg = f"error: {e}"
        self.call_from_thread(self.notify, f"{p.name}: {msg}")
        self.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_restart(self) -> None:
        p = self.call_from_thread(self.current)
        if not p:
            return
        self.call_from_thread(self.notify, f"Restarting {p.name}…")
        self.stopped_by_user.add(p.id)
        processes.stop(p)
        try:
            msg = processes.start(p)
        except Exception as e:  # noqa: BLE001
            msg = f"error: {e}"
        self.call_from_thread(self.notify, f"{p.name}: {msg}")
        self.call_from_thread(self.refresh_status)

    def _best_url(self, p: Project) -> str:
        st = self.statuses.get(p.id, processes.Status(False))
        return url(p, self.ip) if self.ip and st.listening != "local" else url(p, "localhost")

    def action_open(self) -> None:
        p = self.current()
        if not p or not p.port:
            self.notify("This project has no port configured (edit the list with “e”).", severity="warning")
            return
        webbrowser.open(url(p, "localhost"))

    def action_copy(self) -> None:
        p = self.current()
        if not p or not p.port:
            return
        u = self._best_url(p)
        if shutil.which("wl-copy"):
            subprocess.run(["wl-copy", u], check=False)
        else:
            self.copy_to_clipboard(u)  # OSC 52: works in most modern terminals
        self.notify(f"Copied: {u}")

    def action_qr(self) -> None:
        p = self.current()
        if not p or not p.port:
            return
        st = self.statuses.get(p.id, processes.Status(False))
        if not self.ip:
            self.notify("Couldn't find this machine's LAN address.", severity="warning")
            return
        if st.listening == "local":
            self.notify(f"{p.name} only listens on this machine: {lan_hint(p)}", severity="warning", timeout=10)
            return
        self.push_screen(ShowQR(f"Open {p.name} on your phone (same Wi-Fi)", url(p, self.ip)))

    def action_vscode(self) -> None:
        p = self.current()
        if not p or not p.dir:
            self.notify("This project has no folder (it's a standalone container).", severity="warning")
            return
        if not shutil.which("code"):
            self.notify("VS Code's “code” command isn't installed.", severity="warning")
            return
        subprocess.Popen(["code", str(p.folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        self.notify(f"Opening {p.name} in VS Code")

    def action_only_active(self) -> None:
        self.only_active = not self.only_active
        self.notify("Showing active projects only" if self.only_active else "Showing all projects")
        self.refresh_status()

    def action_search(self) -> None:
        f = self.query_one("#filter", Input)
        f.disabled = False
        f.add_class("visible")
        f.focus()

    @on(Input.Changed, "#filter")
    def on_filter(self, e: Input.Changed) -> None:
        self.filter = e.value
        self._paint(self.statuses, self.clashes)

    @on(Input.Submitted, "#filter")
    def end_filter(self) -> None:
        self.query_one("#table", DataTable).focus()

    def key_escape(self) -> None:
        f = self.query_one("#filter", Input)
        if f.has_class("visible"):
            f.value = ""
            f.remove_class("visible")
            f.disabled = True
            self.query_one("#table", DataTable).focus()

    def action_edit(self) -> None:
        if not config.CONFIG_FILE.exists():
            config.save(self.projects)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
        with self.suspend():
            subprocess.run([editor, str(config.CONFIG_FILE)])
        self.reload()
        self.notify(f"List reloaded: {len(self.projects)} projects")
        self.refresh_status()

    @work(thread=True)
    def action_detect(self) -> None:
        from . import detect

        roots = config.scan_roots()
        self.call_from_thread(self.notify, "Scanning " + ", ".join(str(r) for r in roots) + "…")
        everything, new = detect.detect(roots, config.load())
        config.save(everything)
        self.call_from_thread(self.reload)
        self.call_from_thread(self.notify, f"{len(new)} new project(s); review the list with “e”.", timeout=8)
        self.call_from_thread(self.refresh_status)


def main() -> None:
    DevLaunch().run()
