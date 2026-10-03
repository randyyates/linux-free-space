#!/usr/bin/env python3
"""Free Space — see what is eating your disk, and remove it.

Usage: freespace.py [PATH]      (defaults to your home directory)
"""
import heapq
import os
import shutil
import stat
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

APP_ID = "dev.five.FreeSpace"
LARGEST_LIMIT = 500

UNREADABLE = 1  # directory could not be listed
MOUNTPOINT = 2  # directory is another filesystem; not descended into


# ---------------------------------------------------------------- model ----

class Dir:
    """One scanned directory. Files are stored as (name, bytes, mtime) tuples
    to keep memory low on trees with millions of files."""

    __slots__ = ("name", "parent", "size", "count", "mtime", "dirs", "files", "flags")

    def __init__(self, name, parent, size=0, mtime=0.0):
        self.name = name  # the root holds its absolute path here
        self.parent = parent
        self.size = size  # bytes on disk, including everything below
        self.count = 0  # number of entries below (files + dirs)
        self.mtime = mtime
        self.dirs = []
        self.files = []
        self.flags = 0


def node_path(node):
    parts = []
    while node is not None:
        parts.append(node.name)
        node = node.parent
    return os.path.join(*reversed(parts))


def scan(path, cancel=None, progress=None):
    """Walk `path` without following symlinks or crossing filesystems.
    Returns (root Dir, error count), or (None, errors) if cancelled."""
    path = os.path.abspath(path)
    st = os.lstat(path)
    root = Dir(path, None, st.st_blocks * 512, st.st_mtime)
    dev = st.st_dev
    seen_links = set()
    order = []  # pre-order; reversed, every child comes before its parent
    stack = [(root, path)]
    errors = nfiles = 0
    last = time.monotonic()

    while stack:
        if cancel is not None and cancel.is_set():
            return None, errors
        node, p = stack.pop()
        order.append(node)
        try:
            it = os.scandir(p)
        except OSError:
            node.flags |= UNREADABLE
            errors += 1
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    errors += 1
                    continue
                node.count += 1
                if stat.S_ISDIR(st.st_mode):
                    child = Dir(e.name, node, st.st_blocks * 512, st.st_mtime)
                    node.dirs.append(child)
                    if st.st_dev != dev:
                        child.flags |= MOUNTPOINT
                        child.size = 0
                    else:
                        stack.append((child, e.path))
                else:
                    size = st.st_blocks * 512
                    if st.st_nlink > 1:  # count hard-linked data once
                        key = (st.st_dev, st.st_ino)
                        if key in seen_links:
                            size = 0
                        else:
                            seen_links.add(key)
                    node.files.append((e.name, size, st.st_mtime))
                    node.size += size
                    nfiles += 1
        if progress is not None:
            now = time.monotonic()
            if now - last > 0.1:
                last = now
                progress(nfiles, len(order), p)

    for node in reversed(order):
        if node.parent is not None:
            node.parent.size += node.size
            node.parent.count += node.count
    return root, errors


def propagate(node, dsize, dcount):
    while node is not None:
        node.size += dsize
        node.count += dcount
        node = node.parent


def detach_dir(node):
    node.parent.dirs.remove(node)
    propagate(node.parent, -node.size, -(node.count + 1))


def detach_file(parent, file):
    parent.files.remove(file)
    propagate(parent, -file[1], -1)


def replace_dir(old, new):
    """Swap a rescanned subtree in for `old`, fixing ancestor totals."""
    parent = old.parent
    parent.dirs[parent.dirs.index(old)] = new
    new.name, new.parent = old.name, parent
    propagate(parent, new.size - old.size, new.count - old.count)


def iter_files(node):
    stack = [node]
    while stack:
        d = stack.pop()
        for f in d.files:
            yield d, f
        stack.extend(d.dirs)


def fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def mounts():
    """Real (block-device backed) mounted filesystems."""
    out, seen = [], set()
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                dev, mnt, fstype = line.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if not dev.startswith("/dev/") or dev.startswith("/dev/loop") or mnt in seen:
                    continue
                seen.add(mnt)
                out.append((mnt, dev, fstype))
    except OSError:
        pass
    return sorted(out)


# ------------------------------------------------------------ list items ----

