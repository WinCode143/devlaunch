import json
import re
import socket
import time

import pytest

import devlaunch
from devlaunch import config, detect, processes
from devlaunch.config import Project, Settings

LIST_LINE = re.compile(r"^\s*([●○◐])\s+(\S+)\s+(\S+)\s*(.*)$")  # the documented, stable text format


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "projects.toml")
    monkeypatch.setattr(processes, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(processes, "PIDS", tmp_path / "pids")
    monkeypatch.setattr(processes, "containers", lambda: {})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, t=6.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


# --- config ---

def test_config_round_trip():
    ps = [Project(id="web", name="Web “editor”", dir="~/x", command='uv run "web"', port=8765, env={"A B": "1"}),
          Project(id="c", name="C", kind="docker", command="comfyui", depends=["web"])]
    config.save(ps, Settings(roots=["~/code", "~/work"]))
    assert config.load() == ps
    assert config.load_settings().roots == ["~/code", "~/work"]


def test_save_keeps_settings_by_default():
    config.save([], Settings(roots=["~/code"]))
    config.save([Project(id="a", name="A")])
    assert config.load_settings().roots == ["~/code"]


def test_scan_roots_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.scan_roots() == [tmp_path]  # nothing configured: the current directory
    config.save([], Settings(roots=[str(tmp_path / "a")]))
    assert config.scan_roots() == [tmp_path / "a"]
    assert config.scan_roots([str(tmp_path / "b")]) == [tmp_path / "b"]  # explicit folders win


# --- detection ---

def test_infers_ports():
    assert detect.port_in("next dev -p 3005") == 3005
    assert detect.port_in('parser.add_argument("--port", type=int, default=8765)') == 8765
    assert detect.port_in("server: { port: 5174 }") == 5174
    assert detect.port_in("no port here") is None
    assert detect.port_in("--port 80") is None  # privileged ports are almost always a false match


def test_detects_node_python_and_monorepo(tmp_path):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "package.json").write_text(json.dumps(
        {"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^6"}}))
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "main.py").write_text("from fastapi import FastAPI\nuvicorn.run(app, port=8100)\n")
    (tmp_path / "mono" / "frontend").mkdir(parents=True)
    (tmp_path / "mono" / "frontend" / "package.json").write_text(json.dumps({"scripts": {"dev": "next dev"},
                                                                             "dependencies": {"next": "15"}}))
    (tmp_path / "notes").mkdir()  # not a project
    found = {p.id: p for p in detect.folders(tmp_path)}
    assert set(found) == {"shop", "api", "mono-frontend"}
    assert (found["shop"].command, found["shop"].port) == ("npm run dev", 5173)
    assert (found["api"].command, found["api"].port) == ("python main.py", 8100)
    assert found["mono-frontend"].port == 3000 and found["mono-frontend"].name == "mono · frontend"


def test_detect_keeps_hand_edits(tmp_path):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "package.json").write_text(json.dumps({"scripts": {"dev": "vite --port 5300"}}))
    edited = Project(id="shop", name="My shop", command="npm run dev -- --host", port=5300)
    everything, new = detect.detect([tmp_path], [edited])
    assert new == [] and everything == [edited]


def test_compose_detection_skips_database_ports(monkeypatch):
    monkeypatch.setattr(processes, "containers", lambda: {
        "shop-db-1": {"running": True, "ports": [("127.0.0.1", 5432)], "compose": "shop", "service": "db", "dir": ""},
        "shop-web-1": {"running": True, "ports": [("0.0.0.0", 8080)], "compose": "shop", "service": "web", "dir": ""},
    })
    [p] = detect.docker()
    assert (p.kind, p.command, p.port) == ("compose", "shop", 8080)


# --- processes ---

def test_start_stop_and_clash(tmp_path):
    port = free_port()
    a = Project(id="a", name="A", dir=str(tmp_path), command=f"python3 -m http.server {port} --bind 127.0.0.1", port=port,
                env={"PYTHONUNBUFFERED": "1"})  # also checks that `env` reaches the process
    b = Project(id="b", name="B", dir=str(tmp_path), command=f"python3 -m http.server {port}", port=port)
    both = [a, b]
    try:
        processes.start(a)
        assert wait_for(lambda: processes.status(a).listening == "local")
        st = processes.status(a)
        assert st.running and st.origin == "devlaunch"
        owners = processes.project_pids(both)
        assert not processes.status(b, owners=owners).running  # the port belongs to A, not B
        assert processes.clash(b, both) == "the project “A”"
        assert processes.clash(a, both) is None
    finally:
        processes.stop(a)
    assert wait_for(lambda: not processes.status(a).running)
    assert "Serving HTTP on 127.0.0.1" in processes.logs(a)


