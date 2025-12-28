# Xed Extra Plugins

A practical collection of **extra plugins for Xed (Linux Mint)**, mainly focused on **programming productivity**.

This repository is organized as **one folder per plugin**. Each folder contains its own `.plugin` descriptor and one or more `.py` files (plus a plugin-specific README).

## Included plugins

- **xed-git**: highlights lines changed since the last commit (**green=added, orange=modified, red=removed**).
- **xed-indentation-guides**: **VS Code-like indentation guides** inside the editor based on leading whitespace and tab width.
- **xed-quick-highlight**: highlights occurrences of the currently selected text.
- **xed-source-code-browser**: **symbol tree** (functions/classes/macros/variables, etc.) for the current document using **ctags**.
- **xed-split-pane**: split workflow with a **pinned LEFT pane** + normal tabbed editor on the **RIGHT**.
- **xed-terminal**: embedded **VTE terminal** in the bottom panel (tabs + preferences).

## Install

Create the Xed plugins directory:

```bash
mkdir -p ~/.local/share/xed/plugins
```

Copy the plugin folder(s) you want:

```bash
cp -r xed-git ~/.local/share/xed/plugins/
cp -r xed-indentation-guides ~/.local/share/xed/plugins/
cp -r xed-quick-highlight ~/.local/share/xed/plugins/
cp -r xed-source-code-browser ~/.local/share/xed/plugins/
cp -r xed-split-pane ~/.local/share/xed/plugins/
cp -r xed-terminal ~/.local/share/xed/plugins/
```

Restart Xed and enable them:
- **Edit → Preferences → Plugins**

## Uninstall

Remove the folder(s) and restart Xed:

```bash
rm -rf ~/.local/share/xed/plugins/xed-git
rm -rf ~/.local/share/xed/plugins/xed-indentation-guides
rm -rf ~/.local/share/xed/plugins/xed-quick-highlight
rm -rf ~/.local/share/xed/plugins/xed-source-code-browser
rm -rf ~/.local/share/xed/plugins/xed-split-pane
rm -rf ~/.local/share/xed/plugins/xed-terminal
```

## Dependencies

On **Linux Mint / Ubuntu / Debian**, these plugins require:

Common (all plugins):
- `python3`
- `python3-gi`
- `gir1.2-gtk-3.0`

Plugin-specific:
- **xed-git**: `gir1.2-ggit-1.0` + `gir1.2-gtksource-3.0`
- **xed-indentation-guides**: `gir1.2-gtksource-3.0`
- **xed-quick-highlight**: `gir1.2-gtksource-3.0`
- **xed-source-code-browser**: `ctags` (recommended: universal-ctags)
- **xed-split-pane**: no extra dependencies
- **xed-terminal**: `libvte-2.91-0` + `gir1.2-vte-2.91`

### Install dependencies (Linux Mint / Ubuntu / Debian)

```bash
sudo apt update

# common
sudo apt install -y python3 python3-gi gir1.2-gtk-3.0

# GtkSourceView plugins: xed-git, xed-indentation-guides, xed-quick-highlight
sudo apt install -y gir1.2-gtksource-3.0

# xed-git
sudo apt install -y gir1.2-ggit-1.0

# xed-source-code-browser
sudo apt install -y ctags

# xed-terminal
sudo apt install -y libvte-2.91-0 gir1.2-vte-2.91
```

> Package names may vary slightly on other distributions.

### Quick checks

```bash
# common
python3 --version
python3 -c "import gi; from gi.repository import GLib; print('PyGObject OK')"
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk; print('GTK OK')"

# GtkSourceView (xed-git, xed-indentation-guides, xed-quick-highlight)
python3 -c "import gi; gi.require_version('GtkSource','3.0'); from gi.repository import GtkSource; print('GtkSource OK')"

# VTE (xed-terminal)
python3 -c "import gi; gi.require_version('Vte','2.91'); from gi.repository import Vte; print('VTE OK')"

# Ggit (xed-git)
python3 -c "import gi; gi.require_version('Ggit','1.0'); from gi.repository import Ggit; print('Ggit OK')"

# ctags (xed-source-code-browser)
ctags --version
```

## Debug

Run Xed from a terminal with the plugin debug variable:

- xed-git:
  ```bash
  XED_DEBUG_GIT=1 xed
  ```
- xed-indentation-guides:
  ```bash
  XED_DEBUG_INDENTATION_GUIDES=1 xed
  ```
- xed-quick-highlight:
  ```bash
  XED_DEBUG_QUICK_HIGHLIGHT=1 xed
  ```
- xed-source-code-browser:
  ```bash
  XED_DEBUG_SOURCE_CODE_BROWSER=1 xed
  ```
- xed-split-pane:
  ```bash
  XED_DEBUG_SPLIT_PANE=1 xed
  ```
- xed-terminal:
  ```bash
  XED_DEBUG_TERMINAL=1 xed
  ```

## Credits

- Developed and maintained for Xed by **Gabriell Araujo (2025)**.
- **xed-git** is based on the original **gedit Git plugin** by **Ignacio Casal Quinteiro** and **Garrett Regier**.
- **xed-quick-highlight** is based on the original **gedit Quick Highlight plugin** by **Martin Blanchard**.
- **xed-source-code-browser** is based on the original **Pluma Source Code Browser plugin** by **Micah Carrick** and **MATE Developers**.
- **xed-terminal** is based on the original **gedit embedded terminal plugin** by **Paolo Borelli**.
- Other plugins are inspired by ideas from **Geany**, **Gedit**, **Pluma**, and **Visual Studio Code**.

## Licenses

This repository contains **multiple licenses** (per-plugin). You can also rely on each file’s `SPDX-License-Identifier` header.

| Plugin folder | SPDX license |
|---|---|
| `xed-git` | GPL-2.0-or-later |
| `xed-indentation-guides` | GPL-2.0-or-later |
| `xed-quick-highlight` | GPL-2.0-or-later |
| `xed-source-code-browser` | BSD-3-Clause |
| `xed-split-pane` | GPL-2.0-or-later |
| `xed-terminal` | GPL-2.0-or-later |

> The full license text for each plugin is available in each plugin folder (see the `LICENSE` file).

## Screenshots

### xed-git
![xed-git](screenshots/xed-git.png)

### xed-indentation-guides
![xed-indentation-guides](screenshots/xed-indentation-guides.png)

### xed-quick-highlight
![xed-quick-highlight](screenshots/xed-quick-highlight.png)

### xed-source-code-browser
![xed-source-code-browser](screenshots/xed-source-code-browser.png)

### xed-split-pane
![xed-split-pane](screenshots/xed-split-pane-chooser.png)
![xed-split-pane](screenshots/xed-split-pane.png)

### xed-terminal
![xed-terminal](screenshots/xed-terminal.png)
