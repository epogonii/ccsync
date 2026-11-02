#!/usr/bin/env python3
# End-to-end runs between two fake home folders on this machine: the peer is a
# "dir" peer, so no ssh is needed. Run: python3 -m unittest discover -s test
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "scripts", "ccsync.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ccsync_side as side  # noqa: E402


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def line(cwd, n):
    return json.dumps({"uuid": "u%d" % n, "cwd": cwd, "message": "m%d" % n},
                      separators=(",", ":")) + "\n"


class Pair(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccsync-test-"))
        self.a = os.path.join(self.tmp, "alice")
        self.b = os.path.join(self.tmp, "bob")
        self.pa = os.path.join(self.a, "work", "app")
        self.pb = os.path.join(self.b, "work", "app")
        self.ca = os.path.join(self.a, ".claude")
        self.cb = os.path.join(self.b, ".claude")
        self.na = side.encode(self.pa)
        self.nb = side.encode(self.pb)
        os.makedirs(self.b)
        self.config({})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, extra, peer=None):
        cfg = {"peers": {"bob": dict({"dir": self.b}, **(peer or {}))}}
        cfg.update(extra)
        write(os.path.join(self.ca, "ccsync", "config.json"), json.dumps(cfg))

    def run_cli(self, *args, ok=True):
        env = dict(os.environ, HOME=self.a)
        env.pop("CLAUDE_CONFIG_DIR", None)
        p = subprocess.run([sys.executable, "-I", CLI] + list(args), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode()
        if ok:
            self.assertEqual(p.returncode, 0, out)
        return out

    def sync(self, *args):
        return self.run_cli("run", "bob", *args)

    def base(self):
        sdir = os.path.join(self.ca, "ccsync", "state")
        (f,) = os.listdir(sdir)
        with open(os.path.join(sdir, f), encoding="utf-8") as fh:
            return json.load(fh)["base"]

    def ta(self, *p):
        return os.path.join(self.ca, "projects", self.na, *p)

    def tb(self, *p):
        return os.path.join(self.cb, "projects", self.nb, *p)

    def test_transcripts_memory_history(self):
        write(self.ta("s1.jsonl"), line(self.pa, 1) + line(self.pa, 2))
        write(self.tb("memory", "MEMORY.md"), "- [Bob note](bob.md) - b\n")
        write(self.tb("memory", "bob.md"), "bob knows\n")
        write(self.ta("memory", "MEMORY.md"), "- [Alice note](alice.md) - a\n")
        write(self.ta("memory", "alice.md"), "alice knows\n")
        write(os.path.join(self.ca, "history.jsonl"),
              json.dumps({"display": "a", "timestamp": 1, "project": self.pa}) + "\n")
        write(os.path.join(self.cb, "history.jsonl"),
              json.dumps({"display": "b", "timestamp": 2, "project": self.pb}) + "\n")
        out = self.sync()
        self.assertIn("from bob", out)
        # the transcript lands under bob's own project folder, with his paths
        self.assertEqual(read(self.tb("s1.jsonl")), line(self.pb, 1) + line(self.pb, 2))
        self.assertEqual(read(self.ta("memory", "bob.md")), "bob knows\n")
        self.assertEqual(read(self.tb("memory", "alice.md")), "alice knows\n")
        for idx in (self.ta("memory", "MEMORY.md"), self.tb("memory", "MEMORY.md")):
            self.assertIn("(bob.md)", read(idx))
            self.assertIn("(alice.md)", read(idx))
        ha = read(os.path.join(self.ca, "history.jsonl")).splitlines()
        hb = read(os.path.join(self.cb, "history.jsonl")).splitlines()
        self.assertEqual([json.loads(x)["project"] for x in ha], [self.pa, self.pa])
        self.assertEqual([json.loads(x)["project"] for x in hb], [self.pb, self.pb])

        # a session that grew on one side gets the tail appended on the other
        with open(self.ta("s1.jsonl"), "a", encoding="utf-8") as f:
            f.write(line(self.pa, 3))
        out = self.sync()
        self.assertIn("1 appended", out)
        self.assertEqual(read(self.tb("s1.jsonl")).count("\n"), 3)

        # continued on both machines: each gets the lines it lacks
        with open(self.ta("s1.jsonl"), "a", encoding="utf-8") as f:
            f.write(line(self.pa, 4))
        with open(self.tb("s1.jsonl"), "a", encoding="utf-8") as f:
            f.write(line(self.pb, 5))
        self.sync()
        for t, p in ((self.ta("s1.jsonl"), self.pa), (self.tb("s1.jsonl"), self.pb)):
            got = read(t)
            self.assertIn(line(p, 4), got)
            self.assertIn(line(p, 5), got)
        out = self.sync()
        self.assertIn("from bob: nothing new", out)
        self.assertIn("to bob: nothing new", out)

    def test_memory_replace_conflict_delete(self):
        write(self.ta("memory", "MEMORY.md"), "- [One](one.md)\n- [Two](two.md)\n- [Three](three.md)\n")
        for n in ("one", "two", "three"):
            write(self.ta("memory", n + ".md"), n + "\n")
        self.sync()
        base = self.base()
        # edited here only: replaces bob's copy, the old one is kept aside
        write(self.ta("memory", "one.md"), "one, edited\n", mtime=base + 5)
        # edited on both: a conflict, nobody wins
        write(self.ta("memory", "two.md"), "two by alice\n", mtime=base + 5)
        write(self.tb("memory", "two.md"), "two by bob\n", mtime=base + 6)
        out = self.sync()
        self.assertEqual(read(self.tb("memory", "one.md")), "one, edited\n")
        self.assertIn("conflicts", out)
        self.assertEqual(read(self.ta("memory", "two.md")), "two by alice\n")
        self.assertEqual(read(self.tb("memory", "two.md")), "two by bob\n")
        saved = os.path.join(self.cb, "ccsync", "replaced")
        found = [os.path.join(d, f) for d, _, fs in os.walk(saved) for f in fs if f == "one.md"]
        self.assertTrue(found)
        self.assertEqual(read(found[0]), "one\n")

        # deleted here: goes away over there, index line too
        os.remove(self.ta("memory", "three.md"))
        out = self.sync()
        self.assertFalse(os.path.exists(self.tb("memory", "three.md")))
        self.assertNotIn("three.md", read(self.tb("memory", "MEMORY.md")))
        self.assertIn("deleted on bob", out)

        # deleted here but edited there after the last sync: it stays and comes back
        base = self.base()
        write(self.tb("memory", "one.md"), "one, bob's\n", mtime=base + 5)
        os.remove(self.ta("memory", "one.md"))
        out = self.sync()
        self.assertIn("kept", out)
        self.assertEqual(read(self.ta("memory", "one.md")), "one, bob's\n")

    def test_files_newer_wins_and_deletions(self):
        self.config({"files": {
            "scripts": {"path": "scripts", "exclude": ["__pycache__"], "delete": True},
            "rules": {"file": "CLAUDE.md"},
        }})
        write(os.path.join(self.ca, "scripts", "a.sh"), "a1\n")
        write(os.path.join(self.ca, "scripts", "__pycache__", "x.pyc"), "junk\n")
        write(os.path.join(self.cb, "scripts", "tools", "b.py"), "b1\n")
        write(os.path.join(self.cb, "CLAUDE.md"), "be brief\n")
        os.chmod(os.path.join(self.ca, "scripts", "a.sh"), 0o755)
        self.sync()
        self.assertEqual(read(os.path.join(self.cb, "scripts", "a.sh")), "a1\n")
        self.assertTrue(os.access(os.path.join(self.cb, "scripts", "a.sh"), os.X_OK))
        self.assertEqual(read(os.path.join(self.ca, "scripts", "tools", "b.py")), "b1\n")
        self.assertEqual(read(os.path.join(self.ca, "CLAUDE.md")), "be brief\n")
        self.assertFalse(os.path.exists(os.path.join(self.cb, "scripts", "__pycache__")))

        base = self.base()
        write(os.path.join(self.cb, "scripts", "a.sh"), "a2\n", mtime=base + 10)
        os.remove(os.path.join(self.ca, "scripts", "tools", "b.py"))
        out = self.sync()
        self.assertEqual(read(os.path.join(self.ca, "scripts", "a.sh")), "a2\n")
        self.assertFalse(os.path.exists(os.path.join(self.cb, "scripts", "tools", "b.py")))
        self.assertIn("files from bob: scripts/a.sh", out)

    def test_direction_filters_and_dry_run(self):
        other_a = side.encode(os.path.join(self.a, "secret"))
        write(self.ta("s1.jsonl"), line(self.pa, 1))
        write(os.path.join(self.ca, "projects", other_a, "s2.jsonl"), line(self.a + "/secret", 1))
        write(self.tb("s3.jsonl"), line(self.pb, 1))
        self.config({"projects": {"exclude": ["~/secret"]}}, peer={"direction": "push"})

        self.sync("--dry")
        self.assertFalse(os.path.exists(self.tb("s1.jsonl")))

        self.sync()
        self.assertTrue(os.path.exists(self.tb("s1.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.cb, "projects", side.encode(
            os.path.join(self.b, "secret")))))
        # push only: nothing comes back from bob
        self.assertFalse(os.path.exists(self.ta("s3.jsonl")))

    def test_wait_status_and_session_hook(self):
        write(self.ta("s1.jsonl"), line(self.pa, 1))
        out = self.run_cli("run", "bob", "--wait", "60")
        self.assertIn("bob via", out)
        self.assertIn("last sync", self.run_cli("status"))

        self.config({"auto": {"on_start": True, "min_gap_minutes": 0}})
        write(self.ta("s2.jsonl"), line(self.pa, 2))
        env = dict(os.environ, HOME=self.a)
        env.pop("CLAUDE_CONFIG_DIR", None)
        p = subprocess.run([sys.executable, "-I", CLI, "hook", "session-start"], env=env,
                           input=b"{}", stdout=subprocess.PIPE, timeout=10)
        self.assertEqual(p.returncode, 0)
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.exists(self.tb("s2.jsonl")):
            time.sleep(0.5)
        self.assertTrue(os.path.exists(self.tb("s2.jsonl")))

    def test_bad_config_is_refused(self):
        self.config({"files": {"x": {"path": "projects"}}})
        out = self.run_cli("run", "bob", ok=False)
        self.assertIn("synced by ccsync itself", out)


if __name__ == "__main__":
    unittest.main()
