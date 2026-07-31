"""Packages page: install Nix packages and niri add-ons visually."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from nirimod import addons
from nirimod.package_manager import PackageBackend, SearchResult
from nirimod.pages.base import BasePage


class PackagesPage(BasePage):
    def build(self) -> Gtk.Widget:
        tb, header, _, content = self._make_toolbar_page("Пакеты")

        self._backend = PackageBackend()

        self._status_label = Gtk.Label(label="", xalign=0)
        self._status_label.set_opacity(0.7)
        self._status_label.set_wrap(True)

        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.add_css_class("monospace")
        self._log_buffer = self._log_view.get_buffer()
        self._log_revealer = Gtk.Revealer()
        self._log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._log_revealer.set_transition_duration(150)
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroll.set_max_content_height(220)
        log_scroll.set_child(self._log_view)
        self._log_revealer.set_child(log_scroll)

        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(18, 18)
        self._spinner.set_tooltip_text("Выполняется операция\u2026")

        self._busy = False

        view_stack = Adw.ViewStack()
        view_switcher = Adw.ViewSwitcher(stack=view_stack)
        view_switcher.set_policy(Adw.ViewSwitcherPolicy.NARROW)
        header.set_title_widget(view_switcher)

        nix_page = view_stack.add_titled_with_icon(
            self._build_nix_tab(), "nix", "Nix-пакеты", "nix-symbolic"
        )
        nix_page.set_icon_name("system-package-install-symbolic")

        addons_page = view_stack.add_titled_with_icon(
            self._build_addons_tab(), "addons", "Дополнения для niri", "extension-symbolic"
        )
        addons_page.set_icon_name("puzzle-piece-symbolic")

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.set_margin_top(8)
        status_box.append(self._spinner)
        status_box.append(self._status_label)
        content.append(status_box)
        content.append(self._log_revealer)

        return tb

    # ------------------------------------------------------------------ #
    # Nix packages tab
    # ------------------------------------------------------------------ #

    def _build_nix_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Поиск пакетов в Nixpkgs\u2026")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("activate", self._on_nix_search)
        header_box.append(self._search_entry)

        search_btn = Gtk.Button(label="Найти")
        search_btn.add_css_class("suggested-action")
        search_btn.connect("clicked", self._on_nix_search)
        header_box.append(search_btn)
        box.append(header_box)

        manager_hint = Adw.PreferencesGroup(
            title="Менеджер пакетов",
            description=(
                f"Обнаружен: {self._backend.human_name}. "
                "Операции выполняются напрямую из этого окна."
            ),
        )
        box.append(manager_hint)

        self._nix_installed_grp = Adw.PreferencesGroup(title="Установленные пакеты")
        box.append(self._nix_installed_grp)

        self._nix_results_grp = Adw.PreferencesGroup(title="Результаты поиска")
        box.append(self._nix_results_grp)

        return box

    def _on_nix_search(self, *_):
        query = self._search_entry.get_text().strip()
        if not query:
            return
        self._set_busy(True, "Поиск\u2026")
        self._clear_group(self._nix_results_grp)
        self._backend.search(
            query,
            on_output=self._append_log,
            on_done=self._on_search_done,
        )

    def _on_search_done(self, ok: bool, msg: str, results=None):
        self._set_busy(False)
        self._append_log(msg)
        if not ok:
            self._set_status(msg, error=True)
            return
        self._set_status(f"Найдено: {len(results or [])}")
        self._clear_group(self._nix_results_grp)
        for res in results or []:
            self._nix_results_grp.add(self._make_result_row(res))

    def _make_result_row(self, res: SearchResult) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=res.short_name,
            subtitle=res.attribute_path,
        )
        if res.description:
            row.set_tooltip_text(res.description)
        install_btn = Gtk.Button(label="Установить")
        install_btn.add_css_class("suggested-action")
        install_btn.connect(
            "clicked", lambda *_, r=res: self._install_package(r.attribute_path)
        )
        row.add_suffix(install_btn)
        return row

    def _install_package(self, attr: str):
        self._set_busy(True, f"Установка {attr}\u2026")
        self._backend.install(attr, on_output=self._append_log, on_done=self._on_op_done)

    # ------------------------------------------------------------------ #
    # Installed packages list
    # ------------------------------------------------------------------ #

    def refresh(self):
        self._reload_installed()

    def on_shown(self):
        self._reload_installed()

    def _reload_installed(self):
        installed = self._backend.installed_packages()
        self._clear_group(self._nix_installed_grp)
        if not installed:
            row = Adw.ActionRow(title="Пакетов не найдено")
            self._nix_installed_grp.add(row)
            return
        for pkg in installed:
            row = Adw.ActionRow(
                title=pkg.name,
                subtitle=pkg.store_path,
            )
            remove_btn = Gtk.Button(label="Удалить")
            remove_btn.add_css_class("destructive-action")
            remove_btn.connect(
                "clicked", lambda *_, p=pkg: self._remove_package(p)
            )
            row.add_suffix(remove_btn)
            self._nix_installed_grp.add(row)

    def _remove_package(self, pkg):
        self._set_busy(True, f"Удаление {pkg.name}\u2026")
        self._backend.remove(pkg.name, on_output=self._append_log, on_done=self._on_op_done)

    def _on_op_done(self, ok: bool, msg: str):
        self._set_busy(False)
        self._set_status(msg, error=not ok)
        self._append_log(msg)
        if ok:
            self._reload_installed()

    # ------------------------------------------------------------------ #
    # Add-ons tab
    # ------------------------------------------------------------------ #

    def _build_addons_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self._addons_grp = Adw.PreferencesGroup(
            title="Дополнения для niri",
            description="Готовые оболочки, конфигурации и утилиты. Установка и удаление выполняются в этом окне.",
        )
        box.append(self._addons_grp)

        self._addons = addons.load_local()
        for addon in self._addons:
            self._addons_grp.add(self._make_addon_row(addon))

        return box

    def _make_addon_row(self, addon: addons.Addon) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=addon.name,
            subtitle=addon.description,
        )
        category_lbl = Gtk.Label(label=addon.category)
        category_lbl.add_css_class("nm-badge")
        category_lbl.set_valign(Gtk.Align.CENTER)
        row.add_suffix(category_lbl)

        install_btn = Gtk.Button(label="Установить")
        install_btn.add_css_class("suggested-action")
        install_btn.connect(
            "clicked",
            lambda *_, a=addon, b=install_btn: self._run_addon(a, b, install=True),
        )
        row.add_suffix(install_btn)

        remove_btn = Gtk.Button(label="Удалить")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect(
            "clicked",
            lambda *_, a=addon, b=remove_btn: self._run_addon(a, b, install=False),
        )
        row.add_suffix(remove_btn)
        return row

    def _run_addon(self, addon: addons.Addon, btn: Gtk.Button, install: bool):
        commands = addon.install_commands if install else addon.remove_commands
        label = "Установка" if install else "Удаление"
        self._set_busy(True, f"{label} «{addon.name}»\u2026")
        addons.run_addon_commands(
            commands,
            on_output=self._append_log,
            on_done=self._on_addon_done,
        )

    def _on_addon_done(self, ok: bool, msg: str):
        self._set_busy(False)
        self._set_status(msg, error=not ok)
        self._append_log(msg)

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def _set_busy(self, busy: bool, status: str = ""):
        self._busy = busy
        if busy:
            self._spinner.start()
            self._log_revealer.set_reveal_child(True)
            self._set_status(status)
        else:
            self._spinner.stop()

    def _set_status(self, text: str, error: bool = False):
        self._status_label.set_text(text)
        self._status_label.remove_css_class("error")
        if error:
            self._status_label.add_css_class("error")

    def _append_log(self, line: str):
        if line:
            self._log_buffer.insert(self._log_buffer.get_end_iter(), line + "\n")

    @staticmethod
    def _clear_group(group: Adw.PreferencesGroup):
        row = group.get_first_child()
        while row:
            group.remove(row)
            row = group.get_first_child()
