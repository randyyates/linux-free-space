"""Scanner/tree tests: totals must match `du -sx -B1`, and stay consistent after removals."""
import os, subprocess, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import freespace as fs


def du(path):
    return int(subprocess.check_output(["du", "-sx", "-B1", path]).split()[0])


def build(root):
    os.makedirs(f"{root}/a/b/c")
    os.makedirs(f"{root}/d")
    for i, (p, n) in enumerate([("a/x.bin", 300_000), ("a/b/y.bin", 50_000), ("a/b/c/z.bin", 1_234_567),
                                ("d/w.txt", 10), ("top.bin", 700_000)]):
        with open(f"{root}/{p}", "wb") as f:
            f.write(os.urandom(n))
    os.link(f"{root}/top.bin", f"{root}/d/hardlink.bin")      # counted once
    os.symlink(f"{root}/a", f"{root}/d/link-to-a")             # not followed
    with open(f"{root}/sparse.img", "wb") as f:                # sparse: tiny on disk
        f.truncate(1 << 30)


def walk_check(node):
    """Each dir's size/count equals its own contents plus children."""
    st = os.lstat(fs.node_path(node))
    size = st.st_blocks * 512 + sum(f[1] for f in node.files) + sum(walk_check(d) or d.size for d in node.dirs)
    assert node.size == size, (fs.node_path(node), node.size, size)
    assert node.count == len(node.files) + sum(d.count + 1 for d in node.dirs)


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build(self.tmp)

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def test_scan_matches_du(self):
        root, errors = fs.scan(self.tmp)
        self.assertEqual(errors, 0)
        self.assertEqual(root.size, du(self.tmp))
        self.assertEqual(root.count, 12)  # 4 dirs, 7 files, 1 symlink
        walk_check(root)
        sparse = [f for f in root.files if f[0] == "sparse.img"][0]
        self.assertLess(sparse[1], 1 << 20)

    def test_detach_and_replace(self):
        root, _ = fs.scan(self.tmp)
        a = [d for d in root.dirs if d.name == "a"][0]
        subprocess.run(["rm", "-rf", f"{self.tmp}/a/b"])
        b = a.dirs[0]
        fs.detach_dir(b)
        f = [f for f in root.files if f[0] == "sparse.img"][0]
        os.unlink(f"{self.tmp}/sparse.img")
        fs.detach_file(root, f)
        self.assertEqual(root.size, du(self.tmp))
        walk_check(root)
        os.unlink(f"{self.tmp}/a/x.bin")
        fs.replace_dir(a, fs.scan(f"{self.tmp}/a")[0])
        self.assertEqual(root.size, du(self.tmp))
        walk_check(root)

    def test_unreadable_dir(self):
        os.makedirs(f"{self.tmp}/locked/inner")
        os.chmod(f"{self.tmp}/locked", 0)
        try:
            root, errors = fs.scan(self.tmp)
            locked = [d for d in root.dirs if d.name == "locked"][0]
            self.assertTrue(locked.flags & fs.UNREADABLE)
            self.assertEqual(errors, 1)
        finally:
            os.chmod(f"{self.tmp}/locked", 0o755)

    def tree_scan(self, path):
        job = fs.TreeScan(path)
        while not job.apply(1):
            time.sleep(0.01)
        return job

    def test_parallel_scan_matches_sequential(self):
        os.makedirs(f"{self.tmp}/locked/inner")
        os.chmod(f"{self.tmp}/locked", 0)
        for i in range(30):  # enough folders to be split across workers
            os.makedirs(f"{self.tmp}/many/{i}/deep")
            with open(f"{self.tmp}/many/{i}/deep/f", "wb") as f:
                f.write(os.urandom(5000 + i))
        try:
            fs.CHUNK, old = 3, fs.CHUNK
            job = self.tree_scan(self.tmp)
            fs.CHUNK = old
            seq, errors = fs.scan(self.tmp)
            self.assertEqual(job.root.size, seq.size)
            self.assertEqual(job.root.count, seq.count)
            self.assertEqual(job.errors, errors)
            self.assertEqual(job.root.pending, 0)
            walk_check(job.root)
        finally:
            os.chmod(f"{self.tmp}/locked", 0o755)
        self.assertEqual(job.root.size, du(self.tmp))

    def test_parallel_scan_partial_tree_is_consistent(self):
        job = fs.TreeScan(self.tmp)
        fs.CHUNK, old = 2, fs.CHUNK
        try:
            while not job.apply(0):  # one batch at a time; totals add up at every step
                walk_check(job.root)
                self.assertEqual(job.root.pending, len(job.waiting))
                time.sleep(0.005)
        finally:
            fs.CHUNK = old
        walk_check(job.root)
        self.assertEqual(job.root.size, du(self.tmp))

    def test_quick_refresh(self):
        old, _ = fs.scan(self.tmp)
        time.sleep(0.01)
        with open(f"{self.tmp}/a/b/c/new.bin", "wb") as f:  # added deep down
            f.write(os.urandom(400_000))
        subprocess.run(["rm", "-rf", f"{self.tmp}/d"])
        stats = {}
        new, errors = fs.scan(self.tmp, prev=old, stats=stats)
        self.assertEqual(new.size, du(self.tmp))
        walk_check(new)
        self.assertEqual(stats["reread"], 2)  # the top (d removed) and a/b/c (file added)
        # documented limit: a file growing in place in an unchanged folder is not seen
        with open(f"{self.tmp}/a/x.bin", "ab") as f:
            f.write(os.urandom(100_000))
        os.utime(f"{self.tmp}/a", (old.mtime, [d for d in old.dirs if d.name == "a"][0].mtime))
        again, _ = fs.scan(self.tmp, prev=new)
        self.assertEqual(again.size, new.size)
        self.assertEqual(fs.scan(self.tmp)[0].size, du(self.tmp))

    def test_cancel(self):
        import threading
        ev = threading.Event(); ev.set()
        self.assertIsNone(fs.scan(self.tmp, ev)[0])


if __name__ == "__main__":
    unittest.main()
