"""Unit tests for the add-ons registry."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from nirimod import addons
from nirimod.addons import Addon


SAMPLE = {
    "addons": [
        {
            "id": "dms",
            "name": "Dank Material Shell",
            "description": "A shell for niri",
            "category": "Оболочки",
            "repo": "https://example.org/dms",
            "install": ["git clone https://example.org/dms ~/.local/share/dms"],
            "remove": ["rm -rf ~/.local/share/dms"],
        }
    ]
}


class TestAddonFromDict(unittest.TestCase):
    def test_full_dict(self):
        addon = Addon.from_dict(SAMPLE["addons"][0])
        self.assertEqual(addon.id, "dms")
        self.assertEqual(addon.name, "Dank Material Shell")
        self.assertEqual(addon.category, "Оболочки")
        self.assertEqual(len(addon.install_commands), 1)
        self.assertEqual(addon.remove_commands, ["rm -rf ~/.local/share/dms"])

    def test_empty_dict(self):
        addon = Addon.from_dict({})
        self.assertEqual(addon.name, "Без названия")
        self.assertEqual(addon.category, "прочее")
        self.assertEqual(addon.install_commands, [])


class TestLoadLocal(unittest.TestCase):
    def test_loads_bundled_registry(self):
        addon_list = addons.load_local()
        self.assertIsInstance(addon_list, list)
        for addon in addon_list:
            self.assertIsInstance(addon, Addon)
            self.assertTrue(addon.id)

    def test_missing_file_returns_empty(self):
        with patch("nirimod.addons.LOCAL_REGISTRY") as missing:
            missing.__str__ = lambda self: "/nonexistent/addons.json"
            with patch("builtins.open", side_effect=FileNotFoundError):
                self.assertEqual(addons.load_local(), [])


class TestFetchRemote(unittest.TestCase):
    def test_success_callback(self):
        def _fake_urlopen(req, timeout):
            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self):
                    return json.dumps(SAMPLE).encode()

            return Resp()

        results: dict = {}

        def _cb(ok: bool, msg: str, **kw):
            results["ok"] = ok
            results["count"] = len(kw.get("addons", []))

        def _sync_idle(fn, *args, **kwargs):
            fn(*args, **kwargs)

        with patch("nirimod.addons.urllib.request.urlopen", side_effect=_fake_urlopen), patch(
            "nirimod.addons._idle", side_effect=_sync_idle
        ):
            thread = addons.fetch_remote(_cb)
            thread.join(timeout=5)

        self.assertTrue(results.get("ok"))
        self.assertEqual(results.get("count"), 1)
