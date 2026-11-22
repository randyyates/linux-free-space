"""Drive the real window: scan, navigate, delete permanently, trash. Needs a display."""
import os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import freespace as fs
from gi.repository import GLib

def main():
    global tmp, du, app, steps, failures
    HERE = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp(prefix="fs-gui-", dir=HERE)  # on the home fs, so Trash works
    os.makedirs(f"{tmp}/big/inner")
    for p, n in [("big/inner/huge.bin", 3_000_000), ("big/med.bin", 1_000_000), ("small.txt", 100),
                 ("trashme.bin", 500_000)]:
        with open(f"{tmp}/{p}", "wb") as f:
            f.write(os.urandom(n))

    du = lambda: int(subprocess.check_output(["du", "-sx", "-B1", tmp]).split()[0])
    app = fs.App(tmp)
    steps, failures = [], []


    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        cond or failures.append(msg)


    def wait_idle(fn):
        def tick():
            w = app.window
            if w.busy:
                return True
            fn(w)
            GLib.timeout_add(300, run_next)
            return False
        GLib.timeout_add(100, tick)


    def labels(w):
        return [w.sorted.get_item(i).label for i in range(w.sorted.get_n_items())]


    def select(w, label):
        i = labels(w).index(label)
        w.selection.select_item(i, True)
        return w.sorted.get_item(i)


    def s_scanned(w):
        check(w.root is not None and w.root.size == du(), f"scan total matches du ({w.root.size if w.root else None})")
        check(labels(w)[0] == "big", f"sorted by size, biggest first: {labels(w)}")
        w.on_activate(None, 0)
        check(w.current.name == "big", "double-click enters folder")
        w.go_up()
        root = w.root
        w.open_location(tmp)
        check(not w.scanning and w.current is root, "re-opening a scanned location does not rescan")
        w.open_location(f"{tmp}/big")
        check(not w.scanning and w.current.name == "big" and w.root is root,
              "opening a folder inside a scanned tree reuses the scan")
        w.navigate(w.current.dirs[0])
        w.open_location(tmp)
        w.open_location(f"{tmp}/big")
        check(w.current.name == "inner", "location resumes at the folder you left it on")
        with open(f"{tmp}/big/new.bin", "wb") as f:
            f.write(os.urandom(200_000))
        w.open_location(f"{tmp}/big", rescan=True)
        check(w.scanning, "explicit Rescan does scan")


    def s_rescanned(w):
        check(len(w.cache) == 1 and w.root.size == du(), f"subfolder rescan merged into the tree ({w.root.size} vs {du()})")
        check(w.current.name == "big" and "new.bin" in labels(w), f"rescan shows new files: {labels(w)}")
        with open(f"{tmp}/big/inner/quick.bin", "wb") as f:
            f.write(os.urandom(300_000))
        w.navigate(w.lookup(f"{tmp}/big"))
        w.quick_refresh()
        check(w.scanning, "quick refresh runs")

    def s_quick(w):
        check(w.root.size == du(), f"quick refresh finds a file added deep down ({w.root.size} vs {du()})")
        check(w.current.name == "big" and "re-read" in w.status.get_text(), f"status: {w.status.get_text()}")
        w.open_location(tmp)
        w.largest_btn.set_active(True)
        check(labels(w)[0] == os.path.join("big", "inner", "huge.bin"), f"largest-files view: {labels(w)[:2]}")
        w.do_remove([select(w, os.path.join("big", "inner", "huge.bin"))], True)


    def s_deleted(w):
        check(not os.path.exists(f"{tmp}/big/inner/huge.bin"), "file deleted from disk")
        check(w.root.size == du(), f"tree total updated after delete ({w.root.size} vs {du()})")
        w.largest_btn.set_active(False)
        w.do_remove([select(w, "big")], True)


    def s_dir_deleted(w):
        check(not os.path.exists(f"{tmp}/big"), "folder deleted recursively")
        check("big" not in labels(w) and w.root.size == du(), "folder gone from view, totals match")
        w.do_remove([select(w, "trashme.bin")], False)


    def s_trashed(w):
        check(not os.path.exists(f"{tmp}/trashme.bin"), "file moved to Trash")
        check("trashme.bin" not in labels(w), "trashed file removed from view")
        check("Trash" in w.status.get_text(), f"status: {w.status.get_text()}")
        # remove exactly our trashed test file from the real Trash
        tdir = os.path.join(GLib.get_user_data_dir(), "Trash")
        for info in os.listdir(f"{tdir}/info"):
            if info.startswith("trashme") and tmp in open(f"{tdir}/info/{info}").read():
                os.unlink(f"{tdir}/info/{info}")
                os.unlink(f"{tdir}/files/{info[:-len('.trashinfo')]}")
        w.close()


    steps = [s_scanned, s_rescanned, s_quick, s_deleted, s_dir_deleted, s_trashed]


    def run_next():
        if steps:
            wait_idle(steps.pop(0))
        return False


    app.connect("activate", lambda *_: GLib.timeout_add(300, run_next))
    app.run([sys.argv[0]])
    shutil.rmtree(tmp)
    print("FAILED" if failures else "ALL PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":  # worker processes re-import this script
    main()