class Entry(GObject.Object):
    """A row: either a subdirectory (`node`) or a file tuple in `parent`."""

    def __init__(self, parent, node=None, file=None, label=None):
        super().__init__()
        self.parent_dir = parent
        self.node = node
        self.file = file
        if node is not None:
            self.fname, self.size, self.mtime, self.count = node.name, node.size, node.mtime, node.count
        else:
            self.fname, self.size, self.mtime = file
            self.count = None
        self.label = label or self.fname

    @property
    def path(self):
        return os.path.join(node_path(self.parent_dir), self.fname)


def _order(a, b):
    return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL


CSS = b"""
.numeric { font-feature-settings: "tnum"; }
progressbar.usage trough, progressbar.usage progress { min-height: 8px; }
.crumb { padding: 2px 6px; }
levelbar trough block.filled.ok { background: none; background-color: #3584e4; border-color: #3584e4; }
levelbar trough block.filled.warn { background: none; background-color: #e5a50a; border-color: #e5a50a; }
levelbar trough block.filled.crit { background: none; background-color: #e01b24; border-color: #e01b24; }
"""


# --------------------------------------------------------------- window ----

class Window(Gtk.ApplicationWindow):
    def __init__(self, app, start_path):
        super().__init__(application=app, title="Free Space")
        self.set_default_size(1150, 720)
        self.root = None
        self.current = None
        self.history = []
        self.cancel = threading.Event()
        self.scan_token = None
        self.busy = False

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        open_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Scan a folder…")
        open_btn.connect("clicked", self.on_choose_folder)
        header.pack_start(open_btn)
        self.rescan_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan")
        self.rescan_btn.connect("clicked", lambda *_: self.root and self.start_scan(node_path(self.root)))
        header.pack_start(self.rescan_btn)

        paned = Gtk.Paned(position=250, shrink_start_child=False)
        self.set_child(paned)
        paned.set_start_child(self.build_sidebar())

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        paned.set_end_child(main)

        nav = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back (Alt+Left)")
        self.back_btn.connect("clicked", lambda *_: self.go_back())
        self.up_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Parent folder (Backspace)")
        self.up_btn.connect("clicked", lambda *_: self.go_up())
        nav.append(self.back_btn)
        nav.append(self.up_btn)
        crumb_scroll = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER)
        self.crumbs = Gtk.Box(spacing=0)
        crumb_scroll.set_child(self.crumbs)
        nav.append(crumb_scroll)
        self.largest_btn = Gtk.ToggleButton(label="Largest files",
                                            tooltip_text="Show the biggest files anywhere below this folder")
        self.largest_btn.connect("toggled", lambda *_: self.populate())
        nav.append(self.largest_btn)
        main.append(nav)

        self.store = Gio.ListStore(item_type=Entry)
        self.view = Gtk.ColumnView(show_row_separators=False, show_column_separators=False, reorderable=False)
        self.sorted = Gtk.SortListModel(model=self.store, sorter=self.view.get_sorter())
        self.selection = Gtk.MultiSelection(model=self.sorted)
        self.selection.connect("selection-changed", lambda *_: self.update_selection())
        self.view.set_model(self.selection)
        self.view.connect("activate", self.on_activate)
        self.build_columns()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.view)
        main.append(scroller)

        self.progress = Gtk.ProgressBar(show_text=True, visible=False, margin_start=6, margin_end=6)
        self.progress.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        main.append(self.progress)

        bar = Gtk.ActionBar()
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.status.add_css_class("numeric")
        bar.pack_start(self.status)
        self.cancel_btn = Gtk.Button(label="Cancel scan", visible=False)
        self.cancel_btn.connect("clicked", lambda *_: self.cancel.set())
        bar.pack_end(self.cancel_btn)
        self.delete_btn = Gtk.Button(label="Delete Permanently", tooltip_text="Shift+Delete")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", lambda *_: self.request_remove(True))
        bar.pack_end(self.delete_btn)
        self.trash_btn = Gtk.Button(label="Move to Trash", tooltip_text="Delete")
        self.trash_btn.connect("clicked", lambda *_: self.request_remove(False))
        bar.pack_end(self.trash_btn)
        self.fs_label = Gtk.Label(xalign=1)
        self.fs_label.add_css_class("dim-label")
        self.fs_label.add_css_class("numeric")
        bar.pack_end(self.fs_label)
        main.append(bar)

        self.build_actions()
        self.update_selection()
        self.update_nav()
        self.start_scan(start_path)

    # ---- construction ----

    def build_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("navigation-sidebar")
        lb.connect("row-activated", lambda _lb, row: self.start_scan(row.path))

        def add(path, title, icon, show_usage):
            row = Gtk.ListBoxRow()
            row.path = path
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin_top=4, margin_bottom=4)
            h = Gtk.Box(spacing=8)
            h.append(Gtk.Image(icon_name=icon))
            h.append(Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE))
            v.append(h)
            if show_usage:
                try:
                    du = shutil.disk_usage(path)
                    level = Gtk.LevelBar(min_value=0, max_value=1, value=du.used / du.total if du.total else 0)
                    for name in ("low", "high", "full"):
                        level.remove_offset_value(name)
                    level.add_offset_value("ok", 0.8)
                    level.add_offset_value("warn", 0.9)
                    level.add_offset_value("crit", 1.0)
                    v.append(level)
                    sub = Gtk.Label(label=f"{fmt_size(du.free)} free of {fmt_size(du.total)}", xalign=0)
                    sub.add_css_class("dim-label")
                    sub.add_css_class("caption")
                    v.append(sub)
                except OSError:
                    pass
            row.set_child(v)
            row.set_tooltip_text(path)
            lb.append(row)

        home = os.path.expanduser("~")
        add(home, "Home", "user-home-symbolic", False)
        trash = os.path.join(GLib.get_user_data_dir(), "Trash")
        if os.path.isdir(trash):
            add(trash, "Trash", "user-trash-symbolic", False)
        for mnt, dev, fstype in mounts():
            add(mnt, mnt, "drive-harddisk-symbolic", True)

        title = Gtk.Label(label="Locations", xalign=0, margin_start=12, margin_top=10, margin_bottom=4)
        title.add_css_class("heading")
        box.append(title)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(lb)
        box.append(sw)
        return box

    def build_columns(self):
        def col(title, setup, bind, key, expand=False, width=None):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", lambda _f, li: li.set_child(self.cell(setup())))

            def on_bind(_f, li):
                w = li.get_child()
                w.entry = li.get_item()
                bind(w.get_first_child(), w.entry)
            f.connect("bind", on_bind)
            c = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=True)
            if width:
                c.set_fixed_width(width)
            c.set_sorter(Gtk.CustomSorter.new(lambda a, b, _: _order(key(a), key(b)), None))
            self.view.append_column(c)
            return c

        def name_setup():
            b = Gtk.Box(spacing=8)
            b.append(Gtk.Image())
            b.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True))
            return b

        def name_bind(b, e):
            img, lbl = b.get_first_child(), b.get_last_child()
            if e.node is not None:
                icon = ("drive-harddisk-symbolic" if e.node.flags & MOUNTPOINT
                        else "dialog-warning-symbolic" if e.node.flags & UNREADABLE else "folder")
                img.set_from_icon_name(icon)
                note = (" (other filesystem)" if e.node.flags & MOUNTPOINT
                        else " (no permission)" if e.node.flags & UNREADABLE else "")
                lbl.set_text(e.label + note)
            else:
                ctype, _ = Gio.content_type_guess(e.fname, None)
                img.set_from_gicon(Gio.content_type_get_icon(ctype))
                lbl.set_text(e.label)
            lbl.set_tooltip_text(e.path)

        def usage_setup():
            b = Gtk.Box(spacing=6)
            p = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True)
            p.add_css_class("usage")
            b.append(p)
            l = Gtk.Label(xalign=1, width_chars=6)
            l.add_css_class("numeric")
            l.add_css_class("dim-label")
            b.append(l)
            return b

        def usage_bind(b, e):
            total = self.current.size if self.current else 0
            frac = e.size / total if total else 0
            b.get_first_child().set_fraction(frac)
            b.get_last_child().set_text(f"{frac * 100:.1f}%")

        def label_setup():
            l = Gtk.Label(xalign=1)
            l.add_css_class("numeric")
            return l

        name_col = col("Name", name_setup, name_bind, lambda e: e.label.casefold(), expand=True)
        col("Share of folder", usage_setup, usage_bind, lambda e: e.size, width=190)
        size_col = col("Size", label_setup, lambda l, e: l.set_text(fmt_size(e.size)), lambda e: e.size, width=100)
        col("Items", label_setup, lambda l, e: l.set_text("" if e.count is None else f"{e.count:,}"),
            lambda e: -1 if e.count is None else e.count, width=90)
        col("Modified", label_setup, lambda l, e: l.set_text(time.strftime("%Y-%m-%d", time.localtime(e.mtime))),
            lambda e: e.mtime, width=110)
        name_col.set_resizable(True)
        self.view.sort_by_column(size_col, Gtk.SortType.DESCENDING)

        menu = Gio.Menu()
        menu.append("Open", "win.open")
        menu.append("Show in File Manager", "win.reveal")
        menu.append("Copy Path", "win.copy-path")
        danger = Gio.Menu()
        danger.append("Move to Trash", "win.trash")
        danger.append("Delete Permanently", "win.delete")
        menu.append_section(None, danger)
        self.menu = Gtk.PopoverMenu.new_from_model(menu)
        self.menu.set_has_arrow(False)
        self.menu.set_halign(Gtk.Align.START)
        self.menu.set_parent(self.view)
        self.connect("close-request", lambda *_: self.menu.unparent() or False)

    def cell(self, child):
        """Wrap a cell's content so a right-click selects the row and opens the menu."""
        wrap = Gtk.Box()
        child.set_hexpand(True)
        wrap.append(child)
        wrap.entry = None
        g = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        g.connect("pressed", self.on_right_click, wrap)
        wrap.add_controller(g)
        return wrap

    def build_actions(self):
        self.actions = {}
        for name, cb in (("open", lambda *_: self.open_selected()),
                         ("reveal", lambda *_: self.reveal_selected()),
                         ("copy-path", lambda *_: self.copy_paths()),
                         ("trash", lambda *_: self.request_remove(False)),
                         ("delete", lambda *_: self.request_remove(True))):
            a = Gio.SimpleAction(name=name)
            a.connect("activate", cb)
            self.add_action(a)
            self.actions[name] = a
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

    # ---- scanning ----

    def start_scan(self, path):
        self.cancel.set()  # stop any scan already running
        cancel = self.cancel = threading.Event()
        token = self.scan_token = object()
        self.busy = True
        self.progress.set_visible(True)
        self.progress.set_text(f"Scanning {path}…")
        self.cancel_btn.set_visible(True)
        self.update_selection()
        self._pulse = GLib.timeout_add(120, lambda: self.progress.pulse() or self.scan_token is token)

        def progress(nfiles, ndirs, p):
            GLib.idle_add(self.on_scan_progress, token, nfiles, ndirs, p)

        def work():
            try:
                root, errors = scan(path, cancel, progress)
                err = None
            except OSError as e:
                root, errors, err = None, 0, e.strerror or str(e)
            GLib.idle_add(self.on_scan_done, token, path, root, errors, err)

        threading.Thread(target=work, daemon=True).start()

    def on_scan_progress(self, token, nfiles, ndirs, p):
        if token is self.scan_token:
            self.progress.set_text(f"{nfiles:,} files in {ndirs:,} folders — {p}")
        return False

    def on_scan_done(self, token, path, root, errors, err):
        if token is not self.scan_token:
            return False
        self.scan_token = None
        self.busy = False
        self.progress.set_visible(False)
        self.cancel_btn.set_visible(False)
        if root is None:
            self.status.set_text(f"Could not scan {path}: {err}" if err else "Scan cancelled.")
            self.update_selection()
            return False
        self.root = root
        self.scan_errors = errors
        self.history = []
        self.current = None
        self.navigate(root)
        return False

    # ---- navigation ----

    def navigate(self, node, remember=True):
        if remember and self.current is not None and node is not self.current:
            self.history.append(self.current)
        self.current = node
        self.populate()

    def go_back(self):
        if self.history:
            self.navigate(self.history.pop(), remember=False)

    def go_up(self):
        if self.current is not None and self.current.parent is not None:
            self.navigate(self.current.parent)

    def on_activate(self, _view, pos):
        e = self.sorted.get_item(pos)
        if e.node is not None and not e.node.flags & (MOUNTPOINT | UNREADABLE):
            self.navigate(e.node)
        elif e.node is not None and e.node.flags & MOUNTPOINT:
            self.start_scan(e.path)
        else:
            self.open_selected()

    def populate(self):
        node = self.current
        if node is None:
            self.store.remove_all()
            return
        if self.largest_btn.get_active():
            base = node_path(node)
            top = heapq.nlargest(LARGEST_LIMIT, iter_files(node), key=lambda df: df[1][1])
            items = [Entry(d, file=f, label=os.path.relpath(os.path.join(node_path(d), f[0]), base))
                     for d, f in top]
        else:
            items = [Entry(node, node=d) for d in node.dirs] + [Entry(node, file=f) for f in node.files]
        self.store.splice(0, self.store.get_n_items(), items)
        if items:
            self.view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None)
        self.update_nav()
        self.update_selection()

    def update_nav(self):
        while (c := self.crumbs.get_first_child()) is not None:
            self.crumbs.remove(c)
        chain, n = [], self.current
        while n is not None:
            chain.append(n)
            n = n.parent
        for i, n in enumerate(reversed(chain)):
            if i:
                sep = Gtk.Label(label="›")
                sep.add_css_class("dim-label")
                self.crumbs.append(sep)
            b = Gtk.Button(label=n.name)
            b.add_css_class("flat")
            b.add_css_class("crumb")
            if n is self.current:
                b.add_css_class("heading")
            b.connect("clicked", lambda _b, n=n: self.navigate(n))
            self.crumbs.append(b)
        self.back_btn.set_sensitive(bool(self.history))
        self.up_btn.set_sensitive(self.current is not None and self.current.parent is not None)
        if self.root is not None:
            try:
                du = shutil.disk_usage(node_path(self.root))
                self.fs_label.set_text(f"Disk: {fmt_size(du.used)} used · {fmt_size(du.free)} free")
            except OSError:
                self.fs_label.set_text("")

    # ---- selection & actions ----

    def selected_entries(self):
        bits = self.selection.get_selection()
        return [self.sorted.get_item(bits.get_nth(i)) for i in range(bits.get_size())]

    def update_selection(self):
        sel = self.selected_entries()
        removable = [e for e in sel if not (e.node is not None and e.node.flags & MOUNTPOINT)]
        can_remove = bool(removable) and not self.busy
        for name in ("trash", "delete"):
            self.actions[name].set_enabled(can_remove)
        for name in ("open", "reveal", "copy-path"):
            self.actions[name].set_enabled(bool(sel))
        self.trash_btn.set_sensitive(can_remove)
        self.delete_btn.set_sensitive(can_remove)
        if self.busy or self.current is None:
            return
        cur = self.current
        text = f"{fmt_size(cur.size)} in {cur.count:,} items"
        if self.largest_btn.get_active():
            text = f"Largest {self.store.get_n_items()} files below this folder · " + text
        if sel:
            text = f"{len(sel)} selected ({fmt_size(sum(e.size for e in sel))}) · " + text
        if getattr(self, "scan_errors", 0):
            text += f" · {self.scan_errors:,} unreadable entries"
        self.status.set_text(text)

    def on_right_click(self, gesture, _n, x, y, wrap):
        e = wrap.entry
        if e is None:
            return
        if e not in self.selected_entries():
            for i in range(self.sorted.get_n_items()):
                if self.sorted.get_item(i) is e:
                    self.selection.select_item(i, True)
                    break
        ok, pt = wrap.compute_point(self.view, Graphene.Point().init(x, y))
        if ok:
            self.menu.set_pointing_to(Gdk.Rectangle(x=int(pt.x), y=int(pt.y), width=1, height=1))
            self.menu.popup()

    def on_key(self, _ctl, keyval, _code, state):
        shift = state & Gdk.ModifierType.SHIFT_MASK
        alt = state & Gdk.ModifierType.ALT_MASK
        if keyval == Gdk.KEY_Delete:
            self.request_remove(bool(shift))
        elif keyval == Gdk.KEY_BackSpace or (alt and keyval == Gdk.KEY_Up):
            self.go_up()
        elif alt and keyval == Gdk.KEY_Left:
            self.go_back()
        else:
            return False
        return True

    def open_selected(self):
        for e in self.selected_entries()[:10]:
            Gtk.FileLauncher(file=Gio.File.new_for_path(e.path)).launch(self, None, None, None)

    def reveal_selected(self):
        for e in self.selected_entries()[:10]:
            Gtk.FileLauncher(file=Gio.File.new_for_path(e.path)).open_containing_folder(self, None, None, None)

    def copy_paths(self):
        self.get_clipboard().set("\n".join(e.path for e in self.selected_entries()))

    def on_choose_folder(self, _btn):
        dlg = Gtk.FileDialog(title="Choose a folder to scan")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                self.start_scan(f.get_path())
        dlg.select_folder(self, None, done)

    # ---- removal ----

    def request_remove(self, permanent):
        if self.busy:
            return
        entries = [e for e in self.selected_entries()
                   if not (e.node is not None and e.node.flags & MOUNTPOINT)]
        if not entries:
            return
        total = fmt_size(sum(e.size for e in entries))
        what = f"“{entries[0].label}”" if len(entries) == 1 else f"{len(entries)} items"
        listing = "\n".join(e.path for e in entries[:8]) + (f"\n…and {len(entries) - 8} more" if len(entries) > 8 else "")
        if permanent:
            heading = f"Permanently delete {what} ({total})?"
            detail = f"{listing}\n\nThis cannot be undone."
            verb = "Delete Permanently"
        else:
            heading = f"Move {what} ({total}) to the Trash?"
            detail = f"{listing}\n\nNote: space on this disk is not freed until the Trash is emptied."
            verb = "Move to Trash"
        dlg = Gtk.AlertDialog(message=heading, detail=detail, buttons=["Cancel", verb],
                              cancel_button=0, default_button=0 if permanent else 1, modal=True)

        def done(d, res):
            try:
                if d.choose_finish(res) == 1:
                    self.do_remove(entries, permanent)
            except GLib.Error:
                pass
        dlg.choose(self, None, done)

    def do_remove(self, entries, permanent):
        self.busy = True
        self.update_selection()
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        root = self.root

        def work():
            results = []
            for i, e in enumerate(entries):
                p = e.path
                GLib.idle_add(lambda i=i, p=p: self.progress.set_text(f"Removing {p}")
                              or self.progress.set_fraction(i / len(entries)) or False)
                errs = []
                try:
                    if not permanent:
                        Gio.File.new_for_path(p).trash(None)
                    elif e.node is not None:
                        shutil.rmtree(p, onexc=lambda _f, path, exc: errs.append(
                            f"{path}: {getattr(exc, 'strerror', None) or exc}"))
                    else:
                        os.unlink(p)
                except GLib.Error as ex:
                    errs.append(f"{p}: {ex.message}")
                except OSError as ex:
                    errs.append(f"{p}: {ex.strerror or ex}")
                gone = not os.path.lexists(p)
                rescanned = None
                if not gone and e.node is not None and permanent:
                    try:  # partially deleted: re-measure what is left
                        rescanned = scan(p)[0]
                    except OSError:
                        pass
                results.append((e, gone, rescanned, errs))
            GLib.idle_add(self.on_removed, root, results, permanent)

        threading.Thread(target=work, daemon=True).start()

    def on_removed(self, root, results, permanent):
        self.busy = False
        self.progress.set_visible(False)
        freed = 0
        errors = []
        for e, gone, rescanned, errs in results:
            errors.extend(errs)
            if root is not self.root:
                continue  # tree was replaced by a rescan meanwhile
            if gone:
                freed += e.size
                if e.node is not None:
                    detach_dir(e.node)
                else:
                    detach_file(e.parent_dir, e.file)
            elif rescanned is not None:
                freed += e.node.size - rescanned.size
                replace_dir(e.node, rescanned)
        self.populate()
        done = sum(1 for r in results if r[1])
        msg = (f"Deleted {done} item(s), freed {fmt_size(freed)}." if permanent
               else f"Moved {done} item(s) ({fmt_size(freed)}) to the Trash.")
        self.status.set_text(msg + (f" {len(errors)} error(s)." if errors else ""))
        if errors:
            hint = "" if permanent else "\n\nIf this filesystem has no Trash, use Delete Permanently instead."
            Gtk.AlertDialog(message="Some items could not be removed",
                            detail="\n".join(errors[:12]) + ("\n…" if len(errors) > 12 else "") + hint
                            ).show(self)
        return False


class App(Gtk.Application):
    def __init__(self, start_path):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.start_path = start_path

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        self.window = Window(self, self.start_path)
        self.window.present()


def main():
    path = os.path.abspath(os.path.expanduser(sys.argv[1])) if len(sys.argv) > 1 else os.path.expanduser("~")
    return App(path).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
