"""The project list: ~/.config/devlaunch/projects.toml (created by `devlaunch detect`, meant to be edited by hand)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("DEVLAUNCH_CONFIG", Path.home() / ".config" / "devlaunch"))
CONFIG_FILE = CONFIG_DIR / "projects.toml"
STATE_DIR = Path(os.environ.get("DEVLAUNCH_STATE", Path.home() / ".cache" / "devlaunch"))


@dataclass
class Project:
    id: str
    name: str
    kind: str = "process"  # process (a command) · docker (one container) · compose (a docker compose project)
    dir: str = ""  # where the command runs / where the compose file lives
    command: str = ""  # process: what to run; docker: container name; compose: compose project name
    port: int | None = None  # main port (for the URL and clash detection)
    path: str = ""  # appended to the URL, e.g. "/docs"
    env: dict[str, str] = field(default_factory=dict)  # extra environment variables
    depends: list[str] = field(default_factory=list)  # ids started first (e.g. a web app that needs a model server)
    notes: str = ""

    @property
    def folder(self) -> Path:
        return Path(os.path.expanduser(self.dir)) if self.dir else Path.home()


@dataclass
class Settings:
    roots: list[str] = field(default_factory=list)  # folders `detect` scans when none is given


def _read() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load() -> list[Project]:
    out = []
    for pid, d in _read().get("project", {}).items():
        known = {k: v for k, v in d.items() if k in Project.__dataclass_fields__ and k != "id"}
        out.append(Project(id=pid, **known))
    return out


def load_settings() -> Settings:
    s = _read().get("settings", {})
    return Settings(roots=[str(r) for r in s.get("roots", [])])


def scan_roots(given: list[str] | None = None) -> list[Path]:
    """Folders to scan: the ones given, else [settings] roots, else the current directory."""
    roots = given or load_settings().roots or [os.getcwd()]
    return [Path(os.path.expanduser(r)) for r in roots]


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_toml(x)}" for k, x in v.items()) + " }"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() and k.isascii() else _toml(k)


def save(projects: list[Project], settings: Settings | None = None) -> None:
    settings = settings if settings is not None else load_settings()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# devlaunch projects. Edit freely: kind = process | docker | compose.",
             "# process: `command` runs in `dir`. docker: command = container name.",
             "# compose: dir = folder with the compose file, command = compose project name.",
             "# port = the one opened in the browser.", ""]
    if settings.roots:
        lines += ["[settings]", f"roots = {_toml(settings.roots)}", ""]
    for p in projects:
        lines.append(f"[project.{_key(p.id)}]")
        for k, v in asdict(p).items():
            if k == "id" or v in (None, "", {}, []):
                continue
            lines.append(f"{k} = {_toml(v)}")
        lines.append("")
    CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")
