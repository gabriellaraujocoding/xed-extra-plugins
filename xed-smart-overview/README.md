# xed-smart-overview

**VS Code-like overview/minimap** for **Xed (Linux Mint)** — **click and drag the visible overlay** (viewport highlight) to scroll smoothly.

## Why this plugin exists
Fixes unexpected jumps and inconsistent dragging in Xed’s default overview/minimap.

## Features
- **VS Code-like drag behavior**
  - Click **inside** the visible overlay: **no jump**, it only arms dragging
  - Drag with the mouse pressed: scrolls smoothly and predictably
- **No-jump by default**
  - Click **outside** the overlay is **ignored by default** (can be changed in preferences)
- **Works well on both small and huge files**
  - Drag speed stays consistent across file sizes
- Optional **debug scrubber area**
  - Draws the internal scrubber/slider area used for hit-testing (useful for tuning)

## How it works
- Hooks into **GtkSourceMap** (Xed’s overview/minimap).
- Uses the editor **GtkAdjustment** (scroll position and range) as the source of truth.
- Uses the **visible overlay** (viewport highlight) primarily for **hit-testing**, so the overlay is clickable.
- Drag maps mouse delta → scroll delta using a **VS Code-like slider ratio**.

## Usage
1. Enable the plugin (see Install).
2. Open any file with the overview visible.
3. **Click and hold** inside the visible overlay.
4. **Drag** up/down to scroll the document.
5. Release the mouse button to stop dragging.

## Preferences
Open **Edit → Preferences → Plugins → Xed Smart Overview → Configure**.

Settings are stored in:
`~/.config/xed/xed-smart-overview.json`

Key options (defaults):
- `draw_scrubber_area`: `false`
- `disable_click_outside`: `true`

## Install

### Dependencies (Linux Mint / Ubuntu / Debian)
```bash
sudo apt update
sudo apt install -y python3 python3-gi gir1.2-gtk-3.0 gir1.2-gtksource-3.0
```

> Note: Xed already depends on GtkSourceView. If you are missing introspection packages for GtkSourceView on your distro, install the corresponding `gir1.2-gtksource-3.0` (or the matching version available).

### Copy folder
```bash
mkdir -p ~/.local/share/xed/plugins/
cp -r xed-smart-overview ~/.local/share/xed/plugins/
```

### Restart Xed and enable the plugin
**Edit → Preferences → Plugins → Xed Smart Overview**

## Debug
Run Xed with debug enabled:
```bash
XED_DEBUG_SMART_OVERVIEW=1 xed
```

## Credits
- Xed Smart Overview plugin by **Gabriell Araujo (2026)**.

## License
**GPL-2.0-or-later**

## Screenshots

### xed-smart-overview
![xed-smart-overview](../screenshots/xed-smart-overview.png)
