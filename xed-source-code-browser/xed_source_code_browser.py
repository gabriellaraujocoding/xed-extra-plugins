# -*- coding: utf-8 -*-
#
# Copyright (c) 2011 Micah Carrick
# Copyright (c) 2020-2021 MATE Developers
# Copyright (c) 2025 Gabriell Araujo (Xed port)
# SPDX-License-Identifier: BSD-3-Clause
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# * Neither the name of the copyright holder nor the names of its contributors
#   may be used to endorse or promote products derived from this software
#   without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
Source Code Browser plugin for Xed (Linux Mint).

Goals:
- Two-file plugin + icons folder.
- No GSettings schema; settings are stored in JSON under ~/.config/xed/.
- Adds a symbol tree to the side panel, driven by ctags.
- Jump to symbol on activation.
- Performance: only loads when the panel item is active (when supported) and uses debounce.

Debug:
- Set XED_DEBUG_SOURCE_CODE_BROWSER=1 to print debug logs.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Peas", "1.0")
gi.require_version("PeasGtk", "1.0")
gi.require_version("GdkPixbuf", "2.0")

# Xed GI namespace should be available on Linux Mint. We keep a clear error if it isn't.
gi.require_version("Xed", "1.0")

from gi.repository import GObject, GdkPixbuf, PeasGtk, Gio, Gtk, Gdk, GLib, Xed  # type: ignore


LOG = logging.getLogger("XedSourceCodeBrowser")
LOG_LEVEL = logging.WARN
LOG.setLevel(LOG_LEVEL)

PLUGIN_ID = "xed_source_code_browser"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "xed" / f"{PLUGIN_ID}.json"

DEFAULT_CONFIG = {
    "version": 1,
    "show_line_numbers": True,
    "load_remote_files": True,
    "expand_rows": True,
    "sort_list": True,
    "ctags_executable": "ctags",
    # Debounce in milliseconds for reload triggers (tab changes, state changes, etc.)
    "reload_debounce_ms": 200,
    "show_icons": True
}

def _env_truthy(name: str) -> bool:
    v = GLib.getenv(name)
    if v is None:
        return False
    v = v.strip().lower()
    return v not in ("", "0", "false", "no", "off")
    
_DEBUG = _env_truthy("XED_DEBUG_SOURCE_CODE_BROWSER")    

def _debug(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[xed-source-code-browser] {msg}\n")

# -----------------------------
# JSON config manager
# -----------------------------

class ConfigStore:
    def __init__(self, path: Path):
        self._path = path
        self._data = dict(DEFAULT_CONFIG)

    @property
    def data(self) -> dict:
        return self._data

    def load(self) -> dict:
        self._data = dict(DEFAULT_CONFIG)
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    self._data.update(obj)
        except Exception as e:
            LOG.warning("Failed to load config %s: %s", str(self._path), str(e))
        return self._data

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(self._path))
        except Exception as e:
            LOG.warning("Failed to save config %s: %s", str(self._path), str(e))


# -----------------------------
# Ctags wrapper + parser
# -----------------------------

