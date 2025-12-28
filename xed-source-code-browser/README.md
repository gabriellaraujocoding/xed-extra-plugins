# xed-source-code-browser

A **Source Code Browser** for **Xed (Linux Mint)** — shows a symbol tree (functions, classes, variables, macros, etc.) for the current document using **ctags**.

## Features
- Symbol tree grouped by **kind** (Classes, Functions, Macros, Variables, …)
- Jump to definition when activating a symbol
- Optional display of **line numbers** in the tree
- Optional **alphabetical sorting**
- Optional **start expanded**
- Optional **show/hide icons** in the tree
- Optional parsing of **remote/non-local** files (saved to a temporary local file)
- Performance-focused:
  - Reload is **debounced** (default: 200ms)
  - Loads symbols only when the plugin panel item is active (when supported)

## How it works
- On tab/document changes, the plugin runs `ctags` for the active file.
- The ctags output is parsed into tags and kinds, then rendered as a Gtk TreeView.
- Activating a symbol jumps the editor to the reported line.

## Usage
- Enable the plugin (see Install).
- Open any source file — the symbol tree updates automatically.
- Activate a symbol (Enter / click) to jump to its definition.
- Expand/collapse nodes using double-click.

## Preferences
Open **Edit → Preferences → Plugins → Xed Source Code Browser → Configure**.

Settings are stored in JSON:
`~/.config/xed/xed_source_code_browser.json`

Key options (defaults):
- `show_line_numbers`: `true`
- `load_remote_files`: `true`
- `expand_rows`: `true`
- `sort_list`: `true`
- `show_icons`: `true`
- `ctags_executable`: `"ctags"`
- `reload_debounce_ms`: `200`

## Install
### Dependencies (Linux Mint / Ubuntu / Debian)
- Xed with Python (GI) plugin support (default on Linux Mint)
- **ctags**
  - Recommended: **universal-ctags**
  - On Mint/Ubuntu, the package named `ctags` may be either Exuberant or Universal depending on release.

Install:
```bash
sudo apt update
sudo apt install -y ctags
```

### Copy folder
```bash
mkdir -p ~/.local/share/xed/plugins/
cp -r xed-source-code-browser ~/.local/share/xed/plugins/
```

### Restart Xed and enable the plugin
**Edit → Preferences → Plugins → Xed Source Code Browser**

## Troubleshooting
- If the tree is empty, verify `ctags` works:
  ```bash
  ctags --version
  ```
- If you use a custom build, set the full path in preferences:
  `ctags_executable`

## Debug
```bash
XED_DEBUG_SOURCE_CODE_BROWSER=1 xed
```

## Credits
- Based on the original **Pluma Source Code Browser plugin** by **Micah Carrick** and **MATE Developers**.
- Xed port by **Gabriell Araujo (2025)**.

## License
**BSD-3-Clause**

## Screenshots

### xed-source-code-browser
![xed-source-code-browser](../screenshots/xed-source-code-browser.png)
