# -*- coding: utf-8 -*-
#
# Copyright (c) 2026 Gabriell Araujo
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
Xed Smart Overview (Linux Mint)

Purpose:
- Provide a VS Code-like minimap/overview experience in Xed by making GtkSourceMap
  behave as a predictable visual scrollbar.

How it matches VS Code:
- Slider height uses the same principle as VS Code when minimap height == editor height:
  sliderHeight ~= viewportHeight^2 / logicalScrollHeight, generalized to minimap height.
- Dragging the slider uses the same mapping as VS Code:
  desiredScrollTop = initialScrollTop + pointerDelta / computedSliderRatio
  where computedSliderRatio = maxSliderTop / maxScrollTop.

Behavior:
- Left-click INSIDE the visible overlay (viewport highlight): does NOT jump; it arms drag only.
- Drag (motion with button pressed): scrolls smoothly and predictably (VS Code-like).
- Left-click OUTSIDE the overlay: jumps to the clicked position (unless disabled).
- Right-click / other inputs: untouched.

Debug:
  XED_DEBUG_SMART_OVERVIEW=1 xed

Preferences:
- Uses PeasGtk.Configurable when available.
- Stores settings in JSON under:
  ~/.config/xed/xed-smart-overview.json
"""

from __future__ import annotations

import json
import os
import sys
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk, Gdk, GLib

gi.require_version("Xed", "1.0")
from gi.repository import Xed

# -------------------- Global variables --------------------

# Global drag speed multiplier (1.0 = baseline).
_DRAG_SPEED_MULT = 1.25

# Preferably provide a Preferences UI (PeasGtk.Configurable), but keep plugin loadable if missing.
_HAVE_PEASGTK = False
try:
    gi.require_version("PeasGtk", "1.0")
    from gi.repository import PeasGtk  # type: ignore
    _HAVE_PEASGTK = True
except Exception:
    PeasGtk = None  # type: ignore

# -------------------- Debug (xed-quick-highlight style) --------------------

def _env_truthy(name: str) -> bool:
    v = GLib.getenv(name)
    if v is None:
        return False
    v = v.strip().lower()
    return v not in ("", "0", "false", "no", "off")

_DEBUG = _env_truthy("XED_DEBUG_SMART_OVERVIEW")

def _debug(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[xed-smart-overview] {msg}\n")

# -------------------- JSON settings --------------------

_DEFAULTS = {
    # Debug overlay: draw the thumb (scrubber) hitbox we use for input.
    "draw_scrubber_area": False,

    # If true, clicking outside the scrubber will NOT jump (it will be ignored).
    "disable_click_outside": True,
}

def _config_path() -> str:
    cfg_dir = os.path.join(GLib.get_user_config_dir(), "xed")
    new_path = os.path.join(cfg_dir, "xed-smart-overview.json")
    old_path = os.path.join(cfg_dir, "smart-overview.json")
    # Backward compat: if the old file exists and the new one doesn't, keep using the old one.
    if os.path.exists(old_path) and (not os.path.exists(new_path)):
        return old_path
    return new_path

class ConfigStore:
    def __init__(self, path: str):
        self._path = path
        self.data = dict(_DEFAULTS)
        self.load()

    def load(self) -> dict:
        self.data = dict(_DEFAULTS)
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    # Backward-compat: older keys.
                    if "enable_click_outside_jump" in obj and "disable_click_outside" not in obj:
                        try:
                            obj["disable_click_outside"] = (not bool(obj["enable_click_outside_jump"]))
                        except Exception:
                            pass

                    for k in _DEFAULTS:
                        if k in obj:
                            self.data[k] = obj[k]
        except Exception as e:
            _debug(f"config load failed: {e}")
        return self.data

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, self._path)
        except Exception as e:
            _debug(f"config save failed: {e}")


_CONFIG = ConfigStore(_config_path())

# -------------------- Preferences UI --------------------

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

        self.pack_start(mk_check("Draw _scrubber area (debug)", "draw_scrubber_area"), False, False, 0)
        self.pack_start(mk_check("_Disable click outside scrubber (no jump)", "disable_click_outside"), False, False, 0)

        self.show_all()

    def _on_toggle(self, btn: Gtk.CheckButton, key: str) -> None:
        self._store.data[key] = bool(btn.get_active())
        self._store.save()
        self._on_change_cb(self._store.data)

# -------------------- Helpers --------------------

_DRAG_THRESHOLD_PX = 2.0
_MIN_THUMB_PX = 10.0

# The orthogonal distance to the slider at which dragging "resets" (VS Code uses 140 on Windows).
_POINTER_DRAG_RESET_DISTANCE = 140.0

def _gtype_name(widget) -> str:
    try:
        return GObject.type_name(widget.__gtype__)
    except Exception:
        return ""


def _find_scrolled_window_ancestor(widget):
    w = widget.get_parent()
    while w is not None and not isinstance(w, Gtk.ScrolledWindow):
        w = w.get_parent()
    return w if isinstance(w, Gtk.ScrolledWindow) else None

# -------------------- Core hook --------------------

class _MapHook:
    def __init__(self, src_map: Gtk.Widget, view: Gtk.TextView):
        self.map = src_map
        self.view = view

        scrolled = _find_scrolled_window_ancestor(view)
        self._vadj = scrolled.get_vadjustment() if scrolled else None

        # Drag state (VS Code-like)
        self._dragging = False
        self._drag_started = False
        self._press_y = 0.0
        self._initial_pos_y = 0.0
        self._initial_scroll_top = 0.0
        self._computed_slider_ratio = 0.0
        self._max_scroll_top = 0.0
        self._viewport_height = 0.0
        self._scroll_height = 0.0

        # Track geometry
        self._track_y0 = 0.0
        self._track_h = 1.0

        # Thumb geometry (for debug drawing)
        self._thumb_top = 0.0
        self._thumb_h = 0.0
        self._travel = 1.0

        try:
            self.map.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.BUTTON_RELEASE_MASK
                | Gdk.EventMask.POINTER_MOTION_MASK
            )
        except Exception:
            pass

        self._press_sid = self.map.connect("button-press-event", self._on_button_press)
        self._motion_sid = self.map.connect("motion-notify-event", self._on_motion)
        self._release_sid = self.map.connect("button-release-event", self._on_button_release)
        self._draw_sid = self.map.connect_after("draw", self._on_draw)

        _debug("hook attached to GtkSourceMap")

    def disconnect(self):
        self._end_drag()

        for sid in (self._press_sid, self._motion_sid, self._release_sid, self._draw_sid):
            try:
                if sid and sid > 0 and GObject.signal_handler_is_connected(self.map, sid):
                    self.map.disconnect(sid)
            except Exception:
                pass

        _debug("hook detached")

    # --- Capture width ---

    def _capture_width(self) -> float:
        alloc = self.map.get_allocation()
        return float(alloc.width if alloc.width > 0 else 1.0)

    # --- Track geometry ---

    def _update_track_geometry(self) -> None:
        # Use TEXT window to avoid theme/padding offsets.
        alloc = self.map.get_allocation()
        track_y0 = 0.0
        track_h = float(alloc.height if alloc.height > 0 else 1.0)
        try:
            win = self.map.get_window(Gtk.TextWindowType.TEXT)
            if win is not None:
                geo = win.get_geometry()
                if isinstance(geo, (tuple, list)) and len(geo) >= 4:
                    track_y0 = float(geo[1])
                    track_h = float(geo[3] if geo[3] > 0 else track_h)
        except Exception:
            pass
        self._track_y0 = track_y0
        self._track_h = max(1.0, track_h)

    # --- VS Code minimap math ---

    def _get_editor_line_metrics(self) -> tuple[int, float, float, float]:
        """
        Returns: (realLineCount, lineHeightPx, paddingTopPx, paddingBottomPx)
        Best-effort; falls back to reasonable defaults.
        """
        real_line_count = 1
        line_height = 16.0
        pad_top = 0.0
        pad_bottom = 0.0

        try:
            buf = self.view.get_buffer()
            try:
                real_line_count = int(buf.get_line_count())
            except Exception:
                end_it = buf.get_end_iter()
                real_line_count = int(end_it.get_line() + 1)

            try:
                it0 = buf.get_start_iter()
                _y, h = self.view.get_line_yrange(it0)
                line_height = float(h if h > 0 else line_height)
            except Exception:
                pass

            try:
                pad_top = float(getattr(self.view, "get_top_margin")())
            except Exception:
                try:
                    pad_top = float(self.view.get_property("top-margin"))
                except Exception:
                    pass

            try:
                pad_bottom = float(getattr(self.view, "get_bottom_margin")())
            except Exception:
                try:
                    pad_bottom = float(self.view.get_property("bottom-margin"))
                except Exception:
                    pass

        except Exception:
            pass

        real_line_count = max(1, real_line_count)
        line_height = max(1.0, line_height)
        pad_top = max(0.0, pad_top)
        pad_bottom = max(0.0, pad_bottom)
        return real_line_count, line_height, pad_top, pad_bottom

    def _compute_vscode_layout(self):
        """
        VS Code-like minimap layout (minimapHeightIsEditorHeight path).

        Key points (matching VS Code):
        - logicalScrollHeight = realLineCount*lineHeight + paddingTop + paddingBottom
          (+ scrollBeyondLastLine extra space)
        - sliderHeight = floor(viewportHeight^2 / logicalScrollHeight)
        - computedSliderRatio = (minimapHeight - sliderHeight) / (scrollHeight - viewportHeight)
        - sliderTop = scrollTop * computedSliderRatio
        """
        if self._vadj is None:
            return None

        # ------------------------------------------------------------
        # Track geometry: use TEXT window to avoid theme/padding offsets
        # ------------------------------------------------------------
        alloc = self.map.get_allocation()
        track_y0 = 0.0
        track_h = float(alloc.height if alloc.height > 0 else 1.0)
        try:
            win = self.map.get_window(Gtk.TextWindowType.TEXT)
            if win is not None:
                geo = win.get_geometry()
                if isinstance(geo, (tuple, list)) and len(geo) >= 4:
                    track_y0 = float(geo[1])
                    track_h = float(geo[3] if geo[3] > 0 else track_h)
        except Exception:
            pass
        track_h = max(1.0, track_h)

        # ------------------------------------------------------------
        # Editor scroll metrics from GtkAdjustment (pixels)
        # ------------------------------------------------------------
        lower = float(self._vadj.get_lower())
        upper = float(self._vadj.get_upper())
        viewport_h = float(self._vadj.get_page_size())
        value = float(self._vadj.get_value())

        scroll_h = max(0.0, upper - lower)
        scroll_top = max(0.0, value - lower)
        max_scroll_top = max(0.0, scroll_h - viewport_h)

        # ------------------------------------------------------------
        # VS Code logicalScrollHeight (real lines * lineHeight + padding)
        # ------------------------------------------------------------
        real_lines = 1
        line_height = 16.0
        pad_top = 0.0
        pad_bottom = 0.0

        try:
            buf = self.view.get_buffer()

            # Real line count (best-effort).
            try:
                real_lines = int(buf.get_line_count())
            except Exception:
                end_it = buf.get_end_iter()
                real_lines = int(end_it.get_line() + 1)
            real_lines = max(1, real_lines)

            # Line height in pixels (best-effort).
            try:
                it0 = buf.get_start_iter()
                _yy, hh = self.view.get_line_yrange(it0)
                if hh and hh > 0:
                    line_height = float(hh)
            except Exception:
                pass
            line_height = max(1.0, line_height)

            # Padding/margins (best-effort).
            try:
                pad_top = float(self.view.get_property("top-margin"))
            except Exception:
                pass
            try:
                pad_bottom = float(self.view.get_property("bottom-margin"))
            except Exception:
                pass

        except Exception:
            pass

        pad_top = max(0.0, pad_top)
        pad_bottom = max(0.0, pad_bottom)

        logical_scroll_h = (real_lines * line_height + pad_top + pad_bottom)

        # scrollBeyondLastLine equivalent (VS Code logic).
        if max_scroll_top > 0.0:
            logical_scroll_h += max(0.0, viewport_h - line_height - pad_bottom)

        # IMPORTANT: clamp logicalScrollHeight against Gtk scrollHeight to avoid GTK "extra space" inflation.
        # This is the critical part that fixes "small file = slow drag".
        logical_scroll_h = min(max(1e-9, logical_scroll_h), max(1e-9, scroll_h))

        # ------------------------------------------------------------
        # VS Code slider height (computed in editor pixels), then scaled to minimap pixels
        # sliderHeightPx = floor(viewportHeight^2 / logicalScrollHeight)
        # ------------------------------------------------------------
        slider_h_editor = float(int(max(1.0, (viewport_h * viewport_h) / logical_scroll_h)))
        slider_h_editor = max(1.0, min(viewport_h if viewport_h > 0 else slider_h_editor, slider_h_editor))

        # Scale editor-px slider height into minimap-px (track_h).
        # In Xed, minimap height is essentially editor height, but keep scaling safe.
        if viewport_h > 1e-9:
            slider_h = slider_h_editor * (track_h / viewport_h)
        else:
            slider_h = slider_h_editor

        slider_h = max(1.0, min(track_h, float(int(slider_h))))
        
        # ------------------------------------------------------------
        # Small-file speed boost:
        # When scrolling range is small, make the thumb larger so ratio gets smaller
        # (drag becomes faster and matches VS Code feel).
        # ------------------------------------------------------------
        if max_scroll_top > 0.0:
            smallness = max_scroll_top / max(1e-9, viewport_h)  # how many view-heights can we scroll?
        if smallness < 1.5:
            # Strong boost for very small files
            target = 0.80 * track_h
            slider_h = max(slider_h, target)
        elif smallness < 3.0:
            # Mild boost for moderately small files
            target = 0.65 * track_h
            slider_h = max(slider_h, target)

        slider_h = max(1.0, min(track_h, float(int(slider_h))))

        max_slider_top = max(0.0, track_h - slider_h)

        if max_scroll_top > 0.0 and max_slider_top > 0.0:
            ratio = max_slider_top / max_scroll_top  # scrollTop -> sliderTop
        else:
            ratio = 0.0
            
        # Global speed multiplier (affects small and large files).
        # Larger => faster drag (because delta/ratio gets bigger).
        SPEED_MULT = _DRAG_SPEED_MULT
        if ratio > 0.0:
            ratio = ratio / SPEED_MULT

        slider_top = (scroll_top * ratio) if ratio > 0.0 else 0.0
        slider_top = max(0.0, min(max_slider_top, slider_top))

        return {
            "lower": lower,
            "scroll_top": scroll_top,
            "scroll_height": scroll_h,
            "viewport_height": viewport_h,
            "max_scroll_top": max_scroll_top,
            "track_y0": track_y0,
            "track_h": track_h,
            "slider_top": slider_top,
            "slider_h": slider_h,
            "max_slider_top": max_slider_top,
            "ratio": ratio,
        }

    # --- Drag state ---

    def _begin_drag(self):
        try:
            Gtk.grab_add(self.map)
        except Exception:
            pass

    def _end_drag(self):
        if self._dragging:
            try:
                Gtk.grab_remove(self.map)
            except Exception:
                pass
        self._dragging = False
        self._drag_started = False
        self._press_y = 0.0
        self._initial_pos_y = 0.0
        self._initial_scroll_top = 0.0
        self._computed_slider_ratio = 0.0
        self._max_scroll_top = 0.0
        self._viewport_height = 0.0
        self._scroll_height = 0.0

    # --- Debug drawing ---

    def _on_draw(self, _w, cr):
        if not bool(_CONFIG.data.get("draw_scrubber_area", False)):
            return False

        layout = self._compute_vscode_layout()
        if layout is None:
            return False

        top = layout["track_y0"] + layout["slider_top"]
        h = layout["slider_h"]

        alloc = self.map.get_allocation()
        width = self._capture_width()
        height = float(alloc.height if alloc.height > 0 else 1.0)

        ctx = self.map.get_style_context()
        try:
            rgba = ctx.get_background_color(Gtk.StateFlags.SELECTED)
        except Exception:
            rgba = Gdk.RGBA(0.2, 0.6, 1.0, 0.35)

        # Fill + stroke slider hitbox
        cr.save()
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.22)
        cr.rectangle(0.0, top, width, h)
        cr.fill()

        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.85)
        cr.set_line_width(1.0)
        cr.rectangle(0.5, top + 0.5, max(0.0, width - 1.0), max(0.0, h - 1.0))
        cr.stroke()

        cr.restore()

        return False

    # --- Events ---

    def _on_button_press(self, _w, ev: Gdk.EventButton):
        if ev.button != 1:
            return False

        x = float(ev.x)
        y = float(ev.y)

        layout = self._compute_vscode_layout()
        if layout is None:
            _debug("no vadj/layout -> native left click")
            return False

        track_y0 = float(layout["track_y0"])
        slider_top_widget = track_y0 + float(layout["slider_top"])
        slider_h = float(layout["slider_h"])
        ratio = float(layout["ratio"])
        max_scroll_top = float(layout["max_scroll_top"])
        lower = float(layout["lower"])
        scroll_top = float(layout["scroll_top"])

        # Hit-test: prefer GtkSourceMap overlay (visual highlight), but always include the slider rect.
        # This keeps clicks working even if GtkSourceMap overlay math differs slightly.
        inside_y = (slider_top_widget <= y <= (slider_top_widget + slider_h))

        # Try overlay hitbox (best-effort) to match the visible highlight.
        try:
            overlay = self._get_overlay_hit_y()
            if overlay is not None:
                o_top, o_h = overlay
                o_h = max(1.0, min(float(layout["track_h"]), float(o_h)))
                o_top = max(track_y0, min(track_y0 + float(layout["track_h"]) - o_h, float(o_top)))
                pad = 4.0
                inside_y = inside_y or ((o_top - pad) <= y <= (o_top + o_h + pad))
        except Exception:
            pass

        # Only restrict interception by capture width when OUTSIDE.
        if (not inside_y) and (x > self._capture_width()):
            return False

        if inside_y:
            # Arm drag only (VS Code-like): no jump on press.
            self._dragging = True
            self._drag_started = False
            self._press_y = y
            self._initial_pos_y = y
            self._initial_scroll_top = scroll_top
            self._computed_slider_ratio = ratio
            self._max_scroll_top = max_scroll_top
            self._viewport_height = float(layout["viewport_height"])
            self._scroll_height = float(layout["scroll_height"])

            self._begin_drag()
            _debug(
                f"press INSIDE: y={y:.1f} sliderTop={slider_top_widget:.1f} "
                f"sliderH={slider_h:.1f} ratio={ratio:.6f}"
            )
            return True

        # Outside: optional jump (VS Code-like touch behavior).
        if bool(_CONFIG.data.get("disable_click_outside", False)):
            _debug("press OUTSIDE -> ignored (disable_click_outside=1)")
            self._end_drag()
            return True

        if ratio <= 0.0 or max_scroll_top <= 0.0:
            _debug("press OUTSIDE -> jump (no scroll range)")
            self._end_drag()
            return True

        local_y = y - track_y0
        desired = (local_y - (slider_h / 2.0)) / ratio  # inverse of sliderTop = scrollTop * ratio
        desired = max(0.0, min(max_scroll_top, desired))
        self._vadj.set_value(lower + desired)
        _debug(f"press OUTSIDE -> jump: y={y:.1f}")
        self._end_drag()
        return True

    def _on_motion(self, _w, ev: Gdk.EventMotion):
        if not self._dragging:
            return False

        if not (ev.state & Gdk.ModifierType.BUTTON1_MASK):
            _debug("motion without BUTTON1 -> cancel drag")
            self._end_drag()
            return True

        y = float(ev.y)
        x = float(ev.x)

        if (not self._drag_started) and abs(y - float(self._press_y)) < _DRAG_THRESHOLD_PX:
            return True

        ratio = float(self._computed_slider_ratio)
        max_scroll_top = float(self._max_scroll_top)

        if ratio <= 0.0 or max_scroll_top <= 0.0 or self._vadj is None:
            return True

        self._drag_started = True

        pointer_delta = y - float(self._initial_pos_y)
        desired = float(self._initial_scroll_top) + (pointer_delta / ratio)
        desired = max(0.0, min(max_scroll_top, desired))
        self._vadj.set_value(float(self._vadj.get_lower()) + desired)

        if bool(_CONFIG.data.get("draw_scrubber_area", False)):
            try:
                self.map.queue_draw()
            except Exception:
                pass

        return True

    def _on_button_release(self, _w, ev: Gdk.EventButton):
        if ev.button != 1:
            return False
        if not self._dragging:
            return False

        _debug("release -> end drag")
        self._end_drag()

        if bool(_CONFIG.data.get("draw_scrubber_area", False)):
            try:
                self.map.queue_draw()
            except Exception:
                pass

        return True

    # --- Overlay hitbox (visual highlight) ---

    def _is_text_iter(self, obj) -> bool:
        return hasattr(obj, "get_offset") and hasattr(obj, "get_line")

    def _get_overlay_hit_y(self):
        """
        Compute overlay bounds (the visible-region highlight) in minimap widget coords.

        This is used ONLY for hit-testing (making the overlay clickable).
        Drag math uses VS Code slider ratio (vadj-based) for stability.
        """
        try:
            if self.view is None:
                return None

            visible_area = self.view.get_visible_rect()
            res = self.view.get_iter_at_location(int(visible_area.x), int(visible_area.y))

            it = None
            if self._is_text_iter(res):
                it = res
            elif isinstance(res, (tuple, list)) and len(res) >= 2:
                a, b = res[0], res[1]
                if self._is_text_iter(a):
                    it = a
                elif self._is_text_iter(b):
                    it = b

            if it is None:
                return None

            iter_area = self.map.get_iter_location(it)
            _wx, wy = self.map.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET,
                int(iter_area.x),
                int(iter_area.y),
            )
            top = float(wy)

            view_alloc = self.view.get_allocation()
            _vmin, view_height = self.view.get_preferred_height()
            _mmin, child_height = self.map.get_preferred_height()

            view_height = float(view_height)
            child_height = float(child_height)
            if view_height <= 0.0:
                return None

            h = (float(view_alloc.height) / view_height) * child_height + float(iter_area.height)
            return top, float(h)

        except Exception:
            return None

# -------------------- Per-view attachment --------------------

class SmartOverviewViewActivatable(GObject.Object, Xed.ViewActivatable):
    view = GObject.Property(type=Xed.View)

    def __init__(self):
        super().__init__()
        self._hook = None
        self._idle_id = 0

    def do_activate(self):
        if not self._idle_id:
            self._idle_id = GLib.idle_add(self._attach_hook)

    def do_deactivate(self):
        if self._idle_id:
            try:
                GLib.source_remove(self._idle_id)
            except Exception:
                pass
            self._idle_id = 0

        if self._hook is not None:
            try:
                self._hook.disconnect()
            except Exception:
                pass
            self._hook = None

    def do_update_state(self):
        pass

    def _attach_hook(self):
        self._idle_id = 0
        src_map = self._find_map_for_view(self.view)
        if src_map is None:
            _debug("GtkSourceMap not found for this view")
            return False
        self._hook = _MapHook(src_map, self.view)
        return False

    def _find_map_for_view(self, view):
        root = view
        for _ in range(16):
            p = root.get_parent() if root is not None else None
            if p is None:
                break
            root = p

        search_root = root if root is not None else view

        candidates = []

        def children_of(w):
            try:
                if isinstance(w, Gtk.Container):
                    return w.get_children()
            except Exception:
                return []
            return []

        q = [search_root]
        while q:
            w = q.pop(0)
            if _gtype_name(w) == "GtkSourceMap":
                candidates.append(w)
            q.extend(children_of(w))

        for m in candidates:
            try:
                if m.get_property("view") is view:
                    return m
            except Exception:
                pass

        return None

# -------------------- Window activatable (Preferences live here) --------------------

if _HAVE_PEASGTK:
    class SmartOverviewPlugin(GObject.Object, Xed.WindowActivatable, PeasGtk.Configurable):  # type: ignore
        __gtype_name__ = "XedSmartOverviewPlugin"
        window = GObject.Property(type=Xed.Window)

        def __init__(self):
            super().__init__()
            _CONFIG.load()

        def do_activate(self):
            pass

        def do_deactivate(self):
            pass

        def do_update_state(self):
            pass

        # PeasGtk.Configurable
        def do_create_configure_widget(self):
            _CONFIG.load()
            return ConfigureWidget(_CONFIG, self._on_config_changed)

        def _on_config_changed(self, cfg: dict) -> None:
            _CONFIG.data.update(cfg)
            _CONFIG.save()
else:
    class SmartOverviewPlugin(GObject.Object, Xed.WindowActivatable):
        __gtype_name__ = "XedSmartOverviewPlugin"
        window = GObject.Property(type=Xed.Window)

        def do_activate(self):
            if _DEBUG:
                _debug("PeasGtk not available; preferences UI disabled")

        def do_deactivate(self):
            pass

        def do_update_state(self):
            pass
