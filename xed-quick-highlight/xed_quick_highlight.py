# -*- coding: utf-8 -*-
#
# Copyright (c) 2018 Martin Blanchard
# Copyright (c) 2025 Gabriell Araujo (Xed port)
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""
Quick Highlight plugin for Xed (Linux Mint).

Behavior:
- Highlights all occurrences of the currently selected text.
- Highlighting is enabled only when the selection is non-empty and on a single line.
- Updates automatically when selection/caret moves or text is edited.
- Styling is taken from the current GtkSourceView style scheme when available.

Debug:
- Set XED_DEBUG_QUICK_HIGHLIGHT=1 to print debug logs.
"""

from __future__ import annotations

import gi, sys

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk, GLib

# GtkSource can be 3.0 or 4 depending on the distro/editor stack.
GtkSource = None
for _ver in ("3.0", "4"):
    try:
        gi.require_version("GtkSource", _ver)
        from gi.repository import GtkSource as _GtkSource  # type: ignore
        GtkSource = _GtkSource
        break
    except Exception:
        GtkSource = None

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

_DEBUG = _env_truthy("XED_DEBUG_QUICK_HIGHLIGHT")

def _debug(msg: str) -> None:
    if _DEBUG:
        print(f"[xed-quick-highlight] {msg}")

class QuickHighlightViewActivatable(GObject.Object, Xed.ViewActivatable):
    """Per-view plugin that highlights occurrences of selected text."""

    view = GObject.Property(type=Xed.View)

    def __init__(self):
        super().__init__()

        self._buffer = None
        self._insert_mark = None

        self._search_context = None
        self._search_settings = None
        self._match_style = None

        self._view_notify_sid = 0
        self._mark_set_sid = 0
        self._delete_range_sid = 0
        self._style_scheme_sid = 0

        self._queued_highlight_id = 0

    # ---------- Lifecycle ----------

    def do_activate(self):
        try:
            self._view_notify_sid = self.view.connect("notify::buffer", self._on_notify_buffer)
        except Exception:
            self._view_notify_sid = 0

        self._set_buffer(self._get_view_buffer())

    def do_deactivate(self):
        self._cancel_queued_highlight()
        self._clear_highlight()

        self._disconnect_buffer()

        if self._view_notify_sid:
            try:
                self.view.disconnect(self._view_notify_sid)
            except Exception:
                pass
        self._view_notify_sid = 0

        self._buffer = None
        self._insert_mark = None

    # ---------- Buffer wiring ----------

    def _get_view_buffer(self):
        try:
            return self.view.get_buffer()
        except Exception:
            return None

    def _on_notify_buffer(self, *args):
        self._set_buffer(self._get_view_buffer())

    def _disconnect_buffer(self):
        buf = self._buffer
        if buf is None:
            return

        for sid_attr in ("_mark_set_sid", "_delete_range_sid", "_style_scheme_sid"):
            sid = getattr(self, sid_attr)
            if sid:
                try:
                    buf.disconnect(sid)
                except Exception:
                    pass
            setattr(self, sid_attr, 0)

        self._buffer = None
        self._insert_mark = None

    def _set_buffer(self, buffer_obj):
        if buffer_obj is None:
            self._disconnect_buffer()
            self._clear_highlight()
            return

        if self._buffer is buffer_obj:
            return

        self._disconnect_buffer()
        self._clear_highlight()

        self._buffer = buffer_obj
        try:
            self._insert_mark = self._buffer.get_insert()
        except Exception:
            self._insert_mark = None

        try:
            self._mark_set_sid = self._buffer.connect("mark-set", self._on_mark_set)
        except Exception:
            self._mark_set_sid = 0

        try:
            self._delete_range_sid = self._buffer.connect("delete-range", self._on_delete_range)
        except Exception:
            self._delete_range_sid = 0

        # Style scheme changes are on GtkSourceBuffer.
        if GtkSource is not None:
            try:
                self._style_scheme_sid = self._buffer.connect("notify::style-scheme", self._on_notify_style_scheme)
            except Exception:
                self._style_scheme_sid = 0

        self._load_match_style()
        self._queue_update()

    # ---------- Signals ----------

    def _on_mark_set(self, buffer_obj, location, mark):
        if self._insert_mark is not None and mark is not self._insert_mark:
            return
        self._queue_update()

    def _on_delete_range(self, *args):
        self._queue_update()

    def _on_notify_style_scheme(self, *args):
        self._load_match_style()
        if self._search_context is not None:
            self._apply_style_to_context()

    # ---------- Highlight logic ----------

    def _cancel_queued_highlight(self):
        if self._queued_highlight_id:
            try:
                GLib.source_remove(self._queued_highlight_id)
            except Exception:
                pass
        self._queued_highlight_id = 0

    def _queue_update(self):
        if self._queued_highlight_id:
            return

        try:
            self._queued_highlight_id = GLib.idle_add(self._highlight_worker, priority=GLib.PRIORITY_LOW)
        except Exception:
            try:
                self._queued_highlight_id = GLib.idle_add(self._highlight_worker)
            except Exception:
                self._queued_highlight_id = 0

    def _get_selection_single_line(self):
        buf = self._buffer
        if buf is None:
            return None

        try:
            if hasattr(buf, "get_has_selection") and not buf.get_has_selection():
                return None
        except Exception:
            pass

        try:
            res = buf.get_selection_bounds()
        except Exception:
            return None

        if isinstance(res[0], bool):
            has_sel, start, end = res
            if not has_sel:
                return None
        else:
            start, end = res[0], res[1]

        try:
            if start.equal(end):
                return None
        except Exception:
            pass

        try:
            if start.get_line() != end.get_line():
                return None
        except Exception:
            return None

        try:
            text = buf.get_text(start, end, True)
        except Exception:
            try:
                text = start.get_text(end)
            except Exception:
                return None

        if not text:
            return None

        return text

    def _highlight_worker(self):
        self._queued_highlight_id = 0

        text = self._get_selection_single_line()
        if text is None:
            self._clear_highlight()
            return False
            
        _debug(f"Highlight selection: {text!r}")

        self._ensure_search_context()

        if self._search_settings is None or self._search_context is None:
            return False

        try:
            self._search_settings.set_search_text(text)
        except Exception:
            try:
                self._search_settings.set_property("search-text", text)
            except Exception:
                pass

        try:
            self._search_context.set_highlight(True)
        except Exception:
            try:
                self._search_context.set_property("highlight", True)
            except Exception:
                pass

        return False

    def _clear_highlight(self):
        if self._search_context is not None:
            try:
                self._search_context.set_highlight(False)
            except Exception:
                try:
                    self._search_context.set_property("highlight", False)
                except Exception:
                    pass

        self._search_context = None
        self._search_settings = None

    def _ensure_search_context(self):
        if GtkSource is None:
            return
        if self._buffer is None:
            return
        if self._search_context is not None and self._search_settings is not None:
            return

        try:
            settings = GtkSource.SearchSettings()
        except Exception:
            try:
                settings = GObject.new(GtkSource.SearchSettings)
            except Exception:
                return

        try:
            settings.set_at_word_boundaries(False)
        except Exception:
            pass
        try:
            settings.set_case_sensitive(True)
        except Exception:
            pass
        try:
            settings.set_regex_enabled(False)
        except Exception:
            pass

        ctx = None
        try:
            ctx = GtkSource.SearchContext.new(self._buffer, settings)
        except Exception:
            try:
                ctx = GObject.new(GtkSource.SearchContext, buffer=self._buffer, settings=settings, highlight=False)
            except Exception:
                ctx = None

        self._search_settings = settings
        self._search_context = ctx

        if self._search_context is not None:
            self._apply_style_to_context()
            try:
                self._search_context.set_highlight(False)
            except Exception:
                pass

    def _load_match_style(self):
        self._match_style = None
        if GtkSource is None or self._buffer is None:
            return

        scheme = None
        try:
            scheme = self._buffer.get_style_scheme()
        except Exception:
            scheme = None

        if scheme is None:
            return

        for style_id in ("quick-highlight-match", "search-match"):
            try:
                st = scheme.get_style(style_id)
            except Exception:
                st = None
            if st is not None:
                self._match_style = st
                break

    def _apply_style_to_context(self):
        if self._search_context is None:
            return
        if self._match_style is None:
            return

        try:
            self._search_context.set_match_style(self._match_style)
            return
        except Exception:
            pass

        try:
            self._search_context.set_property("match-style", self._match_style)
        except Exception:
            pass
