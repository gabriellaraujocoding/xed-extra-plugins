# -*- coding: utf-8 -*-
#
# Copyright (c) 2005-2006 Paolo Borelli
# Copyright (c) 2025 Gabriell Araujo (Xed port)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""
Terminal plugin for Xed (Linux Mint).

Highlights
- Embedded VTE terminal in Xed's bottom panel.
- Multiple terminal tabs.
- Preferences dialog (right-click inside the terminal):
  - Font: system monospace or custom
  - Colors: theme colors or custom foreground/background
  - Palette: edit all 16 ANSI colors
  - Cursor: blink and shape
  - Scrollback: unlimited or fixed lines
  - Scroll on output / keystroke
  - Audible bell

Debug
- Set XED_DEBUG_TERMINAL=1 to print debug logs.
"""

from __future__ import annotations

import os, sys
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("Vte", "2.91")

from gi.repository import GObject, Gtk, Gdk, Gio, GLib, Pango, Vte  # noqa: E402

try:
    gi.require_version("Xed", "1.0")
    from gi.repository import Xed  # type: ignore
except Exception:  # pragma: no cover
    Xed = None  # type: ignore


# -------------------------
# Helpers / logging
# -------------------------

def _env_truthy(name: str) -> bool:
    v = GLib.getenv(name)
    if v is None:
        return False
    v = v.strip().lower()
    return v not in ("", "0", "false", "no", "off")
    
_DEBUG = _env_truthy("XED_DEBUG_TERMINAL")    

def _debug(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[xed-terminal] {msg}\n")

def _rgba_from_string(s: str) -> Optional[Gdk.RGBA]:
    try:
        rgba = Gdk.RGBA()
        if rgba.parse(s):
            return rgba
    except Exception:
        pass
    return None


def _palette_from_string(s: str) -> List[Gdk.RGBA]:
    """Parse a 16-color VTE palette from a string.

    Supported separators:
      - '|' (preferred; safe with 'rgb(...)' strings)
      - newlines
      - ';'
      - ',' (legacy; only safe for hex colors)
    """
    s = (s or "").strip()
    if not s:
        return []

    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
    elif "\n" in s:
        parts = [p.strip() for p in s.splitlines() if p.strip()]
    elif ";" in s:
        parts = [p.strip() for p in s.split(";") if p.strip()]
    else:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        # If it looks like we split rgb()/rgba() strings into many chunks, bail out.
        if len(parts) > 32 and any(("rgb(" in p or "rgba(" in p) for p in parts):
            return []

    palette: List[Gdk.RGBA] = []
    for p in parts:
        rgba = _rgba_from_string(p)
        if rgba is None:
            return []
        palette.append(rgba)

    if len(palette) != 16:
        return []

    return palette

def _palette_to_string(colors: List[Gdk.RGBA]) -> str:
    """Serialize a 16-color palette.

    IMPORTANT: We must NOT use ',' as a separator because Gdk.RGBA.to_string()
    may produce 'rgb(r,g,b)' strings containing commas.
    """
    if len(colors) != 16:
        return ""
    return "|".join([c.to_string() for c in colors])


# -------------------------
# Settings storage (file-based)
# -------------------------


class TerminalSettingsStore:
    """Simple per-user settings stored in an INI file.

    This avoids requiring custom GSettings schemas installation.
    """

    GROUP = "Terminal"

    DEFAULTS = {
        # Font
        "use_system_font": True,
        "font": "Monospace 10",

        # Colors
        "use_theme_colors": False,
        "foreground_color": "#363636",
        "background_color": "#FFFFFF",
        # 16-color ANSI palette (defaults)
        "palette": (
        "#363636|#363636|#363636|#363636|#363636|#363636|#363636|#363636|"
        "#363636|#363636|#363636|#363636|#363636|#363636|#363636|#363636"
        ),

        # Cursor
        "cursor_blink": True,
        "cursor_shape": int(Vte.CursorShape.BLOCK),

        # Behavior
        "audible_bell": False,
        "scroll_on_keystroke": True,
        "scroll_on_output": False,
        "scrollback_unlimited": True,
        "scrollback_lines": 10000,
    }

    def __init__(self) -> None:
        config_dir = os.path.join(GLib.get_user_config_dir(), "xed", "plugins", "xed-terminal")
        os.makedirs(config_dir, exist_ok=True)
        self._path = os.path.join(config_dir, "settings.ini")

        self._keyfile = GLib.KeyFile()
        self._load()

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> None:
        try:
            self._keyfile.load_from_file(self._path, GLib.KeyFileFlags.NONE)
        except Exception:
            # Create defaults on first run.
            for k, v in self.DEFAULTS.items():
                self._set_value(k, v)
            self.save()

    def save(self) -> None:
        """Persist settings to disk (INI) in a robust way.

        We intentionally avoid GLib.file_set_contents() because its GI binding
        signature varies across distributions / Python-GI versions.
        """
        try:
            data, _length = self._keyfile.to_data()
            if isinstance(data, str):
                data_bytes = data.encode("utf-8")
            else:
                data_bytes = bytes(data)

            tmp_path = self._path + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(data_bytes)
            os.replace(tmp_path, self._path)
        except Exception as e:
            _debug(f"Failed to save settings: {e!r}")

    def _set_value(self, key: str, value) -> None:
        # NOTE: bool is a subclass of int in Python; check bool first.
        if isinstance(value, bool):
            self._keyfile.set_boolean(self.GROUP, key, bool(value))
        elif isinstance(value, int):
            self._keyfile.set_integer(self.GROUP, key, int(value))
        else:
            self._keyfile.set_string(self.GROUP, key, str(value))

    def get_bool(self, key: str) -> bool:
        try:
            return self._keyfile.get_boolean(self.GROUP, key)
        except Exception:
            return bool(self.DEFAULTS.get(key, False))

    def set_bool(self, key: str, value: bool) -> None:
        self._set_value(key, bool(value))

    def get_int(self, key: str) -> int:
        try:
            return int(self._keyfile.get_integer(self.GROUP, key))
        except Exception:
            return int(self.DEFAULTS.get(key, 0))

    def set_int(self, key: str, value: int) -> None:
        self._set_value(key, int(value))

    def get_str(self, key: str) -> str:
        try:
            return self._keyfile.get_string(self.GROUP, key)
        except Exception:
            return str(self.DEFAULTS.get(key, ""))

    def set_str(self, key: str, value: str) -> None:
        self._set_value(key, str(value))


# -------------------------
# Preferences dialog
# -------------------------


class TerminalPreferencesDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, store: TerminalSettingsStore) -> None:
        super().__init__(title="Terminal Preferences", transient_for=parent, modal=True)

        self._store = store

        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_Save", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        self.set_border_width(10)
        self.set_resizable(False)

        box = self.get_content_area()
        grid = Gtk.Grid(row_spacing=10, column_spacing=10)
        box.add(grid)

        row = 0

        # Font
        self._use_system_font = Gtk.CheckButton.new_with_label("Use system monospace font")
        self._use_system_font.set_active(self._store.get_bool("use_system_font"))
        grid.attach(self._use_system_font, 0, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label="Font:"), 0, row, 1, 1)
        self._font_btn = Gtk.FontButton()
        self._font_btn.set_use_font(True)
        self._font_btn.set_font(self._store.get_str("font"))
        grid.attach(self._font_btn, 1, row, 1, 1)
        row += 1

        # Colors
        self._use_theme_colors = Gtk.CheckButton.new_with_label("Use Xed theme colors")
        self._use_theme_colors.set_active(self._store.get_bool("use_theme_colors"))
        grid.attach(self._use_theme_colors, 0, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label="Foreground:"), 0, row, 1, 1)
        self._fg_btn = Gtk.ColorButton()
        fg = _rgba_from_string(self._store.get_str("foreground_color")) or Gdk.RGBA(1, 1, 1, 1)
        self._fg_btn.set_rgba(fg)
        self._fg_btn.set_use_alpha(False)
        grid.attach(self._fg_btn, 1, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Background:"), 0, row, 1, 1)
        self._bg_btn = Gtk.ColorButton()
        bg = _rgba_from_string(self._store.get_str("background_color")) or Gdk.RGBA(0, 0, 0, 1)
        self._bg_btn.set_rgba(bg)
        self._bg_btn.set_use_alpha(False)
        grid.attach(self._bg_btn, 1, row, 1, 1)
        row += 1

        # Palette
        grid.attach(Gtk.Label(label="Palette (16 colors):"), 0, row, 2, 1)
        row += 1

        self._palette_btns: List[Gtk.ColorButton] = []
        pal = _palette_from_string(self._store.get_str("palette"))
        if len(pal) != 16:
            pal = _palette_from_string(str(self._store.DEFAULTS.get("palette", "")))

        pal_grid = Gtk.Grid(row_spacing=6, column_spacing=6)
        grid.attach(pal_grid, 0, row, 2, 1)
        row += 1

        # 2 rows × 8 columns
        for i in range(16):
            btn = Gtk.ColorButton()
            btn.set_use_alpha(False)
            btn.set_title(f"Palette color {i}")
            if i < len(pal):
                btn.set_rgba(pal[i])
            self._palette_btns.append(btn)
            pal_grid.attach(btn, i % 8, i // 8, 1, 1)

        # Cursor
        self._cursor_blink = Gtk.CheckButton.new_with_label("Blinking cursor")
        self._cursor_blink.set_active(self._store.get_bool("cursor_blink"))
        grid.attach(self._cursor_blink, 0, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label="Cursor shape:"), 0, row, 1, 1)
        self._cursor_shape = Gtk.ComboBoxText()
        self._cursor_shape.append_text("Block")
        self._cursor_shape.append_text("I-Beam")
        self._cursor_shape.append_text("Underline")
        shape = self._store.get_int("cursor_shape")
        idx = 0
        if shape == int(Vte.CursorShape.IBEAM):
            idx = 1
        elif shape == int(Vte.CursorShape.UNDERLINE):
            idx = 2
        self._cursor_shape.set_active(idx)
        grid.attach(self._cursor_shape, 1, row, 1, 1)
        row += 1

        # Behavior
        self._audible_bell = Gtk.CheckButton.new_with_label("Audible bell")
        self._audible_bell.set_active(self._store.get_bool("audible_bell"))
        grid.attach(self._audible_bell, 0, row, 2, 1)
        row += 1

        self._scroll_on_keystroke = Gtk.CheckButton.new_with_label("Scroll on keystroke")
        self._scroll_on_keystroke.set_active(self._store.get_bool("scroll_on_keystroke"))
        grid.attach(self._scroll_on_keystroke, 0, row, 2, 1)
        row += 1

        self._scroll_on_output = Gtk.CheckButton.new_with_label("Scroll on output")
        self._scroll_on_output.set_active(self._store.get_bool("scroll_on_output"))
        grid.attach(self._scroll_on_output, 0, row, 2, 1)
        row += 1

        self._scrollback_unlimited = Gtk.CheckButton.new_with_label("Unlimited scrollback")
        self._scrollback_unlimited.set_active(self._store.get_bool("scrollback_unlimited"))
        grid.attach(self._scrollback_unlimited, 0, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label="Scrollback lines:"), 0, row, 1, 1)
        self._scrollback_lines = Gtk.SpinButton.new_with_range(100, 1363636, 100)
        self._scrollback_lines.set_value(self._store.get_int("scrollback_lines"))
        grid.attach(self._scrollback_lines, 1, row, 1, 1)
        row += 1

        # Enable/disable controls based on toggles.
        self._use_system_font.connect("toggled", self._on_any_toggle)
        self._use_theme_colors.connect("toggled", self._on_any_toggle)
        self._scrollback_unlimited.connect("toggled", self._on_any_toggle)
        self._on_any_toggle(None)

        self.show_all()

    def _on_any_toggle(self, _btn) -> None:
        self._font_btn.set_sensitive(not self._use_system_font.get_active())
        use_theme = self._use_theme_colors.get_active()
        self._fg_btn.set_sensitive(not use_theme)
        self._bg_btn.set_sensitive(not use_theme)
        self._scrollback_lines.set_sensitive(not self._scrollback_unlimited.get_active())

    def save_to_store(self) -> None:
        self._store.set_bool("use_system_font", self._use_system_font.get_active())
        self._store.set_str("font", self._font_btn.get_font())

        self._store.set_bool("use_theme_colors", self._use_theme_colors.get_active())
        self._store.set_str("foreground_color", self._fg_btn.get_rgba().to_string())
        self._store.set_str("background_color", self._bg_btn.get_rgba().to_string())

        # Palette
        pal = [b.get_rgba() for b in self._palette_btns]
        pal_s = _palette_to_string(pal)
        if pal_s:
            self._store.set_str("palette", pal_s)

        self._store.set_bool("cursor_blink", self._cursor_blink.get_active())
        shape_idx = self._cursor_shape.get_active()
        shape = int(Vte.CursorShape.BLOCK)
        if shape_idx == 1:
            shape = int(Vte.CursorShape.IBEAM)
        elif shape_idx == 2:
            shape = int(Vte.CursorShape.UNDERLINE)
        self._store.set_int("cursor_shape", shape)

        self._store.set_bool("audible_bell", self._audible_bell.get_active())
        self._store.set_bool("scroll_on_keystroke", self._scroll_on_keystroke.get_active())
        self._store.set_bool("scroll_on_output", self._scroll_on_output.get_active())
        self._store.set_bool("scrollback_unlimited", self._scrollback_unlimited.get_active())
        self._store.set_int("scrollback_lines", int(self._scrollback_lines.get_value_as_int()))

        self._store.save()


# -------------------------
# VTE terminal widget
# -------------------------


class XedTerminal(Vte.Terminal):
    TARGET_URI_LIST = 200

    def __init__(self, store: TerminalSettingsStore):
        super().__init__()
        self._store = store

        self.set_size(self.get_column_count(), 5)
        self.set_size_request(200, 50)

        # Drag & drop URIs
        tl = Gtk.TargetList.new([])
        tl.add_uri_targets(self.TARGET_URI_LIST)
        self.drag_dest_set(
            Gtk.DestDefaults.HIGHLIGHT | Gtk.DestDefaults.DROP,
            [],
            Gdk.DragAction.DEFAULT | Gdk.DragAction.COPY,
        )
        self.drag_dest_set_target_list(tl)

        # React to system monospace font changes
        self._system_settings = Gio.Settings.new("org.gnome.desktop.interface")
        try:
            self._system_settings.connect("changed::monospace-font-name", self._on_system_font_changed)
        except Exception:
            pass

        self.reconfigure_vte()

        # Start shell
        self.spawn_async(
            Vte.PtyFlags.DEFAULT,
            None,
            [Vte.get_user_shell()],
            None,
            GLib.SpawnFlags.SEARCH_PATH,
            None,
            None,
            -1,
            None,
            None,
        )

    def do_drag_data_received(self, drag_context, x, y, data, info, time):
        if info == self.TARGET_URI_LIST:
            try:
                uris = data.get_uris() or []
            except Exception:
                uris = []

            paths: List[str] = []
            for u in uris:
                try:
                    p = Gio.File.new_for_uri(u).get_path()
                    if p:
                        paths.append("'" + p.replace("'", r"'\\''") + "'")
                except Exception:
                    continue

            if paths:
                self.feed_child((" ".join(paths) + " ").encode("utf-8"))

            Gtk.drag_finish(drag_context, True, False, time)
            return

        super().do_drag_data_received(drag_context, x, y, data, info, time)

    def _on_system_font_changed(self, _settings: Gio.Settings, _key: str) -> None:
        # Only relevant if using system font
        if self._store.get_bool("use_system_font"):
            self.reconfigure_vte()

    def _get_font_string(self) -> str:
        if self._store.get_bool("use_system_font"):
            try:
                return self._system_settings.get_string("monospace-font-name")
            except Exception:
                return "Monospace 10"
        return self._store.get_str("font")

    def reconfigure_vte(self) -> None:
        # Font
        try:
            self.set_font(Pango.font_description_from_string(self._get_font_string()))
        except Exception:
            pass

        # Colors
        ctx = self.get_style_context()
        try:
            fg = ctx.get_color(Gtk.StateFlags.NORMAL)
        except Exception:
            fg = Gdk.RGBA(1, 1, 1, 1)

        try:
            bg = ctx.get_background_color(Gtk.StateFlags.NORMAL)
        except Exception:
            bg = Gdk.RGBA(0, 0, 0, 1)

        if not self._store.get_bool("use_theme_colors"):
            fg2 = _rgba_from_string(self._store.get_str("foreground_color"))
            bg2 = _rgba_from_string(self._store.get_str("background_color"))
            if fg2 is not None:
                fg = fg2
            if bg2 is not None:
                bg = bg2

        palette = _palette_from_string(self._store.get_str("palette"))

        try:
            self.set_colors(fg, bg, palette)
        except Exception:
            pass

        # Cursor
        try:
            blink = self._store.get_bool("cursor_blink")
            self.set_cursor_blink_mode(Vte.CursorBlinkMode.ON if blink else Vte.CursorBlinkMode.OFF)
        except Exception:
            pass

        try:
            self.set_cursor_shape(Vte.CursorShape(self._store.get_int("cursor_shape")))
        except Exception:
            pass

        # Behavior
        try:
            self.set_audible_bell(self._store.get_bool("audible_bell"))
        except Exception:
            pass

        try:
            self.set_scroll_on_keystroke(self._store.get_bool("scroll_on_keystroke"))
        except Exception:
            pass

        try:
            self.set_scroll_on_output(self._store.get_bool("scroll_on_output"))
        except Exception:
            pass

        # Scrollback
        try:
            if self._store.get_bool("scrollback_unlimited"):
                self.set_scrollback_lines(-1)
            else:
                self.set_scrollback_lines(self._store.get_int("scrollback_lines"))
        except Exception:
            pass


# -------------------------
# Notebook (tabs) and pages
# -------------------------


class TerminalPage(Gtk.Box):
    def __init__(self, store: TerminalSettingsStore, notebook: "TerminalNotebook") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._store = store
        self._notebook = notebook

        self._vte = XedTerminal(store)
        self._vte.show()
        self.pack_start(self._vte, True, True, 0)

        scrollbar = Gtk.Scrollbar.new(Gtk.Orientation.VERTICAL, self._vte.get_vadjustment())
        scrollbar.show()
        self.pack_start(scrollbar, False, False, 0)

        self._vte.connect("child-exited", self._on_vte_child_exited)
        self._vte.connect("button-press-event", self._on_vte_button_press)
        self._vte.connect("popup-menu", self._on_vte_popup_menu)
        self._vte.connect("key-press-event", self._on_vte_key_press)

    def apply_settings(self) -> None:
        self._vte.reconfigure_vte()

    def grab_focus_terminal(self) -> None:
        self._vte.grab_focus()

    def copy_clipboard(self) -> None:
        try:
            self._vte.copy_clipboard()
        except Exception:
            pass
        self._vte.grab_focus()

    def paste_clipboard(self) -> None:
        try:
            self._vte.paste_clipboard()
        except Exception:
            pass
        self._vte.grab_focus()

    def change_directory(self, path: str) -> None:
        if not path:
            return
        safe = path.replace("\\", "\\\\").replace('"', '\\"')
        try:
            self._vte.feed_child((f'cd "{safe}"\n').encode("utf-8"))
        except Exception:
            pass
        self._vte.grab_focus()

    def _on_vte_child_exited(self, _term, _status) -> None:
        # Restart the shell if it exits.
        _debug("Shell exited; restarting.")
        try:
            self._notebook.shutdown()
        except Exception:
            pass

        self._vte = XedTerminal(self._store)
        self._vte.show()

        # Replace the first child (terminal) in the box
        children = self.get_children()
        if children:
            try:
                self.remove(children[0])
            except Exception:
                pass
        self.pack_start(self._vte, True, True, 0)
        self.reorder_child(self._vte, 0)

        self._vte.connect("child-exited", self._on_vte_child_exited)
        self._vte.connect("button-press-event", self._on_vte_button_press)
        self._vte.connect("popup-menu", self._on_vte_popup_menu)
        self._vte.connect("key-press-event", self._on_vte_key_press)
        self._vte.grab_focus()

    def _on_vte_button_press(self, _term, event) -> bool:
        if event.button == 3:
            self._vte.grab_focus()
            self._make_popup(event)
            return True
        return False

    def _on_vte_popup_menu(self, _term) -> None:
        self._make_popup()

    def _create_popup_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        item = Gtk.MenuItem.new_with_label("Copy")
        item.connect("activate", lambda _mi: self.copy_clipboard())
        item.set_sensitive(self._vte.get_has_selection())
        menu.append(item)

        item = Gtk.MenuItem.new_with_label("Paste")
        item.connect("activate", lambda _mi: self.paste_clipboard())
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem.new_with_label("New Terminal Tab")
        item.connect("activate", lambda _mi: self._notebook.add_terminal_tab())
        menu.append(item)

        item = Gtk.MenuItem.new_with_label("Close Terminal Tab")
        item.set_sensitive(self._notebook.get_n_pages() > 1)
        item.connect("activate", lambda _mi: self._notebook.close_terminal_tab(self))
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem.new_with_label("Preferences…")
        item.connect("activate", lambda _mi: self._notebook.open_preferences())
        menu.append(item)

        # Allow plugin to inject items (e.g., Change Directory)
        self._notebook.emit("populate-popup", self, menu)

        menu.show_all()
        return menu

    def _make_popup(self, event=None) -> None:
        menu = self._create_popup_menu()
        menu.attach_to_widget(self, None)
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(self, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.SOUTH_WEST, None)
            menu.select_first(False)
            
    def _on_vte_key_press(self, _term, event) -> bool:
        """Handle terminal copy/paste shortcuts without breaking Ctrl+C (SIGINT).
        - Copy:  Ctrl+Shift+C (and Ctrl+Insert)
        - Paste: Ctrl+Shift+V (and Shift+Insert)
        """
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        key = Gdk.keyval_to_upper(event.keyval)

        # Common terminal shortcuts
        if mods == (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            if key == Gdk.KEY_C:
                if self._vte.get_has_selection():
                    self.copy_clipboard()
                return True
            if key == Gdk.KEY_V:
                self.paste_clipboard()
                return True

        # Legacy X11-style shortcuts
        if mods == Gdk.ModifierType.CONTROL_MASK and key in (Gdk.KEY_Insert, getattr(Gdk, "KEY_KP_Insert", Gdk.KEY_Insert)):
            if self._vte.get_has_selection():
                self.copy_clipboard()
            return True

        if mods == Gdk.ModifierType.SHIFT_MASK and key in (Gdk.KEY_Insert, getattr(Gdk, "KEY_KP_Insert", Gdk.KEY_Insert)):
            self.paste_clipboard()
            return True

        return False
      
        

class TerminalNotebook(Gtk.Notebook):
    """Notebook managing multiple terminal tabs."""

    __gsignals__ = {
        "populate-popup": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (GObject.TYPE_OBJECT, GObject.TYPE_OBJECT),
        )
    }

    def __init__(self, store: TerminalSettingsStore) -> None:
        super().__init__()
        self._store = store

        self.set_scrollable(True)
        self.set_show_border(True)
        self.set_show_tabs(True)

        self._pages: List[TerminalPage] = []

        # Always start with one terminal
        self.add_terminal_tab()

        self.connect("switch-page", self._on_switch_page)

    def _tab_label(self, idx: int) -> Gtk.Label:
        lbl = Gtk.Label(label=f"Terminal {idx}")
        lbl.set_xalign(0.0)
        return lbl

    def _renumber_tabs(self) -> None:
        for i, p in enumerate(self._pages, start=1):
            try:
                self.set_tab_label(p, self._tab_label(i))
            except Exception:
                pass

    def add_terminal_tab(self) -> None:
        page = TerminalPage(self._store, self)
        page.show()
        self._pages.append(page)

        idx = len(self._pages)
        self.append_page(page, self._tab_label(idx))
        self.set_current_page(self.page_num(page))

        page.grab_focus_terminal()

    def close_terminal_tab(self, page: Optional[TerminalPage] = None) -> None:
        if len(self._pages) <= 1:
            return

        if page is None:
            page_num = self.get_current_page()
            page = self.get_nth_page(page_num)
            if page is None:
                return

        # Resolve page number (may not be current)
        try:
            page_num = self.page_num(page)
        except Exception:
            return

        if page_num < 0:
            return

        try:
            self.remove_page(page_num)
        except Exception:
            return

        try:
            self._pages.remove(page)  # type: ignore[arg-type]
        except Exception:
            # best-effort: rebuild list from notebook
            self._pages = [self.get_nth_page(i) for i in range(self.get_n_pages())]  # type: ignore[list-item]

        self._renumber_tabs()

        # Ensure focus
        cur = self.get_current_page()
        w = self.get_nth_page(cur)
        if isinstance(w, TerminalPage):
            w.grab_focus_terminal()

    def apply_settings_all(self) -> None:
        for p in self._pages:
            p.apply_settings()

    def open_preferences(self) -> None:
        top = self.get_toplevel()
        parent = top if isinstance(top, Gtk.Window) else None
        if parent is None:
            return

        dlg = TerminalPreferencesDialog(parent, self._store)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            dlg.save_to_store()
            self.apply_settings_all()
        dlg.destroy()

    def _on_switch_page(self, _nb, page, _page_num) -> None:
        if isinstance(page, TerminalPage):
            page.grab_focus_terminal()


# -------------------------
# Xed plugin (WindowActivatable)
# -------------------------


class EmbeddedTerminalPlugin(GObject.Object, Xed.WindowActivatable):
    window = GObject.Property(type=Xed.Window)

    def __init__(self):
        super().__init__()
        self._store = TerminalSettingsStore()
        self._notebook: Optional[TerminalNotebook] = None

    def do_activate(self):
        _debug("Activated.")

        self._notebook = TerminalNotebook(self._store)
        self._notebook.show()
        self._notebook.connect("populate-popup", self._on_notebook_populate_popup)

        bottom = None
        try:
            bottom = self.window.get_bottom_panel()
        except Exception:
            bottom = None

        if bottom is None:
            _debug("Bottom panel not found; plugin will not show terminal.")
            return

        # Xed's Panel API differs from gedit/pluma. Try common variants.
        # The goal is to add an item to the bottom panel so the built-in
        # View → Bottom Pane toggle works as expected.
        added = False

        # (widget, name, title) -- observed on some Xed versions
        try:
            bottom.add_item(self._notebook, "EmbeddedTerminalPanel", "Terminal")
            added = True
        except Exception as e:
            _debug(f"add_item(widget, name, title) failed: {e!r}")

        # (widget, name, title, icon)
        if not added:
            try:
                bottom.add_item(self._notebook, "EmbeddedTerminalPanel", "Terminal", None)
                added = True
            except Exception as e:
                _debug(f"add_item(widget, name, title, icon) failed: {e!r}")

        # Fallback: direct add
        if not added:
            try:
                bottom.add(self._notebook)
                added = True
            except Exception as e:
                _debug(f"add(widget) failed: {e!r}")

        if not added:
            _debug("Failed to add terminal to bottom panel (no supported API found).")

    def do_deactivate(self):
        _debug("Deactivating.")

        if self._notebook is None:
            return

        bottom = None
        try:
            bottom = self.window.get_bottom_panel()
        except Exception:
            bottom = None

        if bottom is not None:
            removed = False

            # Most consistent: remove_item(widget)
            try:
                bottom.remove_item(self._notebook)
                removed = True
                _debug("Removed via remove_item(widget).")
            except Exception as e:
                _debug(f"remove_item(widget) failed: {e!r}")

            if not removed:
                try:
                    bottom.remove(self._notebook)
                    removed = True
                    _debug("Removed via remove(widget).")
                except Exception as e:
                    _debug(f"remove(widget) failed: {e!r}")

        try:
            self._notebook.destroy()
        except Exception:
            pass

        self._notebook = None

    def do_update_state(self):
        return

    def _get_active_document_directory(self) -> Optional[str]:
        doc = None
        try:
            doc = self.window.get_active_document()
        except Exception:
            doc = None

        if doc is None:
            return None

        try:
            loc = doc.get_file().get_location()
        except Exception:
            loc = None

        if loc is None:
            return None

        try:
            if loc.has_uri_scheme("file"):
                parent = loc.get_parent()
                return parent.get_path() if parent is not None else None
        except Exception:
            pass

        return None

    def _on_notebook_populate_popup(self, _nb: TerminalNotebook, page: TerminalPage, menu: Gtk.Menu) -> None:
        menu.append(Gtk.SeparatorMenuItem())

        path = self._get_active_document_directory()
        item = Gtk.MenuItem.new_with_label("Change Directory to Active Document")
        item.set_sensitive(path is not None)
        item.connect("activate", lambda _mi: page.change_directory(path or ""))
        menu.append(item)

        menu.show_all()
