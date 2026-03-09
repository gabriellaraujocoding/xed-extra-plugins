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
Xed Indentation Guides plugin for Xed (Linux Mint).

VS Code-like indentation guides:
- Draw vertical indentation guides inside the editor text area.
- Guides are based ONLY on leading whitespace and the current tab width (tab stops),
  not on language syntax (no braces/colons parsing).
- Works for any language (C/C++, Python, Bash, ...).
- Adapts to any tab size configured in Xed.
- Guides are drawn at the beginning of each indent block (VS Code-like):
  level 1 -> column 0, level 2 -> column tabw, level 3 -> column 2*tabw.

Performance:
- Scan only a window around the visible lines (visible range + limited back/forward scan).
- Avoid whole-buffer copies (reads only the window text).
- Coalesce edits and scrolling with an idle recalculation.

Debug:
- Set XED_DEBUG_INDENTATION_GUIDES=1 to print debug logs.
"""


from gi.repository import GLib, GObject, Gtk, Gdk

try:
    from gi.repository import Xed as Gedit  # Linux Mint Xed
except Exception:
    try:
        from gi.repository import Pluma as Gedit  # MATE Pluma
    except Exception:
        from gi.repository import Gedit  # type: ignore

import sys

_DEBUG = (GLib.getenv("XED_DEBUG_INDENTATION_GUIDES") is not None)

def _debug(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[xed-indentation-guides] {msg}\n")

class _ScopeTextOverlay:
    """
    Draw VS Code-like indent guides inside the text area.

    Guides are based ONLY on:
    - the view tab width (tab stops)
    - per-line leading whitespace columns (tabs expanded)
    - a per-line indent level (in tab units), computed from leading whitespace

    Notes:
    - Guides are drawn at the BEGINNING of each tab block:
        level 1 -> column 0
        level 2 -> column tabw
        level 3 -> column 2*tabw
        level 4 -> column 3*tabw
    """

    TEXT_ALPHA = 0.18  # subtle like VS Code

    def __init__(self, view: Gtk.TextView):
        self._view = view
        self._enabled = False

        self._rgba = Gdk.RGBA()
        self._rgba.parse("#888888")
        self._rgba.alpha = self.TEXT_ALPHA

        self._win_first = 0
        self._win_last = -1

        # Maps by global line number
        self._indent_level = {}  # line -> int (indent level in tab units)
        self._ws_cols = {}       # line -> int (leading whitespace columns; HUGE for blank lines)

        # Cached metrics
        self._metrics_valid = False
        self._char_w = 8
        self._tab_spaces = 4
        self._th = 1  # thickness in pixels

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._view.queue_draw()

    def set_color(self, rgba: Gdk.RGBA) -> None:
        tmp = Gdk.RGBA()
        tmp.red = rgba.red
        tmp.green = rgba.green
        tmp.blue = rgba.blue
        tmp.alpha = self.TEXT_ALPHA
        self._rgba = tmp
        self._metrics_valid = False
        self._view.queue_draw()

    def set_window_map(self, win_first: int, win_last: int, indent_level, ws_cols) -> None:
        self._win_first = int(win_first)
        self._win_last = int(win_last)
        self._indent_level = indent_level or {}
        self._ws_cols = ws_cols or {}
        self._view.queue_draw()

    def clear(self) -> None:
        self.set_window_map(0, -1, {}, {})

    def invalidate_metrics(self) -> None:
        self._metrics_valid = False

    def _ensure_metrics(self) -> None:
        if self._metrics_valid:
            return

        tabw = 4
        try:
            if hasattr(self._view, "get_tab_width"):
                tabw = int(self._view.get_tab_width())
        except Exception:
            tabw = 4

        try:
            layout = self._view.create_pango_layout(" ")
            w, _h = layout.get_pixel_size()
            if w > 0:
                self._char_w = int(w)
        except Exception:
            self._char_w = 8

        self._tab_spaces = max(1, int(tabw))
        self._metrics_valid = True

    def _visible_line_range(self):
        buf = self._view.get_buffer()

        top = 0
        bot = 0
        try:
            it = buf.get_iter_at_mark(buf.get_insert())
            top = bot = int(it.get_line())
        except Exception:
            pass

        try:
            rect = self._view.get_visible_rect()
            if rect is None:
                return top, bot
        except Exception:
            return top, bot

        try:
            r0 = self._view.get_line_at_y(int(rect.y) + 1)
            r1 = self._view.get_line_at_y(int(rect.y) + max(1, int(rect.height) - 1))
            it0 = r0[0] if isinstance(r0, tuple) else r0
            it1 = r1[0] if isinstance(r1, tuple) else r1
            top = int(it0.get_line())
            bot = int(it1.get_line())
            if bot < top:
                bot = top
        except Exception:
            pass

        return top, bot

    def _fill_rect(self, cr, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        Gdk.cairo_set_source_rgba(cr, self._rgba)
        cr.rectangle(x, y, w, h)
        cr.fill()

    def on_draw(self, view, cr):
        if not self._enabled:
            return False

        if self._win_last < self._win_first:
            return False

        buf = view.get_buffer()

        vis_top, vis_bot = self._visible_line_range()
        first = max(self._win_first, vis_top)
        last = min(self._win_last, vis_bot)
        if last < first:
            return False

        self._ensure_metrics()

        cr.save()
        try:
            import cairo
            cr.set_antialias(cairo.ANTIALIAS_NONE)
        except Exception:
            pass

        # Base X in buffer coords (start of line).
        base_x_buf = 0
        try:
            it0 = buf.get_iter_at_line(first)
            r0 = view.get_iter_location(it0)
            base_x_buf = int(r0.x)
        except Exception:
            base_x_buf = 0

        try:
            base_x_w, _ = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, base_x_buf, 0)
            base_x_w = int(base_x_w)
        except Exception:
            base_x_w = int(base_x_buf)

        MAX_LEVELS = 64

        for line in range(first, last + 1):
            lvl = int(self._indent_level.get(line, 0))
            if lvl <= 0:
                continue

            lvl = max(0, min(lvl, MAX_LEVELS))

            ws_cols = int(self._ws_cols.get(line, 0))
            if ws_cols <= 0:
                continue

            try:
                it = buf.get_iter_at_line(line)
                y_buf, h = view.get_line_yrange(it)
                y_buf = int(y_buf)
                h = int(h)
            except Exception:
                continue

            if h <= 0:
                continue

            try:
                _xw, y0 = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, 0, y_buf)
                _xw, y1 = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, 0, y_buf + h)
                y0 = int(y0)
                y1 = int(y1)
            except Exception:
                continue

            if y1 <= y0:
                continue

            # Draw per-line segment with small gaps.
            pad = 2
            seg_y0 = y0 + pad
            seg_y1 = y1 - pad
            if seg_y1 <= seg_y0:
                continue

            # BEGINNING of each tab block (VS Code-like, as requested):
            # level 1 -> col 0, level 2 -> col tabw, ...
            for d in range(1, lvl + 1):
                col = (d - 1) * self._tab_spaces
                if col >= ws_cols:
                    continue

                xw = int(base_x_w + float(col) * float(self._char_w))
                self._fill_rect(cr, xw, seg_y0, self._th, seg_y1 - seg_y0)

        cr.restore()
        return False

class XedScopeGuidesViewActivatable(GObject.Object, Gedit.ViewActivatable):
    view = GObject.Property(type=Gedit.View)

    # Window size knobs
    BACKSCAN_LINES = 1200
    FORWARD_LINES = 300

    # Rendering options
    DRAW_TEXT_GUIDES = True      # VS Code-like guides inside the editor area

    def __init__(self):
        super().__init__()

        self._overlay = None
        self._draw_layer_sid = 0

        self._buffer = None
        self._view_signals = []
        self._buffer_signals = []
        self._scroll_sid = 0

        self._recalc_source_id = 0

        self._enabled = False

    # ------------------------- Lifecycle -------------------------

    def do_activate(self):
        # Text overlay (indent guides)
        if self.DRAW_TEXT_GUIDES:
            self._overlay = _ScopeTextOverlay(self.view)
            try:
                self._draw_layer_sid = self.view.connect_after("draw", self._overlay.on_draw)
            except Exception:
                self._draw_layer_sid = 0
        else:
            self._overlay = None
            self._draw_layer_sid = 0

        self._view_signals = [
            self.view.connect("notify::buffer", self._on_notify_buffer),
        ]

        # React to font/style changes (metrics depend on font).
        try:
            self._view_signals.append(self.view.connect("style-updated", self._on_view_style_updated))
        except Exception:
            pass

        # React to tab width changes (metrics depend on tab stops).
        try:
            self._view_signals.append(self.view.connect("notify::tab-width", self._on_view_tab_width_changed))
        except Exception:
            pass

        self._attach_scroll_listener()

        self._buffer = None
        self._on_notify_buffer(self.view)

    def do_deactivate(self):
        self._cancel_recalc()
        self._detach_scroll_listener()
        self._disconnect_all()

        if self._draw_layer_sid:
            try:
                self.view.disconnect(self._draw_layer_sid)
            except Exception:
                pass
            self._draw_layer_sid = 0

        self._overlay = None
        self._buffer = None

    # ------------------------- Signals -------------------------

    def _disconnect_all(self):
        for sid in self._view_signals:
            if not sid:
                continue
            try:
                self.view.disconnect(sid)
            except Exception:
                pass
        self._view_signals = []

        if self._buffer is not None:
            for sid in self._buffer_signals:
                if not sid:
                    continue
                try:
                    self._buffer.disconnect(sid)
                except Exception:
                    pass
        self._buffer_signals = []

    def _attach_scroll_listener(self):
        try:
            adj = self.view.get_vadjustment()
            if adj is not None:
                self._scroll_sid = adj.connect("value-changed", self._on_scroll_changed)
        except Exception:
            self._scroll_sid = 0

    def _detach_scroll_listener(self):
        if self._scroll_sid:
            try:
                adj = self.view.get_vadjustment()
                if adj is not None:
                    adj.disconnect(self._scroll_sid)
            except Exception:
                pass
        self._scroll_sid = 0

    def _on_scroll_changed(self, *args):
        self._schedule_recalc()

    def _on_view_style_updated(self, *args):
        if self._overlay is not None:
            self._overlay.invalidate_metrics()
        self._schedule_recalc()

    def _on_view_tab_width_changed(self, *args):
        if self._overlay is not None:
            self._overlay.invalidate_metrics()
        self._schedule_recalc()

    def _on_notify_buffer(self, view, pspec=None):
        self._cancel_recalc()

        if self._buffer is not None:
            for sid in self._buffer_signals:
                if not sid:
                    continue
                try:
                    self._buffer.disconnect(sid)
                except Exception:
                    pass
            self._buffer_signals = []

        self._buffer = view.get_buffer()

        if self._buffer is None:
            self._set_enabled(False)
            return

        self._buffer_signals = [
            self._buffer.connect("changed", self._on_buffer_changed),
            self._buffer.connect("notify::style-scheme", self._on_style_scheme_changed),
        ]

        self._schedule_recalc()

    def _on_buffer_changed(self, *args):
        self._schedule_recalc()

    def _on_style_scheme_changed(self, *args):
        self._schedule_recalc()

    # ------------------------- Enable/disable + colors -------------------------

    def _theme_color(self) -> Gdk.RGBA:
        fallback = Gdk.RGBA()
        fallback.parse("#888888")
        fallback.alpha = 0.25

        alpha = 0.25

        try:
            scheme = self._buffer.get_style_scheme() if self._buffer is not None else None
            if scheme is not None:
                for style_id in ("text", "def:text", "line-numbers"):
                    style = scheme.get_style(style_id)
                    if style is None:
                        continue
                    fg = style.get_property("foreground")
                    if not fg:
                        continue
                    tmp = Gdk.RGBA()
                    if tmp.parse(fg):
                        tmp.alpha = alpha
                        return tmp
        except Exception:
            pass

        try:
            ctx = self.view.get_style_context()
            c = ctx.get_color(Gtk.StateFlags.NORMAL)
            tmp = Gdk.RGBA(red=c.red, green=c.green, blue=c.blue, alpha=alpha)
            return tmp
        except Exception:
            return fallback

    def _set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if self._overlay is not None:
            self._overlay.set_enabled(enabled)

    # ------------------------- Scheduling -------------------------

    def _cancel_recalc(self):
        if self._recalc_source_id:
            try:
                GLib.source_remove(self._recalc_source_id)
            except Exception:
                pass
            self._recalc_source_id = 0

    def _schedule_recalc(self):
        if self._recalc_source_id:
            return
        self._recalc_source_id = GLib.idle_add(self._recalc_now)

    # ------------------------- Visible window helpers -------------------------

    def _get_iter_at_location(self, x: int, y: int):
        try:
            r = self.view.get_iter_at_location(x, y)
            if isinstance(r, tuple) and len(r) >= 1:
                return r[0]
            return r
        except Exception:
            try:
                r = self.view.get_iter_at_position(x, y)
                if isinstance(r, tuple) and len(r) >= 1:
                    return r[0]
                return r
            except Exception:
                return None

    def _visible_line_range(self):
        fallback_top = 0
        fallback_bot = 0
        try:
            if self._buffer is not None:
                it = self._buffer.get_iter_at_mark(self._buffer.get_insert())
                cur = int(it.get_line())
                fallback_top = cur
                fallback_bot = cur
        except Exception:
            pass

        try:
            rect = self.view.get_visible_rect()
        except Exception:
            return fallback_top, fallback_bot

        if rect is None:
            return fallback_top, fallback_bot

        def _iter_of(r):
            if isinstance(r, tuple) and len(r) >= 1:
                return r[0]
            return r

        try:
            r0 = self.view.get_line_at_y(int(rect.y) + 1)
            r1 = self.view.get_line_at_y(int(rect.y) + max(1, int(rect.height) - 1))
            it_top = _iter_of(r0)
            it_bot = _iter_of(r1)
            top = int(it_top.get_line())
            bot = int(it_bot.get_line())
            if bot < top:
                bot = top
            return top, bot
        except Exception:
            pass

        it_top = self._get_iter_at_location(int(rect.x) + 1, int(rect.y) + 1)
        it_bot = self._get_iter_at_location(int(rect.x) + 1, int(rect.y) + max(1, int(rect.height) - 1))
        if it_top is not None and it_bot is not None:
            try:
                top = int(it_top.get_line())
                bot = int(it_bot.get_line())
                if bot < top:
                    bot = top
                return top, bot
            except Exception:
                pass

        return fallback_top, fallback_bot

    # ------------------------- Indent-only window parser -------------------------

    def _parse_indent_window(self, text: str, win_first: int, win_last: int, tabw: int):
        """
        Compute indent guides maps from leading whitespace only.

        Returns:
        indent_level_map: global_line -> indent level (in tab units)
        ws_cols_map:      global_line -> leading whitespace columns (tabs expanded);
                          blank lines receive a HUGE value so guides can be inherited.
        """

        win_n = text.count("\n") + 1
        expected = (win_last - win_first + 1)
        if win_n != expected:
            win_n = expected

        raw_lines = text.splitlines()
        if len(raw_lines) < win_n:
            raw_lines += [""] * (win_n - len(raw_lines))
        else:
            raw_lines = raw_lines[:win_n]

        tabw = max(1, int(tabw))
        HUGE = 10 ** 9

        ws_cols_local = [0] * win_n
        is_blank = [False] * win_n
        level_raw = [0] * win_n

        for li in range(win_n):
            sline = raw_lines[li] or ""
            stripped = sline.strip()
            blank = (stripped == "")
            is_blank[li] = blank

            # Leading whitespace columns (tabs expanded)
            col = 0
            j = 0
            while j < len(sline):
                ch = sline[j]
                if ch == " ":
                    col += 1
                elif ch == "\t":
                    col += (tabw - (col % tabw))
                else:
                    break
                j += 1

            ws_cols_local[li] = (HUGE if blank else col)
            level_raw[li] = (0 if blank else (col // tabw))

        # Effective levels: blank lines inherit previous non-blank level.
        level_eff = [0] * win_n
        last_level = 0
        for li in range(win_n):
            if not is_blank[li]:
                last_level = level_raw[li]
                level_eff[li] = last_level
            else:
                level_eff[li] = last_level       
       
        indent_level_map = {}
        ws_cols_map = {}

        for li in range(win_n):
            gl = win_first + li
            lvl = int(level_eff[li])

            indent_level_map[gl] = lvl
            ws_cols_map[gl] = int(ws_cols_local[li])

        return indent_level_map, ws_cols_map

    # ------------------------- Recalc -------------------------

    def _recalc_now(self):
        self._recalc_source_id = 0

        enable = (self._buffer is not None)
        self._set_enabled(enable)

        if self._buffer is None:
            return GLib.SOURCE_REMOVE

        rgba = self._theme_color()
        if self._overlay is not None:
            self._overlay.set_color(rgba)
            self._overlay.invalidate_metrics()

        if not enable:
            if _DEBUG:
                _debug("disabled -> empty window map")
            if self._overlay is not None:
                self._overlay.clear()
            return GLib.SOURCE_REMOVE

        top, bot = self._visible_line_range()

        # Always include cursor line in the window (robustness).
        try:
            it = self._buffer.get_iter_at_mark(self._buffer.get_insert())
            cur = int(it.get_line())
            top = min(top, cur)
            bot = max(bot, cur)
        except Exception:
            pass

        try:
            n_lines = int(self._buffer.get_line_count())
        except Exception:
            n_lines = max(bot + 1, 1)

        win_first = max(0, top - self.BACKSCAN_LINES)
        win_last = min(n_lines - 1, bot + self.FORWARD_LINES)

        try:
            it0 = self._buffer.get_iter_at_line(win_first)
            if win_last + 1 < n_lines:
                it1 = self._buffer.get_iter_at_line(win_last + 1)
            else:
                it1 = self._buffer.get_end_iter()
            text = self._buffer.get_text(it0, it1, True)
        except Exception as e:
            if _DEBUG:
                _debug(f"failed to read window text: {e}")
            return GLib.SOURCE_REMOVE

        tabw = 4
        try:
            if hasattr(self.view, "get_tab_width"):
                tabw = int(self.view.get_tab_width())
            else:
                tabw = int(self.view.get_property("tab-width"))
        except Exception:
            tabw = 4

        try:
            indent_level_map, ws_cols_map = (
                self._parse_indent_window(text, win_first, win_last, tabw)
            )
        except Exception as e:
            if _DEBUG:
                _debug(f"parse error: {e}")
            if self._overlay is not None:
                self._overlay.clear()
            return GLib.SOURCE_REMOVE

        if _DEBUG:
            _debug(
                f"window {win_first}-{win_last} parsed: "
                f"indent_lines={sum(1 for v in indent_level_map.values() if v > 0)}"
            )

        if self._overlay is not None:
            self._overlay.set_window_map(win_first, win_last, indent_level_map, ws_cols_map)

        return GLib.SOURCE_REMOVE

