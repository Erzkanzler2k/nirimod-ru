"""Backend for searching, installing and removing system packages.

Supports Nix (``nix profile``) and APT/pacman/dnf/zypper as a fallback.
All long-running operations run in a worker thread and stream output
back to the caller via callbacks (dispatched on the GLib main loop).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

NIXPKGS_CHANNEL = "nixpkgs"


@dataclass
class InstalledPackage:
    """A package currently present in the user's profile."""

    name: str
    attribute_path: str
    store_path: str
    priority: str
    original: str


@dataclass
class SearchResult:
    """A single result from a package search."""

    attribute_path: str
    name: str
    description: str

    @property
    def short_name(self) -> str:
        """Return the last component of the attribute path."""
        return self.attribute_path.rsplit(".", 1)[-1]


OutputFn = Callable[[str], None]
DoneFn = Callable[[bool, str], None]


class PackageBackend:
    """Thin, testable wrapper around the detected package manager."""

    def __init__(self) -> None:
        self._manager = self._detect()
        self._lock = threading.Lock()

    @property
    def manager(self) -> str | None:
        """Detected package manager name (``nix``, ``apt``, ...) or None."""
        return self._manager

    @property
    def is_nix(self) -> bool:
        return self._manager == "nix"

    @property
    def human_name(self) -> str:
        return {
            "nix": "Nix",
            "apt": "APT",
            "pacman": "Pacman",
            "dnf": "DNF",
            "zypper": "Zypper",
        }.get(self._manager or "", self._manager or "неизвестно")

    @staticmethod
    def _detect() -> str | None:
        for name in ("nix", "apt", "pacman", "dnf", "zypper"):
            if shutil.which(name):
                return name
        return None

    # ------------------------------------------------------------------ #
    # Installed packages
    # ------------------------------------------------------------------ #

    def installed_packages(self) -> list[InstalledPackage]:
        if self.is_nix:
            return self._nix_profile_list()
        return []

    def _nix_profile_list(self) -> list[InstalledPackage]:
        try:
            proc = subprocess.run(
                ["nix", "profile", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                return []
            return _nix_profile_list_from_json(proc.stdout or "[]")
        except (subprocess.SubprocessError, json.JSONDecodeError):
            return []

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def search(self, query: str, on_output: OutputFn, on_done: DoneFn) -> None:
        """Search packages in the background, streaming progress to callbacks."""
        threading.Thread(
            target=self._search_worker, args=(query, on_output, on_done), daemon=True
        ).start()

    def _search_worker(self, query: str, on_output: OutputFn, on_done: DoneFn) -> None:
        try:
            if self.is_nix:
                results = self._nix_search(query, on_output)
            else:
                results = self._native_search(query)
        except Exception as exc:  # pragma: no cover - defensive
            _idle(on_done, False, f"Ошибка поиска: {exc}")
            return
        _idle(on_done, True, "", results=results)

    def _nix_search(self, query: str, on_output: OutputFn) -> list[SearchResult]:
        _idle(on_output, "Поиск по Nixpkgs… (это может занять время)")
        cmd = ["nix", "search", NIXPKGS_CHANNEL, query, "--json"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _drain_until_done(proc, on_output)
        if proc.returncode != 0:
            return []

        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []

        results: list[SearchResult] = []
        for attr_path, meta in data.items():
            if not isinstance(meta, dict):
                continue
            desc = meta.get("description") or ""
            name = meta.get("name") or attr_path
            results.append(SearchResult(attribute_path=attr_path, name=name, description=desc))
        return results

    def _native_search(self, query: str) -> list[SearchResult]:
        cmd: list[str] = []
        if self._manager == "apt":
            cmd = ["apt", "search", query]
        elif self._manager == "pacman":
            cmd = ["pacman", "-Ss", query]
        elif self._manager == "dnf":
            cmd = ["dnf", "search", query]
        elif self._manager == "zypper":
            cmd = ["zypper", "search", query]
        else:
            return []

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return []

        results: list[SearchResult] = []
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(" - ", 1)
            attr = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            results.append(
                SearchResult(attribute_path=attr, name=attr.split("/")[-1], description=desc)
            )
        return results

    # ------------------------------------------------------------------ #
    # Install / remove
    # ------------------------------------------------------------------ #

    def install(
        self, package: str, on_output: OutputFn, on_done: DoneFn
    ) -> None:
        threading.Thread(
            target=self._run_op,
            args=(self._install_command(package), "установлен", on_output, on_done),
            daemon=True,
        ).start()

    def remove(
        self, package: str, on_output: OutputFn, on_done: DoneFn
    ) -> None:
        threading.Thread(
            target=self._run_op,
            args=(self._remove_command(package), "удалён", on_output, on_done),
            daemon=True,
        ).start()

    def _install_command(self, package: str) -> list[str]:
        if self.is_nix:
            attr = package if "." in package or "#" in package else f"{NIXPKGS_CHANNEL}#{package}"
            return ["nix", "profile", "install", attr]
        if self._manager == "apt":
            return self._privileged(["apt", "install", "-y", package])
        if self._manager == "pacman":
            return self._privileged(["pacman", "-S", "--noconfirm", package])
        if self._manager == "dnf":
            return self._privileged(["dnf", "install", "-y", package])
        if self._manager == "zypper":
            return self._privileged(["zypper", "install", "-y", package])
        return ["true"]

    def _remove_command(self, package: str) -> list[str]:
        if self.is_nix:
            return ["nix", "profile", "remove", package]
        if self._manager == "apt":
            return self._privileged(["apt", "remove", "-y", package])
        if self._manager == "pacman":
            return self._privileged(["pacman", "-R", "--noconfirm", package])
        if self._manager == "dnf":
            return self._privileged(["dnf", "remove", "-y", package])
        if self._manager == "zypper":
            return self._privileged(["zypper", "remove", "-y", package])
        return ["true"]

    @staticmethod
    def _privileged(cmd: list[str]) -> list[str]:
        """Wrap a command with pkexec when running as a regular user."""
        if os_geteuid() == 0:
            return cmd
        pkexec = shutil.which("pkexec")
        if pkexec:
            return [pkexec, *cmd]
        return ["sudo", *cmd]

    # ------------------------------------------------------------------ #
    # Command runner with streaming output
    # ------------------------------------------------------------------ #

    def _run_op(
        self,
        cmd: list[str],
        done_word: str,
        on_output: OutputFn,
        on_done: DoneFn,
    ) -> None:
        _idle(on_output, f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _drain_until_done(proc, on_output)
            ok = proc.returncode == 0
        except Exception as exc:  # pragma: no cover - defensive
            _idle(on_done, False, f"Не удалось выполнить команду: {exc}")
            return
        if ok:
            _idle(on_done, True, f"Готово: пакет {done_word}")
        else:
            _idle(on_done, False, f"Команда завершилась с ошибкой (код {proc.returncode})")


def _drain_until_done(proc: subprocess.Popen, on_output: OutputFn) -> None:
    """Read process output line by line, forwarding each line via GLib."""
    assert proc.stdout is not None
    for line in proc.stdout:
        stripped = line.rstrip("\n")
        if stripped:
            _idle(on_output, stripped)
    proc.wait()


def _idle(fn: Callable, *args, **kwargs) -> None:
    """Run ``fn`` on the GLib main loop if it is available, else directly."""
    try:
        from gi.repository import GLib

        GLib.idle_add(fn, *args, **kwargs)
    except Exception:
        fn(*args, **kwargs)


def os_geteuid() -> int:
    try:
        import os

        return os.geteuid()
    except AttributeError:
        return -1


def _nix_profile_list_from_json(raw: str) -> list[InstalledPackage]:
    """Parse the output of ``nix profile list --json``."""
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []

    result: list[InstalledPackage] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or ""
        attr = entry.get("attributePath") or ""
        store = (entry.get("storePaths") or [""])[0]
        original = entry.get("originalUrl") or ""
        result.append(
            InstalledPackage(
                name=name,
                attribute_path=attr,
                store_path=store,
                priority=str(entry.get("priority", "5")),
                original=original,
            )
        )
    return result
