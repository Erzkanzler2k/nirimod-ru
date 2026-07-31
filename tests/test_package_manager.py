"""Unit tests for the package manager backend."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from nirimod.package_manager import (
    InstalledPackage,
    PackageBackend,
    SearchResult,
    _drain_stderr_for_progress,
    _nix_profile_list_from_json,
)


def _sample_profile_json() -> str:
    return json.dumps(
        [
            {
                "name": "nirimod",
                "attributePath": "nirimod",
                "storePaths": ["/nix/store/4iah6512g2h51mj5sczndia2w2562mra-nirimod-0.1.0"],
                "priority": 5,
                "originalUrl": "github:Erzkanzler2k/nirimod-ru",
            },
            {
                "name": "ghostty",
                "attributePath": "ghostty",
                "storePaths": ["/nix/store/abc-ghostty-1.0.0"],
                "priority": 5,
                "originalUrl": "nixpkgs",
            },
        ]
    )


class TestInstalledPackages(unittest.TestCase):
    def test_parse_profile_list(self):
        packages = _nix_profile_list_from_json(_sample_profile_json())
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0].name, "nirimod")
        self.assertEqual(packages[0].store_path, "/nix/store/4iah6512g2h51mj5sczndia2w2562mra-nirimod-0.1.0")
        self.assertEqual(packages[1].name, "ghostty")

    def test_empty_profile(self):
        self.assertEqual(_nix_profile_list_from_json("[]"), [])
        self.assertEqual(_nix_profile_list_from_json("not json"), [])

    def test_new_object_format(self):
        obj = json.dumps(
            {
                "version": 2,
                "elements": {
                    "nirimod": {
                        "active": True,
                        "priority": 5,
                        "storePaths": ["/nix/store/4iah6512g2h51mj5sczndia2w2562mra-nirimod-0.1.0"],
                    }
                },
            }
        )
        packages = _nix_profile_list_from_json(obj)
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].name, "nirimod")


class TestDetection(unittest.TestCase):
    def test_detect_unknown(self):
        backend = PackageBackend()
        with patch("shutil.which", return_value=None):
            self.assertIsNone(backend._detect())

    def test_detect_prefers_nix(self):
        def fake_which(name):
            return "/usr/bin/" + name if name in ("nix", "apt") else None

        with patch("shutil.which", side_effect=fake_which):
            backend = PackageBackend()
            self.assertEqual(backend._detect(), "nix")


class TestSearchResult(unittest.TestCase):
    def test_short_name(self):
        res = SearchResult(attribute_path="nixpkgs.hello", name="hello", description="d")
        self.assertEqual(res.short_name, "hello")


class TestPrivileged(unittest.TestCase):
    def test_root_returns_plain(self):
        with patch("nirimod.package_manager.os_geteuid", return_value=0):
            self.assertEqual(PackageBackend._privileged(["apt", "install"]), ["apt", "install"])

    def test_pkexec_wrap(self):
        with patch("nirimod.package_manager.os_geteuid", return_value=1000), patch(
            "shutil.which", return_value="/usr/bin/pkexec"
        ):
            cmd = PackageBackend._privileged(["apt", "install"])
            self.assertEqual(cmd, ["/usr/bin/pkexec", "apt", "install"])


class TestInstallCommands(unittest.TestCase):
    def test_nix_install_uses_channel(self):
        backend = PackageBackend()
        backend._manager = "nix"
        self.assertEqual(
            backend._install_command("hello"),
            ["nix", "profile", "install", "nixpkgs#hello"],
        )

    def test_nix_install_full_attr(self):
        backend = PackageBackend()
        backend._manager = "nix"
        self.assertEqual(
            backend._install_command("nixpkgs#hello"),
            ["nix", "profile", "install", "nixpkgs#hello"],
        )

    def test_nix_remove(self):
        backend = PackageBackend()
        backend._manager = "nix"
        self.assertEqual(
            backend._remove_command("hello"), ["nix", "profile", "remove", "hello"]
        )

    def test_apt_install_privileged(self):
        backend = PackageBackend()
        backend._manager = "apt"
        with patch("nirimod.package_manager.os_geteuid", return_value=1000), patch(
            "shutil.which", return_value="/usr/bin/pkexec"
        ):
            self.assertEqual(
                backend._install_command("hello"),
                ["/usr/bin/pkexec", "apt", "install", "-y", "hello"],
            )


class TestInstalledPackageData(unittest.TestCase):
    def test_dataclass_fields(self):
        pkg = InstalledPackage(name="x", attribute_path="y", store_path="z", priority="5", original="nixpkgs")
        self.assertEqual(pkg.name, "x")
        self.assertEqual(pkg.priority, "5")


class TestDrainStderrForProgress(unittest.TestCase):
    def test_captures_stdout_and_throttles_progress(self):
        proc = subprocess.Popen(
            ["bash", "-c", "echo eval-line1 >&2; echo '{\"a\": 1}'; echo eval-line2 >&2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        seen: list[str] = []

        def _sync_idle(fn, *args, **kwargs):
            fn(*args, **kwargs)

        with patch("nirimod.package_manager._idle", side_effect=_sync_idle):
            stdout = _drain_stderr_for_progress(proc, seen.append)

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(stdout), {"a": 1})
        self.assertIn("eval-line1", seen)
        self.assertIn("eval-line2", seen)