def get_ctags_version(ctags_executable: str) -> Optional[str]:
    """
    Returns ctags --version output as a string, or None if ctags cannot be executed.
    """
    try:
        p = subprocess.run(
            [ctags_executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
        out = (p.stdout or "").strip()
        return out if out else None
    except Exception:
        return None


@dataclass
class Tag:
    name: str
    file: Optional[str] = None
    ex_command: Optional[str] = None
    kind: Optional["Kind"] = None
    fields: Dict[str, str] = None  # type: ignore


@dataclass
class Kind:
    name: str
    language: Optional[str] = None

    def group_name(self) -> str:
        # Simple pluralization heuristic.
        if self.name.endswith("s"):
            group = self.name + "es"
        elif self.name.endswith("y"):
            group = self.name[:-1] + "ies"
        else:
            group = self.name + "s"
        return group.capitalize()

    def icon_name(self) -> str:
        return "source-" + self.name


class CtagsParser:
    def __init__(self) -> None:
        self.tags: List[Tag] = []
        self.kinds: Dict[str, Kind] = {}

    def parse_file(self, ctags_executable: str, path: str) -> None:
        """
        Runs ctags for a local file path and parses tags into self.tags/self.kinds.
        """
        self.tags.clear()
        self.kinds.clear()

        # Original plugin used: ctags -nu --fields=fiKlmnsSzt -f - <file>
        # Use argv list to avoid shell quoting issues.
        argv = [
            ctags_executable,
            "-n",
            "-u",
            "--fields=fiKlmnsSzt",
            "-f",
            "-",
            path,
        ]

        try:
            p = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            raise RuntimeError(f"Could not execute ctags: {e}")

        if p.returncode != 0 and (p.stdout or "").strip() == "":
            # ctags sometimes uses return code even with output; only fail hard if empty output.
            err = (p.stderr or "").strip()
            raise RuntimeError(f"ctags failed (rc={p.returncode}): {err}")

        self._parse_text(p.stdout or "")

    def _parse_text(self, text: str) -> None:
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue

            tag = Tag(name=parts[0], fields={})
            tag.file = parts[1]
            tag.ex_command = parts[2]

            kind_obj: Optional[Kind] = None

            for field in parts[3:]:
                if ":" not in field:
                    continue
                key, value = field.split(":", 1)  # keep the full value even if it contains ':'
                tag.fields[key] = value
                if key == "kind":
                    kind_obj = self.kinds.get(value)
                    if kind_obj is None:
                        kind_obj = Kind(name=value)
                        self.kinds[value] = kind_obj

            if kind_obj is not None:
                if "language" in tag.fields:
                    kind_obj.language = tag.fields["language"]
                tag.kind = kind_obj

            self.tags.append(tag)


# -----------------------------
# UI: Symbol tree widget
# -----------------------------

class SourceTree(Gtk.Box):
    __gsignals__ = {
        "tag-activated": (GObject.SIGNAL_RUN_FIRST, GObject.TYPE_NONE, (GObject.TYPE_PYOBJECT,)),
    }

    def __init__(self, icon_path: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self._icon_path = icon_path
        self._pixbufs: Dict[str, GdkPixbuf.Pixbuf] = {}
        self._current_uri: Optional[str] = None
        self.expanded_rows: Dict[str, List[str]] = {}

        # Preferences (set by plugin)
        self.show_line_numbers = True
        self.ctags_executable = "ctags"
        self.expand_rows = True
        self.sort_list = True
        self.show_icons = True
        
        self._last_click_time = 0
        self._last_click_path = None


        self._store = Gtk.TreeStore(
            GdkPixbuf.Pixbuf,       # icon
            GObject.TYPE_STRING,    # name
            GObject.TYPE_STRING,    # kind
            GObject.TYPE_STRING,    # uri
            GObject.TYPE_STRING,    # line
            GObject.TYPE_STRING,    # markup
        )

        self._treeview = Gtk.TreeView.new_with_model(self._store)
        self._treeview.set_headers_visible(False)
        self._treeview.set_activate_on_single_click(True)

        column = Gtk.TreeViewColumn("Symbol")
        
        # old
        # cell_icon = Gtk.CellRendererPixbuf()
        # column.pack_start(cell_icon, False)
        # column.add_attribute(cell_icon, "pixbuf", 0)  
             
        # new 
        self._cell_icon = Gtk.CellRendererPixbuf()
        column.pack_start(self._cell_icon, False)
        column.add_attribute(self._cell_icon, "pixbuf", 0)        

        cell_text = Gtk.CellRendererText()
        column.pack_start(cell_text, True)
        column.add_attribute(cell_text, "markup", 5)

        self._treeview.append_column(column)
        self._treeview.connect("row-activated", self._on_row_activated)
        
        # Catch double-clicks to expand/collapse category rows.
        self._treeview.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self._treeview.connect("button-press-event", self._on_treeview_button_press)


        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self._treeview)

        self.pack_start(sw, True, True, 0)
        self.show_all()
        
    def set_icons_visible(self, visible: bool) -> None:
        """
        Toggle icon visibility in the tree without rebuilding the model.
        """
        self.show_icons = bool(visible)
        try:
            self._cell_icon.set_visible(self.show_icons)
        except Exception:
            pass

    def _pixbuf_missing(self) -> GdkPixbuf.Pixbuf:
        if "missing" not in self._pixbufs:
            filename = os.path.join(self._icon_path, "missing-image.png")
            self._pixbufs["missing"] = GdkPixbuf.Pixbuf.new_from_file(filename)
        return self._pixbufs["missing"]

    def get_pixbuf(self, icon_name: str) -> GdkPixbuf.Pixbuf:
        if icon_name not in self._pixbufs:
            filename = os.path.join(self._icon_path, icon_name + ".png")
            if os.path.exists(filename):
                try:
                    self._pixbufs[icon_name] = GdkPixbuf.Pixbuf.new_from_file(filename)
                except Exception as e:
                    LOG.warning("Could not load pixbuf '%s': %s", icon_name, str(e))
                    self._pixbufs[icon_name] = self._pixbuf_missing()
            else:
                self._pixbufs[icon_name] = self._pixbuf_missing()
        return self._pixbufs[icon_name]

    def clear(self) -> None:
        # Always preserve the user's current expand/collapse state for this URI.
        self._save_expanded_rows()
        self._store.clear()

    def _save_expanded_rows(self) -> None:
        if not self._current_uri:
            return
        self.expanded_rows[self._current_uri] = []
        self._treeview.map_expanded_rows(self._map_expanded_rows_cb, self._current_uri)

    def _map_expanded_rows_cb(self, _treeview: Gtk.TreeView, path: Gtk.TreePath, uri: str) -> None:
        self.expanded_rows.setdefault(uri, []).append(str(path))

    def _get_tag_iter(self, tag: Tag, parent_iter: Optional[Gtk.TreeIter]) -> Optional[Gtk.TreeIter]:
        it = self._store.iter_children(parent_iter)
        while it:
            if self._store.get_value(it, 1) == tag.name:
                return it
            it = self._store.iter_next(it)
        return None

    def _get_kind_iter(self, kind: Kind, uri: str, parent_iter: Optional[Gtk.TreeIter]) -> Gtk.TreeIter:
        it = self._store.iter_children(parent_iter)
        while it:
            if self._store.get_value(it, 2) == kind.name:
                return it
            it = self._store.iter_next(it)

        pixbuf = self.get_pixbuf(kind.icon_name())
        # markup = f"<i>{GLib.markup_escape_text(kind.group_name())}</i>"
        markup = f"{GLib.markup_escape_text(kind.group_name())}"
        return self._store.append(parent_iter, (pixbuf, kind.group_name(), kind.name, uri, None, markup))

    def load(self, kinds: Dict[str, Kind], tags: List[Tag], uri: str) -> None:
        self._current_uri = uri

        # Root-level tags first.
        for tag in tags:
            if not tag.kind:
                continue
            if "class" not in (tag.fields or {}):
                pixbuf = self.get_pixbuf(tag.kind.icon_name())
                line = (tag.fields or {}).get("line")
                if line and self.show_line_numbers:
                    markup = f"{GLib.markup_escape_text(tag.name)} [{line}]"
                else:
                    markup = GLib.markup_escape_text(tag.name)

                kind_iter = self._get_kind_iter(tag.kind, uri, None)
                self._store.append(kind_iter, (pixbuf, tag.name, tag.kind.name, uri, line, markup))

        # Second-level tags (simple nesting by "class" field).
        for tag in tags:
            if not tag.kind:
                continue
            cls = (tag.fields or {}).get("class")
            if cls and "." not in cls:
                pixbuf = self.get_pixbuf(tag.kind.icon_name())
                line = (tag.fields or {}).get("line")
                if line and self.show_line_numbers:
                    markup = f"{GLib.markup_escape_text(tag.name)} [{line}]"
                else:
                    markup = GLib.markup_escape_text(tag.name)

                parent_tag = next((pt for pt in tags if pt.name == cls), None)
                if not parent_tag or not parent_tag.kind:
                    continue

                parent_kind_iter = self._get_kind_iter(parent_tag.kind, uri, None)
                parent_iter = self._get_tag_iter(parent_tag, parent_kind_iter)
                kind_iter = self._get_kind_iter(tag.kind, uri, parent_iter)
                self._store.append(kind_iter, (pixbuf, tag.name, tag.kind.name, uri, line, markup))

        if self.sort_list:
            self._store.set_sort_column_id(1, Gtk.SortType.ASCENDING)

        # Expand rows.
        if uri in self.expanded_rows:
            for strpath in self.expanded_rows[uri]:
                path = Gtk.TreePath.new_from_string(strpath)
                if path:
                    self._treeview.expand_row(path, False)
        elif self.expand_rows:
            self._treeview.expand_all()

    def parse_file(self, path: str, uri: str) -> None:
        parser = CtagsParser()
        parser.parse_file(self.ctags_executable, path)
        self.load(parser.kinds, parser.tags, uri)

    def _on_row_activated(self, treeview: Gtk.TreeView, path: Gtk.TreePath, _column: Gtk.TreeViewColumn) -> None:
        model = treeview.get_model()
        it = model.get_iter(path)
        uri = model.get_value(it, 3)
        line = model.get_value(it, 4)
        if uri and line:
            self.emit("tag-activated", (uri, line))
            
    def _on_treeview_button_press(self, treeview: Gtk.TreeView, event: Gdk.Event) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 1:
            return False

        hit = treeview.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return False

        path, _col, _cell_x, _cell_y = hit
        model = treeview.get_model()
        it = model.get_iter(path)

        # Only toggle if is has children.
        if not model.iter_has_child(it):
            return False

        settings = Gtk.Settings.get_default()
        threshold = int(settings.get_property("gtk-double-click-time") or 250)  # ms

        # Detects manual "double click" on the SAME item.
        if self._last_click_path == str(path) and (event.time - self._last_click_time) <= threshold:
            # Toggle
            if treeview.row_expanded(path):
                treeview.collapse_row(path)
            else:
                treeview.expand_row(path, False)

            # Reset to allow the next pair of clicks to count as another double.
            self._last_click_time = 0
            self._last_click_path = None
            return True

        # First click: just register and let GTK select the line.
        self._last_click_time = event.time
        self._last_click_path = str(path)
        return False

# -----------------------------
# Config UI widget (no .ui file)
# -----------------------------

class ConfigureWidget(Gtk.Box):
    def __init__(self, store: ConfigStore, on_change_cb) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_border_width(12)
        self._store = store
        self._on_change_cb = on_change_cb

        data = store.data

        def mk_check(label: str, key: str) -> Gtk.CheckButton:
            btn = Gtk.CheckButton.new_with_mnemonic(label)
            btn.set_halign(Gtk.Align.START)
            btn.set_active(bool(data.get(key, False)))
            btn.connect("toggled", self._on_toggle, key)
            return btn

        self.pack_start(mk_check("Show _line numbers in tree", "show_line_numbers"), False, False, 0)
        self.pack_start(mk_check("Load symbols from _remote files", "load_remote_files"), False, False, 0)
        self.pack_start(mk_check("Start with rows _expanded", "expand_rows"), False, False, 0)
        self.pack_start(mk_check("_Sort list alphabetically", "sort_list"), False, False, 0)
        self.pack_start(mk_check("Show _icons in tree", "show_icons"), False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.pack_start(Gtk.Label(label="ctags executable"), False, False, 0)
        self._entry = Gtk.Entry()
        self._entry.set_text(str(data.get("ctags_executable", "ctags")))
        self._entry.connect("changed", self._on_entry_changed)
        row.pack_start(self._entry, True, True, 0)
        self.pack_start(row, False, False, 0)

        hint = Gtk.Label(
            label="Tip: set a full path if you use a custom ctags build.\n"
                  "Common: universal-ctags provides 'ctags' on most distros."
        )
        hint.set_halign(Gtk.Align.START)
        hint.set_xalign(0.0)
        hint.set_justify(Gtk.Justification.LEFT)
        hint.set_margin_top(6)
        self.pack_start(hint, False, False, 0)

        self.show_all()

    def _on_toggle(self, btn: Gtk.CheckButton, key: str) -> None:
        self._store.data[key] = bool(btn.get_active())
        self._store.save()
        self._on_change_cb(self._store.data)

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        self._store.data["ctags_executable"] = entry.get_text().strip() or "ctags"
        self._store.save()
        self._on_change_cb(self._store.data)


# -----------------------------
# Main plugin
# -----------------------------

class SourceCodeBrowserPlugin(GObject.Object, Xed.WindowActivatable, PeasGtk.Configurable):
    __gtype_name__ = "XedSourceCodeBrowserPlugin"

    window = GObject.Property(type=Xed.Window)

    def __init__(self) -> None:
        super().__init__()
        self._handlers: List[Tuple[object, int]] = []
        self._is_loaded = False
        self._ctags_version: Optional[str] = None
        self._reload_source_id: Optional[int] = None

        self._config = ConfigStore(CONFIG_FILE)
        self._config.load()

        self._sourcetree: Optional[SourceTree] = None
        self._loaded_document = None

    # PeasGtk.Configurable
    def do_create_configure_widget(self):
        self._config.load()
        return ConfigureWidget(self._config, self._on_config_changed)

    # Xed.WindowActivatable
    def do_activate(self) -> None:
        LOG.debug("Activating Source Code Browser")

        datadir, icon_dir = self._get_data_dirs()
        
        ctags_exe = self._config.data["ctags_executable"]
        _debug(f"ctags_executable={ctags_exe!r}")
    
        self._ctags_version = get_ctags_version(self._config.data["ctags_executable"])
        _debug(f"ctags_available={bool(self._ctags_version)}")

        self._sourcetree = SourceTree(icon_dir)
        self._apply_config_to_tree()

        # Side panel entry icon.
        icon_path = os.path.join(icon_dir, "source-code-browser.png")
        icon = Gtk.Image.new_from_file(icon_path) if os.path.exists(icon_path) else None

        panel = self.window.get_side_panel()

        # Xed side panel API (on some builds) expects the icon argument to be a *string*,
        # typically an icon-name from the current icon theme.
        # Use a stable themed icon name as default.
        icon_name_candidates = [
            "applications-development",
            "text-x-generic",
            "text-x-generic",
            "code-context",  # harmless if missing
        ]

        last_err = None
        for icon_name in icon_name_candidates:
            try:
                panel.add_item(self._sourcetree, "Source Code Browser", icon_name)
                last_err = None
                break
            except Exception as e:
                last_err = e
                continue
        if last_err is not None:
            raise last_err

        self._handlers.clear()
        self._handlers.append((self._sourcetree, self._sourcetree.connect("draw", self._on_sourcetree_draw)))

        if self._ctags_version:
            self._handlers.append((self._sourcetree, self._sourcetree.connect("tag-activated", self._on_tag_activated)))
            # Connect to window signals. Names are expected to match Pluma, but we keep try/except for robustness.
            self._try_connect(self.window, "active-tab-state-changed", self._on_tab_state_changed)
            self._try_connect(self.window, "active-tab-changed", self._on_active_tab_changed)
            self._try_connect(self.window, "tab-removed", self._on_tab_removed)
        else:
            LOG.warning("ctags not found/executable: %s", self._config.data["ctags_executable"])
            self._sourcetree.set_sensitive(False)

    def do_deactivate(self) -> None:
        LOG.debug("Deactivating Source Code Browser")

        if self._reload_source_id is not None:
            GLib.source_remove(self._reload_source_id)
            self._reload_source_id = None

        for obj, hid in self._handlers:
            try:
                obj.disconnect(hid)
            except Exception:
                pass
        self._handlers.clear()

        if self._sourcetree is not None:
            try:
                panel = self.window.get_side_panel()
                panel.remove_item(self._sourcetree)
            except Exception:
                pass

        self._sourcetree = None
        self._is_loaded = False
        self._loaded_document = None

    # -----------------------------
    # Helpers
    # -----------------------------

    def _get_data_dirs(self) -> Tuple[str, str]:
        """
        Returns (datadir, icon_dir).

        Robust to the user renaming the plugin folder: always derive paths from the
        directory that contains this .py file.
        """
        module_dir = os.path.dirname(os.path.abspath(__file__))

        # Keep datadir for compatibility (if you ever add ui/data), but don't rely on plugin_info.
        datadir = module_dir
        icon_dir = os.path.join(module_dir, "icons")
        return datadir, icon_dir


    def _apply_config_to_tree(self) -> None:
        if not self._sourcetree:
            return
        cfg = self._config.data
        self._sourcetree.ctags_executable = cfg.get("ctags_executable", "ctags")
        self._sourcetree.show_line_numbers = bool(cfg.get("show_line_numbers", True))
        self._sourcetree.expand_rows = bool(cfg.get("expand_rows", True))
        self._sourcetree.sort_list = bool(cfg.get("sort_list", True))
        self._sourcetree.set_icons_visible(bool(cfg.get("show_icons", True)))

    def _on_config_changed(self, cfg: dict) -> None:
        # Apply new config live.
        self._config.data.update(cfg)
        self._apply_config_to_tree()

        # Refresh ctags availability.
        self._ctags_version = get_ctags_version(self._config.data["ctags_executable"])
        if self._sourcetree:
            self._sourcetree.set_sensitive(bool(self._ctags_version))

        # Reload symbols with debounce.
        self._schedule_reload()

    def _try_connect(self, obj, signal_name: str, callback) -> None:
        try:
            hid = obj.connect(signal_name, callback)
            self._handlers.append((obj, hid))
        except Exception:
            LOG.debug("Signal not available: %s", signal_name)

    def _panel_item_is_active(self) -> bool:
        """
        Returns True if the side panel can tell that our item is active, otherwise True (fallback).
        """
        try:
            panel = self.window.get_side_panel()
            if hasattr(panel, "item_is_active"):
                return bool(panel.item_is_active(self._sourcetree))
        except Exception:
            pass
        return True

    def _schedule_reload(self) -> None:
        if not self._sourcetree or not self._ctags_version:
            return

        if self._reload_source_id is not None:
            GLib.source_remove(self._reload_source_id)
            self._reload_source_id = None

        delay = int(self._config.data.get("reload_debounce_ms", 200))
        _debug(f"schedule_reload delay={delay}ms")
        self._reload_source_id = GLib.timeout_add(delay, self._reload_cb)

    def _reload_cb(self) -> bool:
        self._reload_source_id = None
        _debug("reload fired")
        self._load_active_document_symbols()
        return False

    def _load_active_document_symbols(self) -> None:
        if not self._sourcetree:
            return        

        # Performance: only load when our panel item is active (if supported).
        if not self._panel_item_is_active():
            _debug("skip: panel item inactive")
            self._is_loaded = True
            return        

        document = self.window.get_active_document()
        if not document:
            self._is_loaded = True
            return

        location = document.get_location()
        if not location:
            self._is_loaded = True
            return

        uri = location.get_uri()
        if not uri:
            self._is_loaded = True
            return
            
        self._sourcetree.clear()
        self._is_loaded = False
            
        _debug(f"load symbols uri={uri!r}")

        try:
            if uri.startswith("file://"):
                _debug("source=local file://")
                filename = location.get_parse_name()  # UTF-8 safe path
                self._sourcetree.parse_file(filename, uri)
            else:
                allow_remote = bool(self._config.data.get("load_remote_files", True))
                if allow_remote:
                    _debug(f"source=remote (allow_remote={allow_remote})")
                    basename = location.get_basename() or "remote"
                    fd, tmpname = tempfile.mkstemp("." + basename)
                    try:
                        contents = document.get_text(document.get_start_iter(), document.get_end_iter(), True)
                        os.write(fd, contents.encode("utf-8", errors="replace"))
                    finally:
                        os.close(fd)

                    # Keep the UI responsive.
                    while Gtk.events_pending():
                        Gtk.main_iteration()

                    self._sourcetree.parse_file(tmpname, uri)
                    os.unlink(tmpname)
        except Exception as e:
            LOG.warning("Failed to load symbols for %s: %s", uri, str(e))

        self._loaded_document = document
        self._is_loaded = True

    # -----------------------------
    # Signals
    # -----------------------------

    def _on_sourcetree_draw(self, _sourcetree, _cr) -> bool:
        if not self._is_loaded:
            self._load_active_document_symbols()
        return False

    def _on_active_tab_changed(self, *_args) -> None:
        self._schedule_reload()

    def _on_tab_state_changed(self, *_args) -> None:
        self._schedule_reload()

    def _on_tab_removed(self, *_args) -> None:
        if self._sourcetree and not self.window.get_active_document():
            self._sourcetree.clear()

    def _on_tag_activated(self, _sourcetree: SourceTree, location: Tuple[str, str]) -> None:
        uri, line = location
        try:
            document = self.window.get_active_document()
            view = self.window.get_active_view()
            if not document or not view:
                return
            # ctags lines are 1-based; Xed document lines are 0-based.
            line0 = max(0, int(line) - 1)
            _debug(f"jump uri={uri!r} line={line} -> line0={line0}")
            document.goto_line(line0)
            view.scroll_to_cursor()
        except Exception as e:
            LOG.warning("Failed to jump to tag (%s:%s): %s", uri, line, str(e))
