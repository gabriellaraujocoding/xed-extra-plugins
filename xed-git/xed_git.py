# -*- coding: utf-8 -*-
#
# Copyright (c) 2013 Ignacio Casal Quinteiro
# Copyright (c) 2014 Garrett Regier
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
Git Gutter plugin for Xed (Linux Mint).

Shows Git working-tree changes directly in the editor gutter for files inside a Git repository.

Features:
- Draws colored markers in the LEFT gutter to indicate line changes:
  - Added, Modified, Removed
- Tooltip preview for Removed/Modified hunks (shows removed lines).
- Updates automatically when:
  - The buffer is loaded
  - The buffer changes (with a size-aware delay to avoid excessive work)
- Handles new/untracked files by marking all lines as Added.

Notes:
- Uses Ggit (libgit2 bindings) to discover/open repositories and read HEAD contents.
- Only operates on local files (non-file URIs are ignored).

Debug:
- Set XED_DEBUG_GIT=1 to print debug logs.
"""

import gi
import inspect
import io
import os
import sys
import traceback
import abc
import collections
import queue
import threading
import traceback
import os.path
import difflib

# Prefer Xed; fallback to Gedit (so the same code can run in both editors).
try:
    gi.require_version('Xed', '1.0')
except ValueError:
    gi.require_version('Gedit', '3.0')

gi.require_version('Gtk', '3.0')
gi.require_version('Ggit', '1.0')

# gedit and xed share a very similar libpeas/GI API.
# On xed, the namespace is "Xed", so we alias it to "Gedit" to keep the plugin code unchanged.
try:
    from gi.repository import Xed as Gedit
except ImportError:
    from gi.repository import Gedit

from gi.repository import GLib, Gdk, Gtk, GtkSource, GObject, Gio, Ggit

_DEBUG = os.getenv('XED_DEBUG_GIT') is not None
 
def debug(msg, *, frames=1, print_stack=False, limit=None):
    """Mimicks Gedit's gedit_debug_message() output, but only prints
       when the XED_DEBUG_GIT enviroment variable exists.
    """
    if not _DEBUG:
        return
 
    current_frame = inspect.currentframe()
    calling_frame = current_frame
 
    try:
        for i in range(frames):
            calling_frame = calling_frame.f_back
 
        info = inspect.getframeinfo(calling_frame)
 
        path = min(info.filename.replace(x, '') for x in sys.path)
        if path[0] == os.path.sep:
            path = path[1:]
 
        full_message = io.StringIO()
        full_message.writelines((path, ':', str(info.lineno),
                                 ' (', info.function, ') ', msg, '\n'))
 
        if print_stack:
            full_message.write('Stack (most recent call last):\n')
            traceback.print_stack(calling_frame,
                                  file=full_message, limit=limit)
 
        if full_message.getvalue()[-1] != '\n':
            full_message.write('\n')
 
        # Always write the message in a single call to prevent
        # the message from being split when using multiple threads
        sys.stderr.write(full_message.getvalue())
 
        full_message.close()
 
    finally:
        # Avoid leaking
        del calling_frame
        del current_frame

class WorkerThread(threading.Thread):
    __metaclass__ = abc.ABCMeta

    __sentinel = object()

    def __init__(self, callback, chunk_size=1, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__callback = callback
        self.__chunk_size = chunk_size

        self.__quit = threading.Event()
        self.__has_idle = threading.Event()

        self.__tasks = queue.Queue()
        self.__results = collections.deque()

    @abc.abstractmethod
    def handle_task(self, *args, **kwargs):
        raise NotImplementedError

    # TODO: add, put, push?
    def push(self, *args, **kwargs):
        self.__tasks.put((args, kwargs))

    def __close(self, process_results):
        self.__quit.set()

        # Prevent the queue.get() from blocking forever
        self.__tasks.put(self.__sentinel)

        super().join()

        if not process_results:
            self.__results.clear()

        else:
            while self.__in_idle() is GLib.SOURCE_CONTINUE:
                pass

    def terminate(self):
        self.__close(False)

    def join(self):
        self.__close(True)

    def clear(self):
        old_tasks = self.__tasks
        self.__tasks = queue.Queue(1)

        # Prevent the queue.get() from blocking forever
        old_tasks.put(self.__sentinel)

        # Block until the old queue has finished, otherwise
        # a old result could be added to the new results queue
        self.__tasks.put(self.__sentinel)
        self.__tasks.put(self.__sentinel)

        old_tasks = self.__tasks
        self.__tasks = queue.Queue()

        # Switch to the new queue
        old_tasks.put(self.__sentinel)

        # Finally, we can now create a new deque without
        # the possibility of any old results being added to it
        self.__results.clear()

    def run(self):
        while not self.__quit.is_set():
            task = self.__tasks.get()
            if task is self.__sentinel:
                continue

            args, kwargs = task

            try:
                result = self.handle_task(*args, **kwargs)

            except Exception:
                traceback.print_exc()
                continue

            self.__results.append(result)

            # Avoid having an idle for every result
            if not self.__has_idle.is_set():
                self.__has_idle.set()

                debug('%s<%s>: result callback idle started' %
                      (type(self).__name__, self.name))
                GLib.source_set_name_by_id(GLib.idle_add(self.__in_idle),
                                           '[gedit] git %s result callback idle' %
                                           (type(self).__name__,))

    def __in_idle(self):
        try:
            for i in range(self.__chunk_size):
                result = self.__results.popleft()

                try:
                    self.__callback(result)

                except Exception:
                    traceback.print_exc()

        except IndexError:
            # Must be cleared before we check the results length
            self.__has_idle.clear()

            # Only remove the idle when there are no more items,
            # some could have been added after the IndexError was raised
            if len(self.__results) == 0:
                debug('%s<%s>: result callback idle finished' %
                      (type(self).__name__, self.name))
                return GLib.SOURCE_REMOVE

        return GLib.SOURCE_CONTINUE

class DiffType:
    (NONE,
     ADDED,
     MODIFIED,
     REMOVED) = range(4)

class DiffRenderer(GtkSource.GutterRenderer):

    backgrounds = {}
    backgrounds[DiffType.ADDED] = Gdk.RGBA()
    backgrounds[DiffType.MODIFIED] = Gdk.RGBA()
    backgrounds[DiffType.REMOVED] = Gdk.RGBA()
    backgrounds[DiffType.ADDED].parse("#8ae234")
    backgrounds[DiffType.MODIFIED].parse("#fcaf3e")
    backgrounds[DiffType.REMOVED].parse("#ef2929")

    def __init__(self):
        GtkSource.GutterRenderer.__init__(self)

        self.set_size(8)
        self.set_padding(3, 0)

        self.file_context = {}
        self.tooltip = None
        self.tooltip_line = 0

    def do_draw(self, cr, bg_area, cell_area, start, end, state):
        GtkSource.GutterRenderer.do_draw(self, cr, bg_area, cell_area,
                                         start, end, state)

        line_context = self.file_context.get(start.get_line() + 1, None)
        if line_context is None or line_context.line_type == DiffType.NONE:
            return

        background = self.backgrounds[line_context.line_type]

        Gdk.cairo_set_source_rgba(cr, background)
        cr.rectangle(cell_area.x, cell_area.y,
                     cell_area.width, cell_area.height)
        cr.fill()

    def do_query_tooltip(self, it, area, x, y, tooltip):
        line = it.get_line() + 1

        line_context = self.file_context.get(line, None)
        if line_context is None:
            return False

        # Check that the context is the same not the line this
        # way contexts that span multiple times are handled correctly
        if self.file_context.get(self.tooltip_line, None) is line_context:
            tooltip.set_custom(None)
            tooltip.set_custom(self.tooltip)
            return True

        if line_context.line_type not in (DiffType.REMOVED, DiffType.MODIFIED):
            return False

        tooltip_buffer = GtkSource.Buffer()
        tooltip_view = GtkSource.View.new_with_buffer(tooltip_buffer)

        # Propagate the view's settings
        content_view = self.get_view()
        tooltip_view.set_indent_width(content_view.get_indent_width())
        tooltip_view.set_tab_width(content_view.get_tab_width())

        # Propagate the buffer's settings
        content_buffer = content_view.get_buffer()
        tooltip_buffer.set_highlight_syntax(content_buffer.get_highlight_syntax())
        tooltip_buffer.set_language(content_buffer.get_language())
        tooltip_buffer.set_style_scheme(content_buffer.get_style_scheme())

        # Fix some styling issues
        tooltip_buffer.set_highlight_matching_brackets(False)
        tooltip_view.set_border_width(4)
        tooltip_view.set_cursor_visible(False)

        # Set the font
        content_style_context = content_view.get_style_context()
        content_font = content_style_context.get_font(Gtk.StateFlags.NORMAL)
        tooltip_view.override_font(content_font)

        # Only add what can be shown, we
        # don't want to add hundreds of lines
        allocation = content_view.get_allocation()
        lines = allocation.height // area.height
        removed = '\n'.join(map(str, line_context.removed_lines[:lines]))
        tooltip_buffer.set_text(removed)

        # Avoid having to create the tooltip multiple times
        self.tooltip = tooltip_view
        self.tooltip_line = line

        tooltip.set_custom(tooltip_view)
        return True

    def set_file_context(self, file_context):
        self.file_context = file_context
        self.tooltip = None
        self.tooltip_line = 0

        self.queue_draw()

class GitAppActivatable(GObject.Object, Gedit.AppActivatable):
    app = GObject.Property(type=Gedit.App)

    __instance = None

    def __init__(self):
        super().__init__()

        Ggit.init()

        GitAppActivatable.__instance = self

    def do_activate(self):
        self.clear_repositories()

    def do_deactivate(self):
        self.__git_repos = None
        self.__workdir_repos = None

    @classmethod
    def get_instance(cls):
        return cls.__instance

    def clear_repositories(self):
        self.__git_repos = {}
        self.__workdir_repos = {}

    def get_repository(self, location, is_dir, *, allow_git_dir=False):
        # The repos are cached by the directory
        dir_location = location if is_dir else location.get_parent()
        dir_uri = dir_location.get_uri()

        # Fast Path
        try:
            return self.__workdir_repos[dir_uri]

        except KeyError:
            pass

        try:
            repo = self.__git_repos[dir_uri]

        except KeyError:
            pass

        else:
            return repo if allow_git_dir else None

        # Doing remote operations is too slow
        if not location.has_uri_scheme('file'):
            return None

        # Must check every dir, otherwise submodules will have issues
        try:
            repo_file = Ggit.Repository.discover(location)

        except GLib.Error:
            # Prevent trying to find a git repository
            # for every file in this directory
            self.__workdir_repos[dir_uri] = None
            return None

        repo_uri = repo_file.get_uri()

        # Reuse the repo if requested multiple times
        try:
            repo = self.__git_repos[repo_uri]

        except KeyError:
            repo = Ggit.Repository.open(repo_file)

            # TODO: this was around even when not used, on purpose?
            head = repo.get_head()
            commit = repo.lookup(head.get_target(), Ggit.Commit)
            tree = commit.get_tree()

            self.__git_repos[repo_uri] = repo

        # Need to keep the caches for workdir and
        # the .git dir separate to support allow_git_dir
        if dir_uri.startswith(repo_uri):
            top_uri = repo_uri
            repos = self.__git_repos

        else:
            top_uri = repo.get_workdir().get_uri()
            repos = self.__workdir_repos

        # Avoid trouble with symbolic links
        while dir_uri.startswith(top_uri):
            repos[dir_uri] = repo

            dir_location = dir_location.get_parent()
            dir_uri = dir_location.get_uri()

            # Avoid caching the repo all the
            # way up to the top dir each time
            if dir_uri in repos:
                break

        if repos is self.__git_repos:
            return repo if allow_git_dir else None

        return repo

class LineContext:
    __slots__ = ('removed_lines', 'line_type')

    def __init__(self):
        self.removed_lines = []
        self.line_type = DiffType.NONE

class GitViewActivatable(GObject.Object, Gedit.ViewActivatable):
    view = GObject.Property(type=Gedit.View)

    status = GObject.Property(type=Ggit.StatusFlags,
                              default=Ggit.StatusFlags.CURRENT)

    def __init__(self):
        super().__init__()
        
        # Must exist even if do_activate() never fully runs
        self.buffer = None
        self.buffer_signals = []
        self.view_signals = []
        self.gutter = None
        self.diff_renderer = None        
        self._active = False  
        self.diff_timeout = 0
        self.file_contents_list = None
        self.file_context = None
        self.changed_sid = 0

    def do_activate(self):
        self._active = True
        #GitWindowActivatable.register_view_activatable(self)

        self.app_activatable = GitAppActivatable.get_instance()

        self.diff_renderer = DiffRenderer()
        self.gutter = self.view.get_gutter(Gtk.TextWindowType.LEFT)

        # Always reserve gutter space (even outside git repos)
        self.gutter.insert(self.diff_renderer, 40)
        self.diff_renderer.set_file_context({})

        # Note: GitWindowActivatable will call
        #       update_location() for us when needed
        self.view_signals = [
            self.view.connect('notify::buffer', self.on_notify_buffer)
        ]

        self.buffer = None
        self.on_notify_buffer(self.view)

    def do_deactivate(self):
        self._active = False
        if self.diff_timeout != 0:
            GLib.source_remove(self.diff_timeout)

        self.disconnect_buffer()
        self.buffer = None

        #self.disconnect_view()
        #self.gutter.remove(self.diff_renderer)
        
        self.disconnect_view()

        # do_activate() may not have completed, so guard gutter/diff_renderer
        if self.gutter is not None and self.diff_renderer is not None:
            try:
                self.gutter.remove(self.diff_renderer)
            except Exception:
                pass

        self.gutter = None
        self.diff_renderer = None
        
    def disconnect(self, obj, signals):
        # Defensive: only disconnect valid handler ids.
        for sid in list(signals):
            if not sid or sid <= 0:
                continue
            try:
                obj.disconnect(sid)
            except Exception:
                pass
        signals[:] = []

    def disconnect_buffer(self):
        buf = getattr(self, "buffer", None)
        if buf is None:
            return

        # Disconnect our manual 'changed' handler if connected
        sid = getattr(self, "changed_sid", 0)
        if sid:
            try:
                buf.disconnect(sid)
            except Exception:
                pass
            self.changed_sid = 0

        # Disconnect other buffer signals if present
        self.disconnect(buf, getattr(self, "buffer_signals", []))


    def disconnect_view(self):
        if hasattr(self, 'view_signals'):
            self.disconnect(self.view, self.view_signals)

    def on_notify_buffer(self, view, gspec=None):
        if not self._active:
            return
            
        if self.diff_timeout != 0:
            GLib.source_remove(self.diff_timeout)
            self.diff_timeout = 0

        if self.buffer:
            self.disconnect_buffer()

        self.buffer = view.get_buffer()
        self.changed_sid = 0

        # The changed signal is connected to in update_location().
        # The saved signal is pointless as the window activatable
        # will see the change and call update_location().
        self.buffer_signals = [
            self.buffer.connect('loaded', self.update_location)
        ]

        # We wait and let the loaded signal call
        # update_location() as the buffer is currently empty

    # TODO: This can be called many times and by idles,
    #       should instead do the work in another thread
    def update_location(self, *args):
        if not self._active:
            return
            
        self.location = self.buffer.get_file().get_location()

        repo = None
        if self.location is not None:
            repo = self.app_activatable.get_repository(self.location, False)

        if self.location is None or repo is None:
            # Keep renderer inserted; just clear colors
            self.diff_renderer.set_file_context({})
            self.file_context = None
            self.file_contents_list = None

            # Disconnect 'changed' if we had connected it before
            if self.changed_sid:
                self.buffer.disconnect(self.changed_sid)
                self.changed_sid = 0

            return

        # We are inside a git repo: ensure we track edits
        if self.file_contents_list is None and not self.changed_sid:
            self.changed_sid = self.buffer.connect('changed', self.update)

        try:
            head = repo.get_head()
            commit = repo.lookup(head.get_target(), Ggit.Commit)
            tree = commit.get_tree()
            relative_path = os.path.relpath(
                os.path.realpath(self.location.get_path()),
                repo.get_workdir().get_path()
            )

            entry = tree.get_by_path(relative_path)
            file_blob = repo.lookup(entry.get_id(), Ggit.Blob)
            try:
                gitconfig = repo.get_config()
                encoding = gitconfig.get_string('gui.encoding')
            except GLib.Error:
                encoding = 'utf8'
            file_contents = file_blob.get_raw_content().decode(encoding)
            self.file_contents_list = file_contents.splitlines()

            # Remove the last empty line added automatically
            if self.file_contents_list:
                last_item = self.file_contents_list[-1]
                if last_item[-1:] == '\n':
                    self.file_contents_list[-1] = last_item[:-1]

        except GLib.Error:
            # New file in a git repository
            self.file_contents_list = []

        self.update()

    def update(self, *unused):
        if not self._active:
            return
                
        # We don't let the delay accumulate
        if self.diff_timeout != 0:
            return

        # Do the initial diff without a delay
        if self.file_context is None:
            self.on_diff_timeout()

        else:
            n_lines = self.buffer.get_line_count()
            delay = min(10000, 200 * (n_lines // 2000 + 1))

            self.diff_timeout = GLib.timeout_add(delay,
                                                 self.on_diff_timeout)

    def on_diff_timeout(self):
        if not self._active:
            return
                
        self.diff_timeout = 0

        # Must be a new file
        if not self.file_contents_list:
            self.status = Ggit.StatusFlags.WORKING_TREE_NEW

            n_lines = self.buffer.get_line_count()
            if len(self.diff_renderer.file_context) == n_lines:
                return False

            line_context = LineContext()
            line_context.line_type = DiffType.ADDED
            file_context = dict(zip(range(1, n_lines + 1),
                                    [line_context] * n_lines))

            self.diff_renderer.set_file_context(file_context)
            return False

        start_iter, end_iter = self.buffer.get_bounds()
        src_contents = start_iter.get_visible_text(end_iter)
        src_contents_list = src_contents.splitlines()

        # GtkTextBuffer does not consider a trailing "\n" to be text
        if len(src_contents_list) != self.buffer.get_line_count():
            src_contents_list.append('')

        diff = difflib.unified_diff(self.file_contents_list,
                                    src_contents_list, n=0)

        # Skip the first 2 lines: ---, +++
        try:
            next(diff)
            next(diff)

        except StopIteration:
            # Nothing has changed
            self.status = Ggit.StatusFlags.CURRENT

        else:
            self.status = Ggit.StatusFlags.WORKING_TREE_MODIFIED

        file_context = {}
        for line_data in diff:
            if line_data[0] == '@':
                for token in line_data.split():
                    if token[0] == '+':
                        hunk_point = int(token.split(',', 1)[0])
                        line_context = LineContext()
                        break

            elif line_data[0] == '-':
                if line_context.line_type == DiffType.NONE:
                    line_context.line_type = DiffType.REMOVED

                line_context.removed_lines.append(line_data[1:])

                # No hunk point increase
                file_context[hunk_point] = line_context

            elif line_data[0] == '+':
                if line_context.line_type == DiffType.NONE:
                    line_context.line_type = DiffType.ADDED
                    file_context[hunk_point] = line_context

                elif line_context.line_type == DiffType.REMOVED:
                    # Why is this the only one that does
                    # not add it to file_context?
                    line_context.line_type = DiffType.MODIFIED

                else:
                    file_context[hunk_point] = line_context

                hunk_point += 1

        # Occurs when all of the original content is deleted
        if 0 in file_context:
            for i in reversed(list(file_context.keys())):
                file_context[i + 1] = file_context[i]
                del file_context[i]

        self.file_context = file_context
        self.diff_renderer.set_file_context(file_context)
        return False

# ex:ts=4:et:
