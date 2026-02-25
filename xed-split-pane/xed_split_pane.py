# -*- coding: utf-8 -*-
#
# Copyright (c) 2025 Gabriell Araujo
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
Xed Split Pane plugin for Xed (Linux Mint).

A clean implementation for Xed (Linux Mint):
- Keep a pinned editor pane on the LEFT and the normal Xed tabs on the RIGHT.
- The LEFT pane stays pinned when switching tabs on the RIGHT.
- Minimal header in the LEFT pane:
  - Shows ONLY the pinned filename.
  - Hover: pointer cursor + subtle opacity change.
  - Click: opens a chooser listing open tabs to pin.
- The chooser lists currently open documents (full paths/URIs),
  with the pinned one in bold.
- Scrollbars are forced visible in BOTH panes (non-overlay, always).
- Font/zoom is mirrored between RIGHT active tab pane and LEFT pinned pane.
- Status bar: adds a clickable "Toggle Split Pane" text on status bar

Hotkey: Ctrl+Alt+P (best-effort) + View menu entry + clickable status bar text.

Debug:
- Set XED_DEBUG_SPLIT_PANE=1 to print debug logs.
"""

import gi, sys

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GObject, Gtk, Gio, GLib, Gdk

try:
    gi.require_version("Xed", "1.0")
    from gi.repository import Xed
except Exception:  # pragma: no cover
    Xed = None

def _env_truthy(name: str) -> bool:
    v = GLib.getenv(name)
    if v is None:
        return False
    v = v.strip().lower()
    return v not in ("", "0", "false", "no", "off")
    
_DEBUG = _env_truthy("XED_DEBUG_SPLIT_PANE")    

def _debug(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[xed-split-pane] {msg}\n")

def _document_full_path(doc) -> str:
    """Best-effort full path (or URI) for a Xed/Gedit-like document/buffer."""
    if doc is None:
        return "*No document*"

    try:
        f = doc.get_file()
        loc = f.get_location() if f is not None else None
        if loc is not None:
            p = loc.get_path()
            if p:
                return p
            return loc.get_uri()
    except Exception:
        pass

    try:
        uri = doc.get_uri()
        if uri:
            return uri
    except Exception:
        pass

    return "*Unsaved*"


def _document_filename(doc) -> str:
    """Best-effort basename for UI."""
    full = _document_full_path(doc)
    if not full or full.startswith("*"):
        return full

    s = full.rstrip("/")
    # Handle URIs and normal paths the same way.
    if "://" in s:
        return s.split("/")[-1]
    return s.split("/")[-1]


class XedSplitPaneWindowActivatable(GObject.Object, Xed.WindowActivatable):
    window = GObject.Property(type=Xed.Window)

    def __init__(self):
        super().__init__()

        # Split state
        self._active = False
        self._paned = None

        self._left_box = None
        self._left_header_eventbox = None
        self._left_header_label = None
        self._left_sw = None
        self._left_view = None

        self._right_notebook = None
        self._right_parent = None
        self._right_pack_info = None

        self._pinned_doc = None

        # Signals
        self._nb_switch_sid = None

        # Right->Left sync signals
        self._font_sids = []            # list of (obj, sid)
        self._font_sync_src_view = None # which RIGHT view we are currently mirroring
        self._font_sync_dst_view = None  # which LEFT view we are currently mirroring into

        # Wrap state (treated as global while split is active)
        self._global_wrap_mode = None
        self._wrap_syncing = False

        # Menu integration (UIManager)
        self._ui_manager = None
        self._ui_id = None
        self._action_group = None

        # Fallback action (GAction)
        self._gaction = None

        # Cursor handling
        self._pointer_cursor = None
        self._default_cursor = None

        # Statusbar toggle ("Toggle Split Pane")
        self._status_toggle_parent = None
        self._status_toggle_eventbox = None
        self._status_toggle_label = None
        self._statusbar_retry_id = None
        self._statusbar_try_count = 0

    # ---------------- Lifecycle ----------------

    def do_activate(self):
        _debug("activate")
        self._install_menu_item()
        self._install_gaction_fallback()
        
        # Install a clickable statusbar text (best-effort; statusbar may appear later).
        self._install_statusbar_toggle()

    def do_deactivate(self):
        _debug("deactivate")
        try:
            if self._active:
                self._unsplit()
        except Exception:
            pass

        self._remove_menu_item()
        self._remove_gaction_fallback()
        self._remove_statusbar_toggle()

    # ---------------- Actions / menu ----------------

    def _install_gaction_fallback(self):
        # This does not replace the UIManager entry; it's just a robust fallback.
        try:
            act = Gio.SimpleAction.new("toggle_split_pane", None)
            act.connect("activate", self._on_toggle_split)
            self.window.add_action(act)
            self._gaction = act

            try:
                app = self.window.get_application()
                app.set_accels_for_action("win.toggle_split_pane", ["<Ctrl><Alt>P"])
            except Exception:
                pass
        except Exception:
            self._gaction = None

    def _remove_gaction_fallback(self):
        if self._gaction is None:
            return
        try:
            self.window.remove_action("toggle_split_pane")
        except Exception:
            pass
        self._gaction = None

    def _install_menu_item(self):
        try:
            uim = self.window.get_ui_manager()
        except Exception:
            return

        self._ui_manager = uim
        ag = Gtk.ActionGroup("XedSplitPaneActions")

        act = Gtk.Action(
            "XedSplitPaneToggle",
            "Toggle Split Pane",
            "Toggle split pane (pinned left pane)",
            None,
        )
        act.connect("activate", lambda *_: self._on_toggle_split(None, None))
        ag.add_action_with_accel(act, "<Ctrl><Alt>P")

        try:
            uim.insert_action_group(ag, -1)
        except Exception:
            self._ui_manager = None
            return

        ui_xml = """<ui>
        <menubar name=\"MenuBar\">
        <menu name=\"ViewMenu\" action=\"View\">
        <placeholder name=\"ViewOps_2\">
        <menuitem name=\"XedSplitPaneToggle\" action=\"XedSplitPaneToggle\"/>
        </placeholder>
        </menu>
        </menubar>
        </ui>"""
        
        try:
            self._ui_id = uim.add_ui_from_string(ui_xml)
            uim.ensure_update()
            self._action_group = ag
            _debug("menu item installed")
        except Exception:
            self._ui_id = None
            self._action_group = None

    def _remove_menu_item(self):
        if self._ui_manager is None:
            return

        try:
            if self._ui_id is not None:
                self._ui_manager.remove_ui(self._ui_id)
                self._ui_manager.ensure_update()
        except Exception:
            pass

        try:
            if self._action_group is not None:
                self._ui_manager.remove_action_group(self._action_group)
                self._ui_manager.ensure_update()
        except Exception:
            pass

        self._ui_manager = None
        self._ui_id = None
        self._action_group = None

    # ---------------- Toggle split ----------------

    def _on_toggle_split(self, action, param):
        if self._active:
            self._unsplit()
        else:
            self._split()

    def _split(self):
        _debug("split: begin")

        nb = self._find_tabs_notebook()
        if nb is None:
            _debug("split: notebook not found")
            return

        parent = nb.get_parent()
        if parent is None:
            _debug("split: notebook has no parent")
            return

        # Cache 'right side' info so we can restore it exactly.
        self._right_notebook = nb
        self._right_parent = parent
        self._right_pack_info = self._capture_pack_info(parent, nb)

        if not self._remove_child(parent, nb):
            _debug("split: failed to detach notebook")
            self._clear_state()
            return

        # Build left side from current active tab.
        tab = self.window.get_active_tab()
        doc = tab.get_document() if tab is not None else None
        right_view = tab.get_view() if tab is not None else None
        self._pinned_doc = doc

        self._left_view = self._new_view_for_document(doc, like=right_view)

        self._left_sw = Gtk.ScrolledWindow()
        self._force_scrollbars(self._left_sw)
        self._left_sw.add(self._left_view)

        self._left_header_eventbox, self._left_header_label = self._build_left_header()
        self._update_left_header_label()

        self._left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._left_box.pack_start(self._left_header_eventbox, False, False, 0)
        self._left_box.pack_start(self._left_sw, True, True, 0)

        self._paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self._paned.add1(self._left_box)
        self._paned.add2(nb)

        if not self._add_child(parent, self._paned, self._right_pack_info):
            _debug("split: failed to insert paned (restoring notebook)")
            self._add_child(parent, nb, self._right_pack_info)
            self._clear_state()
            return

        self._active = True

        self._left_box.show_all()
        nb.show()
        self._paned.show()

        GLib.idle_add(self._set_paned_half, self._paned)

        self._install_notebook_signals()

        # IMPORTANT:
        # - LEFT mirrors zoom from ACTIVE RIGHT tab
        # - wrap-mode treated as global while split is active
        # - we call sync on idle and timeout to ensure sync between panes
        self._install_font_sync()
        GLib.idle_add(self._late_font_sync)
        GLib.timeout_add(200, self._late_font_sync)

        _debug("split: enabled")

    def _unsplit(self):
        _debug("unsplit: begin")

        self._remove_font_sync()
        self._remove_notebook_signals()

        parent = self._right_parent
        nb = self._right_notebook
        paned = self._paned

        if parent is not None and paned is not None:
            try:
                self._remove_child(parent, paned)
            except Exception:
                pass

        if paned is not None and nb is not None:
            try:
                paned.remove(nb)
            except Exception:
                pass

        if parent is not None and nb is not None:
            self._add_child(parent, nb, self._right_pack_info)

        self._clear_state()
        _debug("unsplit: done")

    def _clear_state(self):
        self._active = False

        self._paned = None
        self._left_box = None
        self._left_header_eventbox = None
        self._left_header_label = None
        self._left_sw = None
        self._left_view = None

        self._right_notebook = None
        self._right_parent = None
        self._right_pack_info = None

        self._pinned_doc = None

        self._pointer_cursor = None
        self._default_cursor = None

        self._font_sync_src_view = None
        self._font_sync_dst_view = None
        self._global_wrap_mode = None
        self._wrap_syncing = False  

    # ---------------- Left header ----------------

    def _build_left_header(self):
        eventbox = Gtk.EventBox()
        eventbox.set_visible_window(False)
        eventbox.set_above_child(False)

        label = Gtk.Label()
        label.set_xalign(0.0)

        # Optional ellipsize
        try:
            gi.require_version("Pango", "1.0")
            from gi.repository import Pango
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        except Exception:
            pass

        try:
            label.set_margin_start(6)
            label.set_margin_end(6)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
        except Exception:
            pass

        eventbox.add(label)

        # Hover + click behavior
        eventbox.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        eventbox.connect("enter-notify-event", self._on_header_enter)
        eventbox.connect("leave-notify-event", self._on_header_leave)
        eventbox.connect("button-press-event", self._on_header_click)

        return eventbox, label

    def _on_header_enter(self, widget, event):
        if self._left_header_label is not None:
            try:
                self._left_header_label.set_opacity(0.78)
            except Exception:
                pass

        win = widget.get_window()
        if win is None:
            return False

        display = win.get_display()
        if self._pointer_cursor is None:
            try:
                self._pointer_cursor = Gdk.Cursor.new_from_name(display, "pointer")
            except Exception:
                self._pointer_cursor = None

        if self._default_cursor is None:
            try:
                self._default_cursor = win.get_cursor()
            except Exception:
                self._default_cursor = None

        try:
            if self._pointer_cursor is not None:
                win.set_cursor(self._pointer_cursor)
        except Exception:
            pass

        return False

    def _on_header_leave(self, widget, event):
        if self._left_header_label is not None:
            try:
                self._left_header_label.set_opacity(1.0)
            except Exception:
                pass

        win = widget.get_window()
        if win is None:
            return False

        try:
            win.set_cursor(self._default_cursor)
        except Exception:
            pass

        return False

    def _on_header_click(self, widget, event):
        # Left click only.
        try:
            if event.button != 1:
                return False
        except Exception:
            pass

        self._open_chooser(widget)
        return True

    def _update_left_header_label(self):
        if self._left_header_label is None:
            return
        try:
            self._left_header_label.set_text(_document_filename(self._pinned_doc))
        except Exception:
            pass

    # ---------------- Chooser ----------------

    def _open_chooser(self, anchor_widget):
        docs = self._get_open_documents()
        _debug(f"chooser: {len(docs)} docs")

        menu = Gtk.Menu()

        if not docs:
            item = Gtk.MenuItem.new_with_label("(No open tabs)")
            item.set_sensitive(False)
            menu.append(item)
        else:
            for doc in docs:
                path = _document_full_path(doc)
                safe = GLib.markup_escape_text(path)

                prefix = "\u00A0\u00A0"  # NBSP NBSP: visual padding
                if doc is self._pinned_doc:
                    markup = f"{prefix}<b>{safe}</b>"
                else:
                    markup = f"{prefix}{safe}"

                item = Gtk.MenuItem.new_with_label("")
                child = item.get_child()
                try:
                    child.set_use_markup(True)
                    child.set_label(markup)
                except Exception:
                    item.set_label(prefix + path)

                item.connect("activate", self._on_choose_doc, doc)
                menu.append(item)

        menu.show_all()

        try:
            menu.popup_at_widget(
                anchor_widget,
                Gdk.Gravity.SOUTH_WEST,
                Gdk.Gravity.NORTH_WEST,
                None,
            )
        except Exception:
            try:
                menu.popup(None, None, None, None, 0, Gtk.get_current_event_time())
            except Exception:
                pass

    def _on_choose_doc(self, menuitem, doc):
        if not self._active or doc is None:
            return
        _debug(f"pin: {_document_full_path(doc)}")
        self._pin_document_left(doc)

    def _pin_document_left(self, doc):
        if self._left_sw is None:
            return

        like = self._get_active_right_view()
        new_view = self._new_view_for_document(doc, like=like)

        old_view = self._left_view

        # Remove old pinned view safely
        try:
            if old_view is not None:                
                self._left_sw.remove(old_view)
        except Exception:
            pass

        # Swap state
        self._left_view = new_view
        self._pinned_doc = doc

        # Add new pinned view
        try:
            self._left_sw.add(new_view)
            self._left_sw.show_all()
        except Exception:
            pass

        self._update_left_header_label()

        # Refresh sync against the current right pane
        self._install_font_sync()

    # ---------------- Notebook / tabs ----------------

    def _get_open_documents(self):
        # Prefer the Xed API if available.
        try:
            tabs = list(self.window.get_tabs())
        except Exception:
            tabs = []

        docs = []
        if tabs:
            for t in tabs:
                try:
                    d = t.get_document()
                    if d is not None:
                        docs.append(d)
                except Exception:
                    pass
            return docs

        # Fallback: enumerate notebook pages.
        nb = self._right_notebook or self._find_tabs_notebook()
        if nb is None:
            return []

        try:
            n = nb.get_n_pages()
        except Exception:
            return []

        for i in range(n):
            try:
                page = nb.get_nth_page(i)
            except Exception:
                continue
            if hasattr(page, "get_document"):
                try:
                    d = page.get_document()
                    if d is not None:
                        docs.append(d)
                except Exception:
                    pass

        return docs

    def _install_notebook_signals(self):
        nb = self._right_notebook
        if nb is None:
            return
        try:
            self._nb_switch_sid = nb.connect("switch-page", self._on_switch_page)
        except Exception:
            self._nb_switch_sid = None

    def _remove_notebook_signals(self):
        nb = self._right_notebook
        if nb is None:
            return
        if self._nb_switch_sid is not None:
            try:
                nb.disconnect(self._nb_switch_sid)
            except Exception:
                pass
        self._nb_switch_sid = None

    def _on_switch_page(self, notebook, page, page_num):
        _debug(f"switch-page: {page_num}")
        # On tab change: LEFT must mirror the newly active RIGHT zoom;
        # wrap-mode remains global while split is active.
        self._install_font_sync()


    # ---------------- Font sync ----------------

    def _get_active_right_view(self):
        tab = self.window.get_active_tab()
        if tab is None:
            return None
        try:
            return tab.get_view()
        except Exception:
            return None

    def _install_font_sync(self):
        """
        Rules (while split is active):
        - Each RIGHT tab keeps its own zoom (font-scale).
        - LEFT pinned view ALWAYS mirrors the zoom of the ACTIVE RIGHT tab.
        - wrap-mode is treated as global: a change in LEFT or ACTIVE RIGHT is applied
          to LEFT + all RIGHT views.
        """
        lv = self._left_view
        rv = self._get_active_right_view()
        if lv is None or rv is None:
            return

        # Initialize global wrap from the active RIGHT view the first time.
        if self._global_wrap_mode is None:
            self._capture_wrap_from_view(rv)

        # If the active RIGHT view changed, rewire signals to the new source view.
        if rv is not self._font_sync_src_view or lv is not self._font_sync_dst_view:
            self._remove_font_sync()
            self._font_sync_src_view = rv
            self._font_sync_dst_view = lv

            # RIGHT -> LEFT (zoom + font)
            for prop in ("font-desc", "font-scale"):
                try:
                    rv.get_property(prop)
                    sid = rv.connect(f"notify::{prop}", self._on_right_view_changed, rv, lv)
                    self._font_sids.append((rv, sid))
                except Exception:
                    pass

            try:
                sid = rv.connect("style-updated", self._on_right_view_style_updated, rv, lv)
                self._font_sids.append((rv, sid))
            except Exception:
                pass

            # Any wrap change in ACTIVE RIGHT becomes global
            try:
                rv.get_property("wrap-mode")
                sid = rv.connect("notify::wrap-mode", self._on_any_wrap_mode_changed)
                self._font_sids.append((rv, sid))
            except Exception:
                pass

            # Any wrap change in LEFT becomes global
            try:
                lv.get_property("wrap-mode")
                sid = lv.connect("notify::wrap-mode", self._on_any_wrap_mode_changed)
                self._font_sids.append((lv, sid))
            except Exception:
                pass

        # Always refresh LEFT from current active RIGHT (especially on tab switch).
        self._apply_font_like(rv, lv)

        # Enforce global wrap everywhere.
        self._apply_wrap_everywhere()

    def _remove_font_sync(self):
        for obj, sid in self._font_sids:
            try:
                obj.disconnect(sid)
            except Exception:
                pass
        self._font_sids = []
        self._font_sync_src_view = None
        self._font_sync_dst_view = None

    def _apply_font_like(self, src, dst):
        # Mirror font + zoom only (wrap handled separately as global).
        for prop in ("font-desc", "font-scale"):
            try:
                dst.set_property(prop, src.get_property(prop))
            except Exception:
                pass

        # Best-effort font override (safe to ignore failures)
        try:
            ctx = src.get_style_context()
            font_desc = ctx.get_font(Gtk.StateFlags.NORMAL)
            dst.override_font(font_desc)
        except Exception:
            pass
    
    def _iter_right_views(self):
        """Yield all tab views from the window (right pane tabs)."""
        try:
            tabs = list(self.window.get_tabs())
        except Exception:
            tabs = []

        for t in tabs:
            try:
                v = t.get_view()
            except Exception:
                v = None
            if v is not None:
                yield v

    def _capture_wrap_from_view(self, view):
        if view is None:
            return
        try:
            self._global_wrap_mode = view.get_property("wrap-mode")
        except Exception:
            self._global_wrap_mode = Gtk.WrapMode.NONE

    def _apply_wrap_everywhere(self):
        """Apply the current global wrap-mode to LEFT + all RIGHT views."""
        if self._global_wrap_mode is None:
            return

        if self._wrap_syncing:
            return

        self._wrap_syncing = True
        try:
            for v in self._iter_right_views():
                try:
                    v.set_property("wrap-mode", self._global_wrap_mode)
                except Exception:
                    pass

            if self._left_view is not None:
                try:
                    self._left_view.set_property("wrap-mode", self._global_wrap_mode)
                except Exception:
                    pass
        finally:
            self._wrap_syncing = False

    def _on_right_view_changed(self, widget, pspec, src, dst):
        # src is ACTIVE RIGHT view, dst is LEFT view
        self._apply_font_like(src, dst)


    def _on_right_view_style_updated(self, widget, src, dst):
        self._apply_font_like(src, dst)


    def _on_any_wrap_mode_changed(self, view, pspec):
        if self._wrap_syncing:
            return
        try:
            self._global_wrap_mode = view.get_property("wrap-mode")
        except Exception:
            return
        self._apply_wrap_everywhere()

    # ---------------- Widget discovery ----------------

    def _find_tabs_notebook(self):
        notebooks = self._find_widgets(self.window, Gtk.Notebook)
        for nb in notebooks:
            try:
                n = nb.get_n_pages()
            except Exception:
                continue

            for i in range(n):
                try:
                    page = nb.get_nth_page(i)
                except Exception:
                    continue

                if hasattr(page, "get_view") and hasattr(page, "get_document"):
                    return nb

        return notebooks[0] if notebooks else None

    def _find_widgets(self, root, cls):
        found = []

        def rec(w):
            try:
                if isinstance(w, cls):
                    found.append(w)
            except Exception:
                pass

            if isinstance(w, Gtk.Container):
                try:
                    for child in w.get_children():
                        rec(child)
                except Exception:
                    pass

        rec(root)
        return found

    # ---------------- Pane creation ----------------

    def _new_view_for_document(self, doc, like=None):
        view2 = None

        if doc is not None and Xed is not None and hasattr(Xed.View, "new_with_buffer"):
            try:
                view2 = Xed.View.new_with_buffer(doc)
            except Exception:
                view2 = None

        if view2 is None:
            try:
                view2 = Xed.View()
                if doc is not None:
                    view2.set_buffer(doc)
            except Exception:
                view2 = Gtk.TextView.new_with_buffer(doc) if doc is not None else Gtk.TextView()

        # It mirrors properties
        if like is not None and view2 is not None:
            for prop in (
                "show-line-numbers",
                "highlight-current-line",
                "tab-width",
                "indent-width",
                "insert-spaces-instead-of-tabs",
                "auto-indent",
                "show-right-margin",
                "right-margin-position",
                "wrap-mode",
                "font-desc",
                "font-scale",
            ):
                try:
                    view2.set_property(prop, like.get_property(prop))
                except Exception:
                    pass

        return view2

    # ---------------- Scrollbars ----------------

    def _force_scrollbars(self, scrolled_window: Gtk.ScrolledWindow):
        try:
            scrolled_window.set_overlay_scrolling(False)
        except Exception:
            pass

        try:
            scrolled_window.set_policy(Gtk.PolicyType.ALWAYS, Gtk.PolicyType.ALWAYS)
        except Exception:
            try:
                scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            except Exception:
                pass

    # ---------------- Packing helpers ----------------

    def _capture_pack_info(self, parent, child):
        info = {"type": type(parent).__name__}

        if isinstance(parent, Gtk.Box):
            try:
                children = parent.get_children()
                info["index"] = children.index(child)
            except Exception:
                info["index"] = -1

            try:
                expand, fill, padding, pack_type = parent.query_child_packing(child)
                info["expand"] = bool(expand)
                info["fill"] = bool(fill)
                info["padding"] = int(padding)
                info["pack_type"] = pack_type
            except Exception:
                info["expand"] = True
                info["fill"] = True
                info["padding"] = 0
                info["pack_type"] = Gtk.PackType.START

        return info

    def _remove_child(self, parent, child) -> bool:
        try:
            parent.remove(child)
            return True
        except Exception:
            return False

    def _add_child(self, parent, child, info) -> bool:
        try:
            if isinstance(parent, Gtk.Box) and isinstance(info, dict):
                expand = info.get("expand", True)
                fill = info.get("fill", True)
                padding = info.get("padding", 0)
                pack_type = info.get("pack_type", Gtk.PackType.START)

                if pack_type == Gtk.PackType.END:
                    parent.pack_end(child, expand, fill, padding)
                else:
                    parent.pack_start(child, expand, fill, padding)

                idx = info.get("index", -1)
                if isinstance(idx, int) and idx >= 0:
                    try:
                        parent.reorder_child(child, idx)
                    except Exception:
                        pass
                return True

            if hasattr(parent, "add"):
                parent.add(child)
                return True
        except Exception:
            return False

        return False

    # ---------------- Layout helpers ----------------

    def _set_paned_half(self, paned):
        try:
            alloc = paned.get_allocation()
            if alloc.width > 0:
                paned.set_position(alloc.width // 2)
        except Exception:
            pass
        return False
        
    def _late_font_sync(self):
        # Run after the UI settles; fixes occasional missing zoom mirroring.
        if self._active:
            self._install_font_sync()
        return False
        
    # ---------------- Statusbar: clickable "Toggle Split Pane" ----------------

    def _install_statusbar_toggle(self):
        """Best-effort install. If the statusbar isn't ready yet, retry for a short period."""
        if self._status_toggle_eventbox is not None:
            return

        # Avoid multiple timers.
        if self._statusbar_retry_id is not None:
            return

        # Try now, otherwise retry a few times.
        if self._try_install_statusbar_toggle_once():
            return

        self._statusbar_try_count = 0
        self._statusbar_retry_id = GLib.timeout_add(250, self._retry_install_statusbar_toggle)

    def _retry_install_statusbar_toggle(self):
        if self._status_toggle_eventbox is not None:
            self._statusbar_retry_id = None
            return False

        self._statusbar_try_count += 1
        # ~10 seconds max (250ms * 40)
        if self._statusbar_try_count > 40:
            _debug("statusbar: container not found (giving up)")
            self._statusbar_retry_id = None
            return False

        ok = self._try_install_statusbar_toggle_once()
        if ok:
            self._statusbar_retry_id = None
            return False

        return True

    def _try_install_statusbar_toggle_once(self) -> bool:
        # Prefer the RIGHT status item box (Python/Spaces/Ln/Col/INS area).
        right_box, root_box = self._find_statusbar_right_box_and_root()
        if right_box is None and root_box is None:
            return False

        eventbox, label = self._build_statusbar_toggle_widget()

        try:
            if right_box is not None:
                # Put BEFORE the first right item (index 0).
                right_box.pack_start(eventbox, False, False, 6)
                try:
                    right_box.reorder_child(eventbox, 0)
                except Exception:
                    pass
                self._status_toggle_parent = right_box
            else:
                # Fallback: append as the LAST item (right-most) in the root statusbar box.
                root_box.pack_end(eventbox, False, False, 6)
                self._status_toggle_parent = root_box

            # Show only our widget (do not force-show the whole statusbar container).
            eventbox.show_all()

            self._status_toggle_eventbox = eventbox
            self._status_toggle_label = label
            _debug("statusbar: toggle widget installed (right side)")
            return True
        except Exception:
            return False

    def _remove_statusbar_toggle(self):
        # Stop retry timer if running
        if self._statusbar_retry_id is not None:
            try:
                GLib.source_remove(self._statusbar_retry_id)
            except Exception:
                pass
            self._statusbar_retry_id = None

        # Remove widget if installed
        w = self._status_toggle_eventbox
        if w is not None:
            try:
                parent = w.get_parent()
                if parent is not None and hasattr(parent, "remove"):
                    parent.remove(w)
            except Exception:
                pass

        self._status_toggle_parent = None
        self._status_toggle_eventbox = None
        self._status_toggle_label = None
        self._statusbar_try_count = 0

    def _build_statusbar_toggle_widget(self):
        eventbox = Gtk.EventBox()
        eventbox.set_visible_window(False)
        eventbox.set_above_child(False)

        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_text("Toggle Split Pane")

        try:
            label.set_margin_start(6)
            label.set_margin_end(6)
            label.set_margin_top(2)
            label.set_margin_bottom(2)
        except Exception:
            pass

        eventbox.add(label)

        # Hover + click behavior
        eventbox.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        eventbox.connect("enter-notify-event", self._on_status_toggle_enter)
        eventbox.connect("leave-notify-event", self._on_status_toggle_leave)
        eventbox.connect("button-press-event", self._on_status_toggle_click)

        return eventbox, label

    def _on_status_toggle_enter(self, widget, event):
        if self._status_toggle_label is not None:
            try:
                self._status_toggle_label.set_opacity(0.78)
            except Exception:
                pass

        win = widget.get_window()
        if win is None:
            return False

        display = win.get_display()
        if self._pointer_cursor is None:
            try:
                self._pointer_cursor = Gdk.Cursor.new_from_name(display, "pointer")
            except Exception:
                self._pointer_cursor = None

        try:
            if self._pointer_cursor is not None:
                win.set_cursor(self._pointer_cursor)
        except Exception:
            pass

        return False

    def _on_status_toggle_leave(self, widget, event):
        if self._status_toggle_label is not None:
            try:
                self._status_toggle_label.set_opacity(1.0)
            except Exception:
                pass

        win = widget.get_window()
        if win is None:
            return False

        try:
            win.set_cursor(None)
        except Exception:
            pass

        return False

    def _on_status_toggle_click(self, widget, event):
        # Left click only.
        try:
            if event.button != 1:
                return False
        except Exception:
            pass

        # Use the exact same toggle path as the View menu item.
        self._on_toggle_split(None, None)
        return True

    def _find_statusbar_right_box_and_root(self):
        """
        Returns (right_box, root_box).
        - root_box: a horizontal Gtk.Box that contains a Gtk.Statusbar (message area).
        - right_box: the child of root_box packed with PackType.END (usually the indicators area).
        """
        root = self._find_statusbar_root_box()
        if root is None:
            return (None, None)

        # Find the right-side container packed at END inside the root.
        try:
            for ch in root.get_children():
                try:
                    _expand, _fill, _padding, pack_type = root.query_child_packing(ch)
                except Exception:
                    continue
                if pack_type == Gtk.PackType.END and isinstance(ch, Gtk.Box):
                    return (ch, root)
        except Exception:
            pass

        # If no END child box exists, fallback to root only.
        return (None, root)

    def _find_statusbar_root_box(self):
        """
        Locate the main statusbar container box.
        Heuristic: a horizontal Gtk.Box that contains a Gtk.Statusbar descendant.
        Prefer boxes whose name/style classes include 'statusbar'.
        """
        best = None
        best_score = -1

        for box in self._find_widgets(self.window, Gtk.Box):
            try:
                if box.get_orientation() != Gtk.Orientation.HORIZONTAL:
                    continue
            except Exception:
                continue

            # Must contain a Gtk.Statusbar somewhere inside.
            if not self._find_widgets(box, Gtk.Statusbar):
                continue

            score = 0
            try:
                name = (box.get_name() or "").lower()
            except Exception:
                name = ""
            try:
                classes = box.get_style_context().list_classes()
                classes = [c.lower() for c in classes if isinstance(c, str)]
            except Exception:
                classes = []

            if "statusbar" in name:
                score += 5
            if any("statusbar" in c for c in classes):
                score += 5

            # Prefer roots that have children packed at END (typical statusbar layout).
            try:
                for ch in box.get_children():
                    try:
                        _e, _f, _p, pt = box.query_child_packing(ch)
                    except Exception:
                        continue
                    if pt == Gtk.PackType.END:
                        score += 2
                        break
            except Exception:
                pass

            if score > best_score:
                best_score = score
                best = box

        return best
