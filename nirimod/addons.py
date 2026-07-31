"""Registry of community add-ons for the niri compositor.

Add-ons are described by a JSON registry.  The registry ships with the
application (``data/addons.json``) and can be refreshed from a remote URL
so users always get the latest list without updating NiriMod itself.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REGISTRY_URL = (
    "https://raw.githubusercontent.com/Erzkanzler2k/nirimod-ru/main/data/addons.json"
)
LOCAL_REGISTRY = Path(__file__).resolve().parent.parent / "data" / "addons.json"

DoneFn = Callable[[bool, str], None]
OutputFn = Callable[[str], None]


@dataclass
class Addon:
    """A single installable add-on for niri."""

    id: str
    name: str
    description: str
    category: str
    repo: str
    install_commands: list[str]
    remove_commands: list[str]

    @classmethod
    def from_dict(cls, raw: dict) -> "Addon":
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "Без названия")),
            description=str(raw.get("description", "")),
            category=str(raw.get("category", "прочее")),
            repo=str(raw.get("repo", "")),
            install_commands=list(raw.get("install", [])),
            remove_commands=list(raw.get("remove", [])),
        )


def load_local() -> list[Addon]:
    """Load add-ons bundled with the application."""
    try:
        with open(LOCAL_REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
        return [Addon.from_dict(item) for item in data.get("addons", [])]
    except (OSError, json.JSONDecodeError):
        return []


def fetch_remote(callback: DoneFn) -> threading.Thread:
    """Fetch the latest registry from GitHub in the background."""

    def _worker() -> None:
        try:
            req = urllib.request.Request(
                REGISTRY_URL, headers={"User-Agent": "NiriMod-Addons"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            addons = [Addon.from_dict(item) for item in data.get("addons", [])]
            _idle(callback, True, "", addons=addons)
        except Exception as exc:
            _idle(callback, False, str(exc), addons=[])

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def run_addon_commands(
    commands: list[str],
    on_output: OutputFn,
    on_done: DoneFn,
    workdir: Path | None = None,
) -> None:
    """Run the install/remove shell commands of an add-on in a thread."""

    def _worker() -> None:
        import subprocess

        if not commands:
            _idle(on_done, True, "Готово")
            return
        for cmd in commands:
            _idle(on_output, f"$ {cmd}")
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=workdir,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    stripped = line.rstrip("\n")
                    if stripped:
                        _idle(on_output, stripped)
                proc.wait()
                if proc.returncode != 0:
                    _idle(on_done, False, f"Команда завершилась с кодом {proc.returncode}")
                    return
            except Exception as exc:  # pragma: no cover - defensive
                _idle(on_done, False, f"Ошибка: {exc}")
                return
        _idle(on_done, True, "Готово")

    threading.Thread(target=_worker, daemon=True).start()


def _idle(fn: Callable, *args, **kwargs) -> None:
    try:
        from gi.repository import GLib

        GLib.idle_add(fn, *args, **kwargs)
    except Exception:
        fn(*args, **kwargs)
