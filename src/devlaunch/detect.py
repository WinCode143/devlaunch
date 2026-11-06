"""Find projects in a folder, and Docker containers with published ports.

These are educated guesses: whatever can't be inferred is left with a note to fix in projects.toml.
"""

from __future__ import annotations

import json
import re
import tomllib
import unicodedata
from pathlib import Path

from . import processes
from .config import Project

SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next"}
PORT_RE = re.compile(r"(?:--port[ =]|-p[ =]|port\s*[=:]\s*|PORT\s*[=:]\s*)[\"']?(\d{4,5})")
ARGPARSE_RE = re.compile(r"[\"']--port[\"'][^)]*?default\s*=\s*(\d{4,5})")  # parser.add_argument("--port", default=8765)
DATABASE_PORTS = {5432, 5433, 3306, 6379, 27017}


def slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "project"


def _short(d: Path) -> str:
    home = str(Path.home())
    return "~" + str(d)[len(home):] if str(d).startswith(home) else str(d)


def port_in(text: str) -> int | None:
    m = ARGPARSE_RE.search(text) or PORT_RE.search(text)
    return int(m.group(1)) if m and 1024 <= int(m.group(1)) <= 65535 else None


def _node(d: Path, root: Path) -> Project | None:
    try:
        pkg = json.loads((d / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = pkg.get("scripts", {})
    script = next((s for s in ("dev", "start", "serve") if s in scripts), None)
    if not script:
        return None
    cmd = scripts[script]
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    port = port_in(cmd)
    if port is None:
        for cfg in ("vite.config.ts", "vite.config.js", "vite.config.mjs", ".env", ".env.local"):
            if (d / cfg).exists():
                port = port_in((d / cfg).read_text(encoding="utf-8", errors="ignore"))
                if port:
                    break
    if port is None:  # framework defaults
        port = 3000 if "next" in deps or "next " in cmd else 5173 if "vite" in deps or "vite" in cmd else \
            4321 if "astro" in deps else 3000 if "react-scripts" in deps else None
    name = root.name if d == root else f"{root.name} · {d.name}"
    return Project(id=slug(name), name=name, dir=_short(d), command=f"npm run {script}", port=port,
                   notes="" if port else "Couldn't infer the port: please fill it in.")


def _python(d: Path) -> Project | None:
    base = dict(id=slug(d.name), name=d.name, dir=_short(d))
    if (d / "pyproject.toml").exists():
        try:
            scripts = tomllib.loads((d / "pyproject.toml").read_text(encoding="utf-8")).get("project", {}).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        web = next((s for s in scripts if re.search(r"web|server|app|ui|serve", s)), None)
        if web:
            src = " ".join(f.read_text(encoding="utf-8", errors="ignore") for f in list(d.glob("src/**/*.py"))[:60])
            return Project(**base, command=f"uv run {web}", port=port_in(src))
    for main in ("app.py", "main.py", "server.py", "streamlit_app.py"):
        f = d / main
        if not f.exists():
            continue
        code = f.read_text(encoding="utf-8", errors="ignore")
        py = ".venv/bin/python" if (d / ".venv").exists() else "python"
        if "streamlit" in code:
            return Project(**base, command=f"{py} -m streamlit run {main}", port=port_in(code) or 8501)
        if "gradio" in code:
            return Project(**base, command=f"{py} {main}", port=port_in(code) or 7860)
        if "FastAPI" in code or "uvicorn" in code:
            return Project(**base, command=f"{py} {main}", port=port_in(code) or 8000)
        if "Flask" in code:
            return Project(**base, command=f"{py} {main}", port=port_in(code) or 5000)
    return None


def folders(root: Path) -> list[Project]:
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        found = _node(d, d) if (d / "package.json").exists() else None
        if not found:
            found = _python(d)
        if not found:  # simple monorepos: frontend/, web/, app/, client/…
            for sub in sorted(s for s in d.iterdir() if s.is_dir() and s.name not in SKIP):
                if (sub / "package.json").exists() and (n := _node(sub, d)):
                    out.append(n)
            continue
        out.append(found)
    return out


def docker() -> list[Project]:
    out, groups = [], {}
    for name, c in processes.containers().items():
        if c["compose"]:
            groups.setdefault(c["compose"], []).append((name, c))
        elif c["ports"]:
            out.append(Project(id=slug(name), name=f"{name} (docker)", kind="docker", command=name, port=c["ports"][0][1]))
    for proj, items in groups.items():
        ports = [p for _, c in items for _, p in c["ports"]]
        web = next((p for p in ports if p not in DATABASE_PORTS), ports[0] if ports else None)
        workdir = items[0][1]["dir"]
        out.append(Project(id=slug(proj), name=f"{proj} (compose)", kind="compose", command=proj,
                           dir=_short(Path(workdir)) if workdir else "", port=web,
                           notes=f"{len(items)} containers: " + ", ".join(n for n, _ in items)))
    return out


def detect(roots: list[Path], current: list[Project]) -> tuple[list[Project], list[Project]]:
    """(merged list, new ones). Projects already configured are left alone: they may have hand edits."""
    ids = {p.id for p in current}
    new = []
    found = [p for r in roots if r.is_dir() for p in folders(r)] + docker()
    for p in found:
        if p.id in ids:
            continue
        ids.add(p.id)
        new.append(p)
    return current + new, new
