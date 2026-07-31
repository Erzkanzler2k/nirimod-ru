"""Backend for searching, installing and removing system packages.

Supports Nix (``nix profile``) and APT/pacman/dnf/zypper as a fallback.
All long-running operations run in a worker thread and stream output
back to the caller via callbacks (dispatched on the GLib main loop).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

NIXPKGS_CHANNEL = "nixpkgs"
WARMUP_QUERY = "hello"
CACHE_DIR = os.path.expanduser("~/.cache/nirimod")
SEARCH_CACHE_DIR = os.path.join(CACHE_DIR, "search")
POPULAR_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "popular.json"
)


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
        self._warmup_done = False
        self._warmup_active = False

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
        cached = _load_search_cache(query)
        if cached is not None:
            _idle(on_done, True, "", results=cached, cached=True)
            return
        try:
            if self.is_nix:
                results = self._nix_search(query, on_output)
            else:
                results = self._native_search(query)
        except Exception as exc:  # pragma: no cover - defensive
            _idle(on_done, False, f"Ошибка поиска: {exc}")
            return
        _save_search_cache(query, results)
        _idle(on_done, True, "", results=results)

    @property
    def warmup_active(self) -> bool:
        return self._warmup_active

    @property
    def warmup_done(self) -> bool:
        return self._warmup_done

    def warm_up_cache(
        self, on_output: OutputFn, on_done: DoneFn | None = None
    ) -> None:
        """Pre-populate Nix's eval cache in the background.

        ``nix search`` evaluates the whole Nixpkgs tree on first run, which
        can take minutes.  Warming the cache once at startup makes every
        subsequent search run in about a second.
        """
        if not self.is_nix or self._warmup_done or self._warmup_active:
            return
        self._warmup_active = True

        def _worker() -> None:
            try:
                self._nix_search(WARMUP_QUERY, on_output)
                self._warmup_done = True
                _idle(on_output, "Индекс Nixpkgs готов — поиск будет быстрым")
                if on_done:
                    _idle(on_done, True, "Готово")
            except Exception:  # pragma: no cover - defensive
                self._warmup_done = True
                if on_done:
                    _idle(on_done, False, "Не удалось подготовить индекс")
            finally:
                self._warmup_active = False

        threading.Thread(target=_worker, daemon=True).start()

    def _nix_search(self, query: str, on_output: OutputFn) -> list[SearchResult]:
        _idle(on_output, "Поиск по Nixpkgs… первый запрос может занять время")
        cmd = ["nix", "search", NIXPKGS_CHANNEL, query, "--json"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        raw_stdout = _drain_stderr_for_progress(proc, on_output)
        if proc.returncode != 0:
            return []

        try:
            data = json.loads(raw_stdout or "{}")
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


def _drain_stderr_for_progress(
    proc: subprocess.Popen, on_output: OutputFn
) -> str:
    """Read stderr line by line for progress and wait for the process to finish.

    Used for long-running commands (e.g. ``nix search``) whose stdout is a
    single JSON blob produced only at the end.  stderr lines like
    "evaluating 'legacyPackages...'" are forwarded as live progress
    (throttled to avoid flooding the log with thousands of lines).
    Returns the full stdout contents as a string.
    """
    assert proc.stdout is not None
    assert proc.stderr is not None

    captured: list[str] = []
    forwarded = 0
    PROGRESS_CAP = 5
    stderr_stream = proc.stderr
    stdout_stream = proc.stdout

    def _stderr_reader() -> None:
        nonlocal forwarded
        for line in stderr_stream:
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            if forwarded < PROGRESS_CAP:
                forwarded += 1
                _idle(on_output, stripped)
            elif forwarded == PROGRESS_CAP:
                forwarded += 1
                _idle(on_output, "… выполняется поиск …")

    def _stdout_reader() -> None:
        for line in stdout_stream:
            captured.append(line)

    t_err = threading.Thread(target=_stderr_reader, daemon=True)
    t_out = threading.Thread(target=_stdout_reader, daemon=True)
    t_err.start()
    t_out.start()
    proc.wait()
    t_err.join(timeout=5)
    t_out.join(timeout=5)
    return "".join(captured)


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
    """Parse the output of ``nix profile list --json``.

    Supports both the legacy array form and the newer object form
    ``{"elements": {"name": {...}}}``.
    """
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []

    if isinstance(data, dict):
        elements = data.get("elements", data)
    else:
        elements = data
    if isinstance(elements, dict):
        entries = [
            {**meta, "name": key}
            for key, meta in elements.items()
            if isinstance(meta, dict)
        ]
    else:
        entries = elements if isinstance(elements, list) else []

    result: list[InstalledPackage] = []
    for entry in entries:
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


def _search_cache_path(query: str) -> str:
    digest = hashlib.md5(query.encode("utf-8", errors="replace")).hexdigest()[:16]
    return os.path.join(SEARCH_CACHE_DIR, f"{digest}.json")


def _load_search_cache(query: str) -> list[SearchResult] | None:
    """Return cached results for ``query`` or None when no cache exists."""
    try:
        with open(_search_cache_path(query), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        results.append(
            SearchResult(
                attribute_path=str(item.get("attribute_path", "")),
                name=str(item.get("name", "")),
                description=str(item.get("description", "")),
            )
        )
    return results


def _save_search_cache(query: str, results: list[SearchResult]) -> None:
    try:
        os.makedirs(SEARCH_CACHE_DIR, exist_ok=True)
        with open(_search_cache_path(query), "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "attribute_path": r.attribute_path,
                        "name": r.name,
                        "description": r.description,
                    }
                    for r in results
                ],
                fh,
                ensure_ascii=False,
            )
    except OSError:  # pragma: no cover - cache is best-effort
        pass


def load_popular_packages() -> list[SearchResult]:
    """Return the bundled list of popular Nixpkgs packages (instant, offline)."""
    try:
        with open(POPULAR_REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):  # pragma: no cover - bundled file
        return []
    results: list[SearchResult] = []
    for item in data.get("packages", []):
        if not isinstance(item, dict):
            continue
        attr = item.get("attr", "")
        if not attr:
            continue
        results.append(
            SearchResult(
                attribute_path=attr,
                name=item.get("name", attr),
                description=item.get("description", ""),
            )
        )
    return results