def test_dependencies_first_without_cycles():
    a = Project(id="a", name="A", depends=["b"])
    b = Project(id="b", name="B", depends=["c", "a"])  # a ↔ b cycle: must not hang
    c = Project(id="c", name="C")
    assert [p.id for p in processes.start_order(a, [a, b, c])] == ["c", "b", "a"]


def _shop(web=False, worker=False, db=True) -> dict[str, dict]:
    def c(service, up, ports=()):
        return {"running": up, "ports": list(ports) if up else [], "compose": "shop", "service": service, "dir": ""}
    return {"shop-web-1": c("web", web, [("0.0.0.0", 3000)]), "shop-worker-1": c("worker", worker),
            "shop-db-1": c("db", db, [("127.0.0.1", 5432)])}


SHOP = Project(id="shop", name="Shop", kind="compose", command="shop", port=3000)


def test_compose_with_only_the_database_is_partial():
    st = processes.status(SHOP, _shop(), {})
    assert not st.running and st.partial and st.listening is None
    assert st.detail.startswith("only db") and "1/3" in st.detail
    no_port = Project(id="s", name="S", kind="compose", command="shop")
    st = processes.status(no_port, _shop(), {})
    assert st.partial and not st.running


def test_compose_complete_and_stopped():
    st = processes.status(SHOP, _shop(web=True), {})  # worker is down, but port 3000 answers
    assert st.running and not st.partial and st.listening == "network"
    no_port = Project(id="s", name="S", kind="compose", command="shop")
    assert processes.status(no_port, _shop(True, True, True), {}).running
    st = processes.status(SHOP, _shop(db=False), {})
    assert not st.running and not st.partial


def test_port_published_by_a_container_is_not_a_process():
    p = Project(id="p", name="P", command="npm run dev", port=3000)
    st = processes.status(p, _shop(web=True), {3000: [("0.0.0.0", None)]})
    assert not st.running


def test_stopping_a_partial_compose_stops_what_is_up(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return processes.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(processes.subprocess, "run", run)
    processes.stop(SHOP)
    assert calls == [["docker", "compose", "-p", "shop", "stop"]]


# --- CLI ---

def test_text_list_shows_partial(monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda: [SHOP])
    monkeypatch.setattr(processes, "containers", lambda: _shop())
    monkeypatch.setattr(processes, "listeners", lambda: {})
    monkeypatch.setattr(processes, "lan_ip", lambda: "192.168.1.10")
    devlaunch.main(["list"])
    m = LIST_LINE.match(capsys.readouterr().out.splitlines()[0])
    assert m and m.groups()[:3] == ("◐", "shop", "3000")
    assert "only db" in m[4] and "http" not in m[4]


def test_json_list_and_logs(capsys, monkeypatch):
    p = Project(id="web", name="Web", command="python3 -m http.server", port=free_port())
    q = Project(id="idle", name="Idle", command="true", port=free_port())
    p.command += f" {p.port}"
    config.save([p, q])
    try:
        processes.start(p)
        assert wait_for(lambda: processes.status(p).listening is not None)
        devlaunch.main(["list", "--json"])
        data = json.loads(capsys.readouterr().out)
        by_id = {d["id"]: d for d in data}
        for d in data:
            assert set(d) == {"id", "name", "kind", "port", "running", "partial", "detail", "urls", "usage"}
        assert by_id["web"]["running"] is True and by_id["web"]["urls"][0] == f"http://localhost:{p.port}"
        assert set(by_id["web"]["usage"]) == {"ram_mb", "vram_mb", "cpu", "since"}
        assert by_id["web"]["usage"]["ram_mb"] > 0
        assert by_id["idle"]["running"] is False and by_id["idle"]["usage"] is None

        monkeypatch.setattr(processes, "logs", lambda pr, n=300: f"{pr.id}:{n}")
        devlaunch.main(["logs", "web", "-n", "5"])
        assert capsys.readouterr().out.strip() == "web:5"
        devlaunch.main(["logs", "web"])
        assert capsys.readouterr().out.strip() == "web:100"
    finally:
        processes.stop(p)


def test_unknown_project_exits_with_a_hint(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: [SHOP])
    with pytest.raises(SystemExit, match="devlaunch list"):
        devlaunch.main(["up", "nope"])


# --- TUI ---

async def test_tui_lists_projects_and_filters(monkeypatch):
    from devlaunch.app import DevLaunch

    config.save([SHOP, Project(id="api", name="API", command="true", port=8000)])
    monkeypatch.setattr(processes, "containers", lambda: _shop(web=True))
    monkeypatch.setattr(processes, "listeners", lambda: {})
    app = DevLaunch(refresh_every=60)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one("#table")
        assert table.row_count == 2
        assert app.visible()[0].id == "shop"  # running projects are listed first
        await pilot.press("slash", "a", "p", "i")
        await pilot.pause()
        assert [p.id for p in app.visible()] == ["api"]
