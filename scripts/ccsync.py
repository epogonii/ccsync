#!/usr/bin/env python3
# ccsync: two-way sync of Claude Code transcripts, memory, prompt history and
# chosen files between machines over ssh. The config lives in
# ~/.claude/ccsync/config.json, see README.md.
import base64
import fnmatch
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ccsync_side as side  # noqa: E402

VERSION = "0.1.0"
SIDE_FILE = os.path.join(HERE, "ccsync_side.py")
with open(SIDE_FILE, "rb") as _f:
    SIDE_BYTES = _f.read()
REMOTE_HELPER = ".cache/ccsync/side-%s.py" % hashlib.sha256(SIDE_BYTES).hexdigest()[:12]

DEFAULTS = {
    "peers": {},
    "sync": {"transcripts": True, "memory": True, "history": True, "memory_deletions": True},
    "projects": {"include": [], "exclude": []},
    "files": {},
    "after": [],
    "auto": {"on_start": False, "on_end": False, "every_minutes": 0, "min_gap_minutes": 15,
             "peers": []},
    "keep_replaced_days": 30,
    "retries": 5,
    "ssh_options": [],
}
PEER_KEYS = {"ssh", "dir", "direction", "paths", "python", "claude_dir", "sync", "projects",
             "files", "after", "ssh_options"}
SET_KEYS = {"path", "file", "include", "exclude", "delete", "direction"}
DIRECTIONS = ("both", "push", "pull")
RESERVED = ("projects", "ccsync", "history.jsonl")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9-]+$")
MIRROR_RE = re.compile(r"/\.cache/ccsync/mirror/[0-9a-f]{32}$")
TIMER_LABEL = "ccsync"


class UserError(Exception):
    pass


class SyncError(Exception):
    def __init__(self, msg, unreachable=False, busy=False):
        Exception.__init__(self, msg)
        self.unreachable = unreachable
        self.busy = busy


# ---- paths and config

def claude_dir():
    return side.claude_dir({})


def data_dir():
    return side.data_dir(claude_dir())


def config_path():
    return os.path.join(data_dir(), "config.json")


def short(path):
    h = os.path.expanduser("~")
    if path == h or path.startswith(h + "/"):
        return "~" + path[len(h):]
    return path


def load_raw():
    try:
        with open(config_path(), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise UserError("%s is not valid JSON: %s" % (short(config_path()), e))
    if not isinstance(raw, dict):
        raise UserError("%s must hold a JSON object" % short(config_path()))
    return raw


def save_raw(raw):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(path + ".tmp", path)


def effective(raw):
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k in ("sync", "projects", "auto") and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def strlist(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def norm_set(sid, s):
    where = "files.%s" % sid
    if not NAME_RE.match(sid):
        raise UserError("%s: use letters, digits, '.', '_' or '-' in a set name" % where)
    if not isinstance(s, dict):
        raise UserError("%s must be an object" % where)
    for k in s:
        if k not in SET_KEYS:
            raise UserError("%s: unknown key %r" % (where, k))
    if ("path" in s) == ("file" in s):
        raise UserError('%s needs either "path" (a folder) or "file"' % where)
    raw = s.get("path", s.get("file"))
    if not isinstance(raw, str) or not raw.strip():
        raise UserError("%s: the path is empty" % where)
    if raw.startswith("/"):
        raise UserError("%s: give the path relative to ~/.claude, or starting with ~/" % where)
    in_home = raw == "~" or raw.startswith("~/")
    parts = [x for x in (raw[2:] if raw.startswith("~/") else "" if raw == "~" else raw).split("/")
             if x not in ("", ".")]
    if ".." in parts:
        raise UserError('%s: ".." is not allowed in a path' % where)
    inner = parts[1:] if in_home and parts[:1] == [".claude"] else parts if not in_home else None
    if inner and inner[0] in RESERVED:
        raise UserError("%s: %s is synced by ccsync itself" % (where, inner[0]))
    prefix = "~/" if in_home else ""
    if "file" in s:
        if not parts:
            raise UserError("%s: name a file" % where)
        only = parts[-1]
        path = prefix + "/".join(parts[:-1]) if len(parts) > 1 else ("~" if in_home else ".")
    else:
        if not parts or (in_home and parts == [".claude"]):
            raise UserError("%s: name a folder, not all of %s" % (where, "~" if in_home and not parts else "~/.claude"))
        only = None
        path = prefix + "/".join(parts)
    for k in ("include", "exclude"):
        if k in s and not strlist(s[k]):
            raise UserError("%s.%s must be a list of patterns" % (where, k))
    if s.get("direction", "both") not in DIRECTIONS:
        raise UserError("%s.direction must be one of %s" % (where, ", ".join(DIRECTIONS)))
    if not isinstance(s.get("delete", False), bool):
        raise UserError("%s.delete must be true or false" % where)
    return {"path": path, "only": only, "include": list(s.get("include", [])),
            "exclude": list(s.get("exclude", [])), "delete": s.get("delete", False),
            "direction": s.get("direction", "both")}


def problems(raw):
    out = []
    cfg = effective(raw)
    for k in raw:
        if k not in DEFAULTS:
            out.append("unknown key %r" % k)

    def check_sync(v, where):
        if not isinstance(v, dict):
            out.append("%s must be an object" % where)
            return
        for k, x in v.items():
            if k not in DEFAULTS["sync"]:
                out.append("%s: unknown key %r" % (where, k))
            elif not isinstance(x, bool):
                out.append("%s.%s must be true or false" % (where, k))

    def check_projects(v, where):
        if not isinstance(v, dict):
            out.append("%s must be an object" % where)
            return
        for k, x in v.items():
            if k not in ("include", "exclude"):
                out.append("%s: unknown key %r" % (where, k))
            elif not strlist(x):
                out.append("%s.%s must be a list of patterns" % (where, k))

    def check_path(v):
        return isinstance(v, str) and (v == "~" or v.startswith("~/") or v.startswith("/"))

    check_sync(raw.get("sync", {}), "sync")
    check_projects(raw.get("projects", {}), "projects")
    files = cfg["files"] if isinstance(cfg["files"], dict) else {}
    if not isinstance(cfg["files"], dict):
        out.append("files must be an object")
    for sid, s in files.items():
        try:
            norm_set(sid, s)
        except UserError as e:
            out.append(str(e))
    peers = cfg["peers"] if isinstance(cfg["peers"], dict) else {}
    if not isinstance(cfg["peers"], dict):
        out.append("peers must be an object")
    for name, p in peers.items():
        where = "peers.%s" % name
        if not NAME_RE.match(name):
            out.append("%s: use letters, digits, '.', '_' or '-' in a peer name" % where)
        if not isinstance(p, dict):
            out.append("%s must be an object" % where)
            continue
        for k in p:
            if k not in PEER_KEYS:
                out.append("%s: unknown key %r" % (where, k))
        if ("ssh" in p) == ("dir" in p):
            out.append('%s needs either "ssh" or "dir"' % where)
        if "ssh" in p and not (isinstance(p["ssh"], str) and p["ssh"]
                               or strlist(p["ssh"]) and p["ssh"]):
            out.append("%s.ssh must be a host or a list of hosts" % where)
        if "dir" in p and not (isinstance(p["dir"], str)
                               and os.path.isabs(os.path.expanduser(p["dir"]))):
            out.append("%s.dir must be an absolute path" % where)
        if p.get("direction", "both") not in DIRECTIONS:
            out.append("%s.direction must be one of %s" % (where, ", ".join(DIRECTIONS)))
        if "paths" in p:
            m = p["paths"]
            if not (isinstance(m, dict) and m and all(check_path(a) and check_path(b)
                                                       for a, b in m.items())):
                out.append('%s.paths must map paths here to paths there, like {"~": "~"}' % where)
        if "sync" in p:
            check_sync(p["sync"], where + ".sync")
        if "projects" in p:
            check_projects(p["projects"], where + ".projects")
        if "files" in p:
            if not strlist(p["files"]):
                out.append("%s.files must be a list of set names" % where)
            else:
                for sid in p["files"]:
                    if sid not in files:
                        out.append("%s.files: no set %r under files" % (where, sid))
        for k in ("after", "ssh_options"):
            if k in p and not strlist(p[k]):
                out.append("%s.%s must be a list of strings" % (where, k))
        for k in ("python", "claude_dir"):
            if k in p and not isinstance(p[k], str):
                out.append("%s.%s must be a string" % (where, k))
    for k in ("after", "ssh_options"):
        if not strlist(cfg[k]):
            out.append("%s must be a list of strings" % k)
    auto = raw.get("auto", {})
    if not isinstance(auto, dict):
        out.append("auto must be an object")
    else:
        for k, v in auto.items():
            if k not in DEFAULTS["auto"]:
                out.append("auto: unknown key %r" % k)
            elif k in ("on_start", "on_end") and not isinstance(v, bool):
                out.append("auto.%s must be true or false" % k)
            elif k in ("every_minutes", "min_gap_minutes") and not (
                    isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                out.append("auto.%s must be a whole number of minutes" % k)
            elif k == "peers" and not (strlist(v) and all(x in peers for x in v)):
                out.append("auto.peers must list peers from peers")
    for k in ("keep_replaced_days", "retries"):
        v = cfg[k]
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
            out.append("%s must be a whole number from 1" % k)
    return out


def peer_conf(cfg, name):
    p = cfg["peers"][name]
    sync = dict(cfg["sync"])
    sync.update(p.get("sync") or {})
    projects = dict(DEFAULTS["projects"])
    projects.update(p.get("projects", cfg["projects"]))
    names = p.get("files")
    sets = {sid: norm_set(sid, s) for sid, s in cfg["files"].items()
            if names is None or sid in names}
    hosts = p.get("ssh")
    if isinstance(hosts, str):
        hosts = [hosts]
    return {
        "name": name,
        "ssh": hosts,
        "dir": os.path.expanduser(p["dir"]) if p.get("dir") else None,
        "direction": p.get("direction", "both"),
        "paths": p.get("paths") or {"~": "~"},
        "python": p.get("python", "python3"),
        "claude_dir": p.get("claude_dir"),
        "sync": sync,
        "projects": projects,
        "sets": sets,
        "after": p.get("after", cfg["after"]),
        "ssh_options": list(p.get("ssh_options", [])) + list(cfg["ssh_options"]),
    }


def scope_hash(pc):
    keys = ("paths", "sync", "projects", "sets")
    blob = json.dumps({k: pc[k] for k in keys}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---- log and results

class Log:
    def __init__(self, echo):
        self.echo = echo
        d = os.path.join(data_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, time.strftime("%Y-%m-%d") + ".log")
        self.f = open(self.path, "a", encoding="utf-8")

    def __call__(self, msg=""):
        for line in str(msg).splitlines() or [""]:
            self.f.write(time.strftime("%H:%M:%S ") + line + "\n")
        self.f.flush()
        if self.echo:
            print(msg, flush=True)


def last_path(name):
    return os.path.join(data_dir(), "last", name + ".json")


def read_last(name):
    try:
        with open(last_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def write_last(name, summary):
    prev = read_last(name)
    if prev and prev.get("notified") and prev.get("error") == summary.get("error") \
            and conflicts_of(prev) == conflicts_of(summary):
        summary["notified"] = True
    side.write_json(last_path(name), summary)


def conflicts_of(s):
    out = []
    for k in ("in", "out"):
        out += (s.get(k) or {}).get("conflicts", [])
    return out + s.get("file_conflicts", [])


def tail(text, n=3):
    lines = [x for x in text.strip().splitlines() if x.strip()]
    return " / ".join(lines[-n:])


def ago(t):
    d = max(0, time.time() - t)
    if d < 90:
        return "%d s ago" % d
    if d < 5400:
        return "%d min ago" % (d / 60)
    if d < 129600:
        return "%.0f h ago" % (d / 3600)
    return "%.0f days ago" % (d / 86400)


def counts(res):
    st = res.get("stats", {})
    bits = ["%d %s" % (st[k], label) for k, label in (
        ("copied", "copied"), ("appended", "appended"), ("merged", "merged"),
        ("replaced", "replaced"), ("index_lines", "index lines")) if st.get(k)]
    h = res.get("history")
    if isinstance(h, list) and h[1] != h[0]:
        bits.append("history +%d" % (h[1] - h[0]))
    elif h == "busy":
        bits.append("history busy, next time")
    return ", ".join(bits) or "nothing new"


def describe(s):
    name = s["peer"]
    if not s.get("ok"):
        return ["%s: %s" % (name, s.get("error", "failed"))]
    lines = ["%s via %s%s, %.0f s" % (name, s.get("via", "?"), " (dry run, nothing written)"
                                      if s.get("dry") else "", s.get("duration", 0))]
    if "in" in s:
        lines.append("  from %s: %s" % (name, counts(s["in"])))
    if "out" in s:
        lines.append("  to %s: %s" % (name, counts(s["out"])))

    def listing(label, items, limit=8):
        if items:
            more = " and %d more" % (len(items) - limit) if len(items) > limit else ""
            lines.append("  %s: %s%s" % (label, ", ".join(items[:limit]), more))

    listing("files from %s" % name, s.get("files_in", []))
    listing("files to %s" % name, s.get("files_out", []))
    listing("deleted here", [os.path.basename(p) for p in s.get("deleted_in", [])])
    listing("deleted on %s" % name, [os.path.basename(p) for p in s.get("deleted_out", [])])
    listing("kept, changed after the last sync",
            [os.path.basename(p) for p in s.get("kept_in", []) + s.get("kept_out", [])])
    listing("conflicts, left as they are", conflicts_of(s))
    listing("after-commands that failed", s.get("after_failed", []))
    return lines


# ---- the two sides

class LocalSide:
    remote = False

    def __init__(self, label, home=None):
        self.label = label
        self.env = dict(os.environ)
        if home:
            self.env["HOME"] = home
            self.env.pop("CLAUDE_CONFIG_DIR", None)

    def cmd(self, op, *args):
        return [sys.executable, "-I", SIDE_FILE, op] + list(args)

    def path(self, p):
        return p


class SshSide:
    remote = True

    def __init__(self, label, host, opts, python):
        self.label, self.host, self.opts, self.python = label, host, opts, python
        self.env = None

    def cmd(self, op, *args):
        remote = " ".join(shlex.quote(a) for a in [self.python, "-I", REMOTE_HELPER, op] + list(args))
        return ["ssh"] + self.opts + [self.host, remote]

    def path(self, p):
        return "%s:%s" % (self.host, p)


def call(sd, op, req, tries=1):
    data = json.dumps(req).encode()
    for attempt in range(tries):
        p = subprocess.run(sd.cmd(op), input=data, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=sd.env)
        if p.returncode == 0:
            out = p.stdout.decode()
            return json.loads(out) if out.strip() else {}
        lost = sd.remote and p.returncode == 255
        if lost and attempt + 1 < tries:
            time.sleep(3)
            continue
        raise SyncError("%s: %s failed: %s" % (sd.label, op, tail(p.stderr.decode(errors="replace"))),
                        unreachable=lost)


def ssh_opts(pc, auto):
    opts = []
    for o in pc["ssh_options"]:  # ssh keeps the first value it sees, so these go first
        opts += ["-o", o]
    opts += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % (8 if auto else 20),
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=4",
             "-o", "StrictHostKeyChecking=accept-new"]
    cp = os.path.join(os.path.expanduser("~"), ".cache", "ccsync", "ssh-%C")
    if " " not in cp and len(cp) + 38 < 100:  # unix socket paths are short
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        opts += ["-o", "ControlMaster=auto", "-o", "ControlPath=" + cp, "-o", "ControlPersist=60"]
    return opts


def connect(pc, log, tries, auto):
    opts = ssh_opts(pc, auto)
    errors = {}
    probe = "test -f %s && echo have || echo need" % shlex.quote(REMOTE_HELPER)
    for attempt in range(tries):
        for host in pc["ssh"]:
            p = subprocess.run(["ssh"] + opts + [host, probe], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
            if p.returncode != 0:
                errors[host] = tail(p.stderr.decode(errors="replace"), 1) or "exit %d" % p.returncode
                continue
            if p.stdout.strip() == b"need":
                up = "mkdir -p .cache/ccsync && cat > {0}.tmp && mv {0}.tmp {0}".format(
                    shlex.quote(REMOTE_HELPER))
                q = subprocess.run(["ssh"] + opts + [host, up], input=SIDE_BYTES,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if q.returncode != 0:
                    raise SyncError("%s: could not copy the helper over: %s"
                                    % (host, tail(q.stderr.decode(errors="replace"))))
            return SshSide(pc["name"], host, opts, pc["python"])
        if attempt + 1 < tries:
            log("  %s did not answer, trying again" % pc["name"])
            time.sleep(3)
    raise SyncError("unreachable (%s)" % "; ".join("%s: %s" % kv for kv in errors.items()),
                    unreachable=True)


def rsync(args, sd, log, tries):
    cmd = ["rsync"] + args
    if sd.remote:
        cmd[1:1] = ["-e", "ssh " + " ".join(sd.opts)]
    for attempt in range(tries):
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0:
            return
        err = tail(p.stderr.decode(errors="replace"))
        if attempt + 1 < tries:
            log("  rsync failed (%s), trying again" % err)
            time.sleep(3)
            continue
        raise SyncError("rsync failed: %s" % err, unreachable=p.returncode in (12, 30, 35, 255))


def mirror_rules(names, want):
    r = []
    for n in names:
        r.append("--include=/projects/%s/" % n)
        if want["transcripts"]:
            if not want["memory"]:
                r.append("--exclude=/projects/%s/memory/" % n)
            r.append("--include=/projects/%s/**" % n)
        elif want["memory"]:
            r += ["--include=/projects/%s/memory/" % n, "--include=/projects/%s/memory/**" % n]
    r += ["--include=/projects/", "--exclude=/projects/**"]
    if want["history"]:
        r.append("--include=/history.jsonl")
    r.append("--exclude=/*")
    return r


def mirror(src, src_dir, dst, dst_dir, names, want, remote, log, tries):
    # --delete-excluded makes the copy exact, so it may only ever point at a mirror
    if not MIRROR_RE.search(dst_dir):
        raise SyncError("refusing to mirror into %s" % dst_dir)
    args = ["-a", "--delete", "--delete-excluded", "--partial"] + mirror_rules(names, want)
    rsync(args + [src.path(src_dir + "/"), dst.path(dst_dir + "/")], remote, log, tries)


def transfer(src, src_cd, dst, dst_cd, files, sets_req, stamp):
    if not files:
        return []
    preq = {"sets": sets_req, "files": files}
    ureq = {"sets": sets_req, "stamp": stamp}
    if src_cd:
        preq["claude_dir"] = src_cd
    if dst_cd:
        ureq["claude_dir"] = dst_cd
    arg = base64.b64encode(json.dumps(ureq).encode()).decode()
    e1, e2 = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    p1 = subprocess.Popen(src.cmd("pack"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=e1, env=src.env)
    p2 = subprocess.Popen(dst.cmd("unpack", arg), stdin=p1.stdout, stdout=subprocess.PIPE,
                          stderr=e2, env=dst.env)
    p1.stdout.close()
    p1.stdin.write(json.dumps(preq).encode())
    p1.stdin.close()
    out = p2.stdout.read()
    p2.wait()
    p1.wait()
    if p1.returncode or p2.returncode:
        e1.seek(0)
        e2.seek(0)
        err = tail(e1.read().decode(errors="replace") + "\n" + e2.read().decode(errors="replace"))
        raise SyncError("copying files failed: %s" % err)
    return json.loads(out.decode())["written"]


# ---- one sync

def expand(p, home):
    if p == "~":
        return home
    if p.startswith("~/"):
        p = home + p[1:]
    return p.rstrip("/") or "/"


def encode_glob(g, home):
    return side.encode(expand(g, home) if g.startswith("~") else g, keep="*?[]!")


def translate(p, pairs):
    parts = p.split("/")
    if parts[0] != "projects":
        return p
    n = side.map_name(parts[1], pairs)
    return None if n is None else "/".join(["projects", n] + parts[2:])


def plan_sets(sets, here, there, push, pull):
    out, into, clash = [], [], []
    for sid, s in sets.items():
        can_out = push and s["direction"] in ("both", "push")
        can_in = pull and s["direction"] in ("both", "pull")
        a_all, b_all = here.get(sid, {}), there.get(sid, {})
        for rel in sorted(set(a_all) | set(b_all)):
            a, b = a_all.get(rel), b_all.get(rel)
            if a and b and a[2] == b[2]:
                continue
            if a and b:
                if a[0] > b[0] + 1:
                    if can_out:
                        out.append([sid, rel])
                elif b[0] > a[0] + 1:
                    if can_in:
                        into.append([sid, rel])
                else:
                    clash.append(sid + "/" + rel)
            elif a:
                if can_out:
                    out.append([sid, rel])
            elif can_in:
                into.append([sid, rel])
    return out, into, clash


def sync_peer(cfg, name, log, dry=False, full=False, auto=False, reason="manual"):
    pc = peer_conf(cfg, name)
    start = time.time()
    s = {"peer": name, "start": start, "dry": dry, "reason": reason, "ok": False}
    tries = 1 if auto else cfg["retries"]
    loc = LocalSide("here")
    try:
        if pc["ssh"]:
            rem = connect(pc, log, tries, auto)
            s["via"] = rem.host
        else:
            if not os.path.isdir(pc["dir"]):
                raise SyncError("%s is not there" % pc["dir"], unreachable=True)
            rem = LocalSide(name, home=pc["dir"])
            s["via"] = short(pc["dir"])
        rcd = pc["claude_dir"]
        li = call(loc, "info", {})
        ri = call(rem, "info", {"claude_dir": rcd} if rcd else {}, tries)
        if ri.get("protocol") != side.PROTOCOL:
            raise SyncError("%s runs a different ccsync helper protocol" % name)
        if rem.remote and not ri.get("rsync"):
            raise SyncError("%s has no rsync" % name)
        if li["machine_id"] == ri["machine_id"]:
            raise SyncError("%s has the same machine id as this machine. If it is a clone, "
                            "delete ~/.claude/ccsync/machine-id on it" % name)
        holder = "%s pid %d %s" % (li["host"], os.getpid(), uuid.uuid4().hex[:8])
        got = call(loc, "lock", {"holder": holder})
        if not got["ok"]:
            raise SyncError("another sync is running here (%s)" % got["holder"], busy=True)
        try:
            req = {"holder": holder, "claude_dir": rcd} if rcd else {"holder": holder}
            got = call(rem, "lock", req, tries)
            if not got["ok"]:
                raise SyncError("another sync is running on %s (%s)" % (name, got["holder"]),
                                busy=True)
            try:
                locked_sync(cfg, pc, s, loc, rem, li, ri, log, dry, full, tries)
            finally:
                try:
                    call(rem, "unlock", req)
                except SyncError:
                    pass
        finally:
            call(loc, "unlock", {"holder": holder})
        s["ok"] = True
    except SyncError as e:
        s["error"] = str(e)
        s["unreachable"] = e.unreachable
        s["busy"] = e.busy
    s["duration"] = time.time() - start
    return s


def locked_sync(cfg, pc, s, loc, rem, li, ri, log, dry, full, tries):
    name = pc["name"]
    rcd = pc["claude_dir"]

    def rq(req):
        if rcd:
            req["claude_dir"] = rcd
        return req

    want = pc["sync"]
    push = pc["direction"] in ("both", "push")
    pull = pc["direction"] in ("both", "pull")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lid, rid = li["machine_id"], ri["machine_id"]
    lhome, rhome = li["home"], ri["home"]
    maps = [(expand(a, lhome), expand(b, rhome)) for a, b in pc["paths"].items()]
    enc_out = sorted(((side.encode(a), side.encode(b)) for a, b in maps), key=lambda p: -len(p[0]))
    enc_in = sorted(((side.encode(b), side.encode(a)) for a, b in maps), key=lambda p: -len(p[0]))
    inc = [encode_glob(g, lhome) for g in pc["projects"]["include"]]
    exc = [encode_glob(g, lhome) for g in pc["projects"]["exclude"]]

    def glob_ok(n):
        if inc and not any(fnmatch.fnmatchcase(n, g) for g in inc):
            return False
        return not any(fnmatch.fnmatchcase(n, g) for g in exc)

    def sel_here(n):
        return bool(PROJECT_RE.match(n)) and side.map_name(n, enc_out) is not None and glob_ok(n)

    def sel_there(n):
        here_name = side.map_name(n, enc_in) if PROJECT_RE.match(n) else None
        return here_name is not None and glob_ok(here_name)

    sets = pc["sets"]
    sets_req = {sid: {k: x[k] for k in ("path", "only", "include", "exclude")}
                for sid, x in sets.items()}
    projects_on = want["transcripts"] or want["memory"]
    mem_del = want["memory"] and want["memory_deletions"]

    lscan = call(loc, "scan", {"peer_id": rid, "sets": sets_req})
    rscan = call(rem, "scan", rq({"peer_id": lid, "sets": sets_req}), tries)
    lb = (lscan.get("state") or {}).get("base")
    rb = (rscan.get("state") or {}).get("base")
    base = min(lb, rb) if lb and rb else 0
    scope = scope_hash(pc)
    same_scope = (lscan.get("state") or {}).get("scope") == scope
    skip_before = base if base and same_scope and not full else 0
    if not base:
        log("  first sync with %s: every file is compared" % name)
    elif not skip_before:
        log("  %s: every file is compared this time" % ("--full" if full else "what is synced changed"))

    # deletions first, or the copy would bring the files back
    def deletable(p, sel):
        parts = p.split("/")
        if parts[0] == "projects":
            return mem_del and sel(parts[1])
        x = sets.get(parts[1]) if parts[0] == "files" else None
        if not x or not x["delete"]:
            return False
        rel = "/".join(parts[2:])
        return rel == x["only"] if x["only"] else side.matches(rel, x["include"], x["exclude"])

    def way_ok(p, way):
        parts = p.split("/")
        return parts[0] != "files" or sets[parts[1]]["direction"] in ("both", way)

    gone_here = [p for p in lscan.get("missing", []) if deletable(p, sel_here) and way_ok(p, "push")]
    gone_there = [p for p in rscan.get("missing", []) if deletable(p, sel_there) and way_ok(p, "pull")]
    del_out = [q for q in (translate(p, enc_out) for p in gone_here) if q] if push else []
    del_in = [q for q in (translate(p, enc_in) for p in gone_there) if q] if pull else []
    if del_out:
        r = call(rem, "delete", rq({"paths": del_out, "base": base, "stamp": stamp, "dry": dry,
                                    "sets": sets_req}), tries)
        s["deleted_out"], s["kept_out"] = r["deleted"], r["kept"]
    if del_in:
        r = call(loc, "delete", {"paths": del_in, "base": base, "stamp": stamp, "dry": dry,
                                 "sets": sets_req})
        s["deleted_in"], s["kept_in"] = r["deleted"], r["kept"]

    if projects_on or want["history"]:
        names_here = [n for n in lscan["projects"] if sel_here(n)] if projects_on else []
        names_there = [n for n in rscan["projects"] if sel_there(n)] if projects_on else []
        lmirror = lhome + "/.cache/ccsync/mirror/" + rid
        rmirror = rhome + "/.cache/ccsync/mirror/" + lid
        mreq = {"base": base, "skip_before": skip_before, "dry": dry, "stamp": stamp, "want": want}
        if push:
            log("  copying %d projects to %s" % (len(names_here), name))
            mirror(loc, li["claude_dir"], rem, rmirror, names_here, want, rem, log, tries)
            s["out"] = call(rem, "merge", rq(dict(mreq, mirror=rmirror, maps=maps)), tries)
        if pull:
            # after the merge over there, so this side sees what it has now
            log("  copying %d projects from %s" % (len(names_there), name))
            mirror(rem, ri["claude_dir"], loc, lmirror, names_there, want, rem, log, tries)
            s["in"] = call(loc, "merge", dict(mreq, mirror=lmirror, maps=[(b, a) for a, b in maps]))

    if sets:
        if del_out or del_in:
            here = call(loc, "scan", {"projects": False, "sets": sets_req})["sets"]
            there = call(rem, "scan", rq({"projects": False, "sets": sets_req}), tries)["sets"]
        else:
            here, there = lscan["sets"], rscan["sets"]
        out, into, clash = plan_sets(sets, here, there, push, pull)
        s["files_out"] = ["/".join(f) for f in out]
        s["files_in"] = ["/".join(f) for f in into]
        s["file_conflicts"] = clash
        if not dry:
            transfer(loc, None, rem, rcd, out, sets_req, stamp)
            transfer(rem, rcd, loc, None, into, sets_req, stamp)

    if dry:
        return
    failed = []
    for sd, ok, label in ((loc, pull, "here"), (rem, push, name)):
        if ok and pc["after"]:
            res = call(sd, "after", rq({"commands": pc["after"]}) if sd is rem
                       else {"commands": pc["after"]}, tries if sd is rem else 1)
            for r in res["results"]:
                if r["code"] != 0:
                    failed.append("%s on %s (exit %d)" % (r["cmd"], label, r["code"]))
                    log("  after-command failed on %s: %s\n%s" % (label, r["cmd"], r["out"]))
    s["after_failed"] = failed

    keep = cfg["keep_replaced_days"]
    call(loc, "prune", {"keep_days": keep})
    call(rem, "prune", rq({"keep_days": keep}), tries)

    # what each side holds now, so the next run can tell a deletion from a new file
    del_sets = {sid: sets_req[sid] for sid, x in sets.items() if x["delete"]}
    lf = call(loc, "scan", {"projects": mem_del, "sets": del_sets, "hash": False})
    rf = call(rem, "scan", rq({"projects": mem_del, "sets": del_sets, "hash": False}), tries)

    def manifest(scan, sel):
        out = [p for p in scan.get("memory", []) if sel(p.split("/")[1])] if mem_del else []
        for sid, files in sorted(scan["sets"].items()):
            out += ["files/%s/%s" % (sid, rel) for rel in sorted(files)]
        return out

    call(loc, "save", {"peer_id": rid, "state": {
        "base": s["start"], "manifest": manifest(lf, sel_here), "scope": scope, "name": name}})
    call(rem, "save", rq({"peer_id": lid, "state": {
        "base": s["start"], "manifest": manifest(rf, sel_there), "name": li["host"]}}), tries)


# ---- commands

def spawn(args):
    return subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__)] + args,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def run_peers(cfg, peers, log, dry, full, auto, reason):
    results = []
    for name in peers:
        if auto:
            last = read_last(name)
            gap = cfg["auto"]["min_gap_minutes"] * 60
            if last and last.get("ok") and time.time() - last["start"] < gap:
                continue
        log("sync with %s (%s)%s" % (name, reason, ", dry run" if dry else ""))
        s = sync_peer(cfg, name, log, dry=dry, full=full, auto=auto, reason=reason)
        for line in describe(s):
            log(line)
        if s.get("busy") and auto:
            continue
        if not dry:
            s["notified"] = not auto
            write_last(name, s)
        results.append(s)
    return results


def cmd_run(args, from_command):
    dry = full = auto = quiet = False
    reason, wait, result, peers = "manual", None, None, []
    it = iter(args)
    for a in it:
        if a in ("--dry", "--dry-run", "-n"):
            dry = True
        elif a == "--full":
            full = True
        elif a == "--auto":
            auto = True
        elif a == "--quiet":
            quiet = True
        elif a == "--reason":
            reason = next(it, "manual")
        elif a == "--wait":
            wait = int(next(it, "90"))
        elif a == "--result":
            result = next(it, None)
        elif a.startswith("-"):
            raise UserError("unknown option %s" % a)
        else:
            peers.append(a)
    raw = load_raw()
    probs = problems(raw)
    if probs:
        if auto:
            return 1
        raise UserError("fix the config first:\n  " + "\n  ".join(probs))
    cfg = effective(raw)
    if not cfg["peers"]:
        if auto:
            return 0
        raise UserError("no machines to sync with yet. Set one up: /ccsync:setup")
    unknown = [p for p in peers if p not in cfg["peers"]]
    if unknown:
        raise UserError("no peer named %s. Peers: %s" % (", ".join(unknown), ", ".join(cfg["peers"])))
    if auto and not peers:
        peers = cfg["auto"]["peers"] or list(cfg["peers"])
    peers = peers or list(cfg["peers"])
    if from_command and wait is None:
        wait = 60
    if wait is not None:
        os.makedirs(data_dir(), exist_ok=True)
        result = os.path.join(data_dir(), "run-%s.json" % uuid.uuid4().hex[:8])
        child_args = ["run"] + peers + ["--quiet", "--result", result, "--reason", reason]
        child_args += [f for f, on in (("--dry", dry), ("--full", full)) if on]
        child = spawn(child_args)
        try:
            child.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            print("Still syncing in the background. /ccsync status shows the result once it is "
                  "done, the log is %s" % short(Log(False).path))
            return 0
        try:
            with open(result, encoding="utf-8") as f:
                results = json.load(f)
            os.remove(result)
        except (OSError, ValueError):
            print("The sync stopped without a result, see %s" % short(Log(False).path))
            return 1
        for s in results:
            print("\n".join(describe(s)))
        return 0 if all(s.get("ok") for s in results) else 1
    log = Log(echo=not quiet)
    results = run_peers(cfg, peers, log, dry, full, auto, reason)
    if result:
        side.write_json(result, results)
    return 0 if all(s.get("ok") or s.get("busy") and auto for s in results) else 1


def what_syncs(cfg, pc=None):
    want = (pc or {}).get("sync") or cfg["sync"]
    bits = [k for k in ("transcripts", "memory", "history") if want[k]]
    if want["memory"] and want["memory_deletions"]:
        bits[bits.index("memory")] = "memory (with deletions)"
    sets = pc["sets"] if pc else cfg["files"]
    if sets:
        bits.append("files: " + ", ".join(sets))
    proj = (pc or {}).get("projects") or cfg["projects"]
    if proj.get("include"):
        bits.append("only projects " + ", ".join(proj["include"]))
    if proj.get("exclude"):
        bits.append("not " + ", ".join(proj["exclude"]))
    return "; ".join(bits) or "nothing"


def cmd_status():
    raw = load_raw()
    cfg = effective(raw)
    print("ccsync %s, config %s" % (VERSION, short(config_path())
                                    if os.path.exists(config_path()) else "not written yet"))
    probs = problems(raw)
    if probs:
        print("Config problems:\n  " + "\n  ".join(probs))
        return 1
    if not cfg["peers"]:
        print("No machines to sync with yet. Set one up: /ccsync:setup")
        return 0
    for name in cfg["peers"]:
        pc = peer_conf(cfg, name)
        where = ", ".join(pc["ssh"]) if pc["ssh"] else pc["dir"]
        arrow = {"both": "both ways", "push": "only to it", "pull": "only from it"}[pc["direction"]]
        print("\n%s: %s, %s" % (name, where, arrow))
        print("  syncs %s" % what_syncs(cfg, pc))
        last = read_last(name)
        if not last:
            print("  never synced")
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["start"]))
        if last.get("ok"):
            print("  last sync %s (%s), %s" % (when, ago(last["start"]), last.get("reason", "")))
            for line in describe(last)[1:]:
                print("  " + line.strip() if not line.startswith("  ") else line)
        else:
            print("  last try %s (%s) failed: %s" % (when, ago(last["start"]), last.get("error")))
    a = cfg["auto"]
    when = [w for w, on in (("when a session starts", a["on_start"]),
                            ("when a session ends", a["on_end"])) if on]
    if a["every_minutes"]:
        when.append("every %d min" % a["every_minutes"])
    auto_peers = a["peers"] or list(cfg["peers"])
    print("\nAuto sync: %s" % ((", ".join(when) + " (%s, at most once in %d min)"
                                % (", ".join(auto_peers), a["min_gap_minutes"])) if when else "off"))
    return 0


def cmd_peer(args):
    if len(args) < 2 or args[0] not in ("add", "rm"):
        raise UserError("usage: peer add <name> <user@host> [<user@host>...] | "
                        "peer add <name> --dir <path> | peer rm <name>")
    raw = load_raw()
    peers = raw.setdefault("peers", {})
    name = args[1]
    if args[0] == "rm":
        if name not in peers:
            raise UserError("no peer named %s" % name)
        del peers[name]
        auto = raw.get("auto") or {}
        if name in (auto.get("peers") or []):
            auto["peers"].remove(name)
        save_raw(raw)
        print("Removed %s. Its sync state stays in %s" % (name, short(data_dir())))
        return 0
    rest = args[2:]
    if not rest:
        raise UserError("give the address: peer add %s <user@host>" % name)
    entry = dict(peers.get(name) or {})
    entry.pop("ssh", None)
    entry.pop("dir", None)
    if rest[0] == "--dir":
        if len(rest) != 2:
            raise UserError("usage: peer add <name> --dir <path>")
        entry["dir"] = rest[1]
    else:
        entry["ssh"] = rest[0] if len(rest) == 1 else rest
    peers[name] = entry
    probs = problems(raw)
    if probs:
        raise UserError("not saved:\n  " + "\n  ".join(probs))
    save_raw(raw)
    print("Saved %s. Try it without writing anything: /ccsync run %s --dry" % (name, name))
    return 0


def cmd_auto(args):
    raw = load_raw()
    auto = raw.setdefault("auto", {})
    if len(args) == 2 and args[0] in ("start", "end") and args[1] in ("on", "off"):
        auto["on_" + args[0]] = args[1] == "on"
        save_raw(raw)
        print("Sync when a session %ss: %s" % (args[0], args[1]))
        return 0
    if len(args) == 2 and args[0] == "gap" and args[1].isdigit():
        auto["min_gap_minutes"] = int(args[1])
        save_raw(raw)
        print("Automatic syncs at most once in %s min" % args[1])
        return 0
    if len(args) == 2 and args[0] == "every" and (args[1].isdigit() or args[1] == "off"):
        minutes = 0 if args[1] == "off" else int(args[1])
        msg = set_timer(minutes)
        auto["every_minutes"] = minutes
        save_raw(raw)
        print(msg)
        return 0
    raise UserError("usage: auto start on|off | auto end on|off | auto every <minutes>|off | "
                    "auto gap <minutes>")


def merge_patch(target, patch):
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        else:
            target[k] = merge_patch(target.get(k), v)
    return target


def cmd_config(args):
    raw = load_raw()
    if args[:1] == ["set"]:
        if len(args) != 2:
            raise UserError("usage: config set '<JSON merge patch>'")
        try:
            patch = json.loads(args[1])
        except ValueError as e:
            raise UserError("not valid JSON: %s" % e)
        if not isinstance(patch, dict):
            raise UserError("the patch must be a JSON object")
        new = merge_patch(json.loads(json.dumps(raw)), patch)
        probs = problems(new)
        if probs:
            raise UserError("not saved:\n  " + "\n  ".join(probs))
        before = effective(raw)["auto"].get("every_minutes", 0)
        after = effective(new)["auto"].get("every_minutes", 0)
        msg = set_timer(after) if after != before else None
        save_raw(new)
        print("Saved %s." % short(config_path()))
        if msg:
            print(msg)
        return cmd_status()
    probs = problems(raw)
    if args[:1] == ["check"]:
        print("Config is fine." if not probs else "Config problems:\n  " + "\n  ".join(probs))
        return 1 if probs else 0
    if os.path.exists(config_path()):
        print("%s:\n%s" % (short(config_path()), json.dumps(raw, indent=2, ensure_ascii=False)))
    else:
        print("No config yet (%s). Defaults:\n%s"
              % (short(config_path()), json.dumps(DEFAULTS, indent=2)))
    if probs:
        print("Config problems:\n  " + "\n  ".join(probs))
    return 1 if probs else 0


INTERNAL = {"projects", "ccsync", "plugins", "shell-snapshots", "session-env", "sessions",
            "statsig", "todos", "file-history", "paste-cache", "debug", "cache", "ide", "backups",
            "logs", "downloads", "telemetry", "local", "jobs", "tmp", "state", "chrome", "daemon",
            "feedback", "sync-replaced", "history.jsonl", "settings.local.json", "stats-cache.json"}
KNOWN = [
    ("skills", "your skills"),
    ("agents", "your subagents"),
    ("commands", "your slash commands"),
    ("output-styles", "your output styles"),
    ("themes", "your custom themes"),
    ("hooks", "scripts your hooks run: code that runs by itself, sync only between your own machines"),
    ("plans", "plans written in plan mode"),
    ("CLAUDE.md", "your instructions for every project"),
    ("settings.json", "settings: permissions, hooks, status line, model. Paths in it must also work on the other machine"),
    ("keybindings.json", "your key bindings"),
]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def tree_size(path):
    total = count = 0
    if os.path.isfile(path):
        return os.path.getsize(path), 1
    for dirpath, dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
                count += 1
            except OSError:
                pass
    return total, count


def ssh_hosts():
    hosts, cur = [], []
    try:
        with open(os.path.expanduser("~/.ssh/config"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return hosts
    for raw in lines:
        parts = raw.strip().split(None, 1)
        if len(parts) != 2 or parts[0].startswith("#"):
            continue
        key, val = parts[0].lower(), parts[1].strip()
        if key == "host":
            cur = [{"alias": a} for a in val.split() if not any(c in a for c in "*?!")]
            hosts += cur
        elif key in ("hostname", "user"):
            for h in cur:
                h.setdefault(key, val)
    return hosts


def cmd_detect():
    raw = load_raw()
    cd = claude_dir()
    proj = os.path.join(cd, "projects")
    size, files = tree_size(proj) if os.path.isdir(proj) else (0, 0)
    names, memory = side.scan_projects(cd)
    try:
        with open(os.path.join(cd, "history.jsonl"), "rb") as f:
            history = sum(1 for _ in f)
    except OSError:
        history = 0
    known, other = [], []
    for name, what in KNOWN:
        p = os.path.join(cd, name)
        if os.path.exists(p):
            sz, n = tree_size(p)
            known.append({"path": name, "kind": "file" if os.path.isfile(p) else "dir",
                          "what": what, "files": n, "size": human(sz)})
    taken = {k for k, _ in KNOWN} | INTERNAL
    for name in sorted(os.listdir(cd)) if os.path.isdir(cd) else []:
        p = os.path.join(cd, name)
        if name in taken or name.startswith(".") or os.path.islink(p):
            continue
        if os.path.isfile(p) and re.search(r"(\.bak|\.orig|backup|\.lock|\.log|\.json\..*)", name):
            continue
        if re.match(r"policy-limits|remote-settings", name):
            continue
        sz, n = tree_size(p)
        other.append({"path": name, "kind": "file" if os.path.isfile(p) else "dir",
                      "files": n, "size": human(sz)})
    timer = "launchd" if sys.platform == "darwin" else "systemd" if sys.platform.startswith("linux") else None
    out = {
        "cli": "python3 -I %s" % shlex.quote(os.path.abspath(__file__)),
        "version": VERSION,
        "config_path": short(config_path()),
        "config": raw,
        "problems": problems(raw),
        "this_machine": {"host": socket.gethostname().split(".")[0], "home": os.path.expanduser("~"),
                         "claude_dir": short(cd), "timer": timer},
        "claude_data": {"projects": len(names), "transcript_files": files,
                        "projects_size": human(size), "memory_notes": len(memory),
                        "history_lines": history},
        "candidates": known,
        "other_in_claude_dir": other,
        "ssh_hosts": ssh_hosts(),
        "last_runs": {n: {k: (read_last(n) or {}).get(k) for k in ("ok", "error", "start")}
                      for n in effective(raw)["peers"]} if isinstance(raw.get("peers"), dict) else {},
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


HINTS = [
    ("Permission denied", "the key is not accepted there. Run once in a terminal: ssh-copy-id <user@host>"),
    ("Could not resolve hostname", "this name is not known on the network. Try the IP address"),
    ("nodename nor servname", "this name is not known on the network. Try the IP address"),
    ("timed out", "no answer: the machine is off, asleep, or not on this network"),
    ("No route to host", "no route there: the machine is off or on another network"),
    ("Connection refused", "nothing listens for ssh there. On a Mac turn on System Settings > "
                           "General > Sharing > Remote Login, on Linux start sshd"),
    ("Host key verification failed", "the host key changed. Check it, then fix ~/.ssh/known_hosts"),
    ("python3: command not found", "python3 is missing there: install it"),
    ("python3: not found", "python3 is missing there: install it"),
]


def cmd_test(args):
    if not args:
        raise UserError("usage: test <peer> | test <user@host> [<user@host>...] | test --dir <path>")
    raw = load_raw()
    cfg = effective(raw)
    if args[0] in (cfg.get("peers") or {}) and not problems(raw):
        pc = peer_conf(cfg, args[0])
    else:
        pc = {"name": args[0], "ssh": None, "dir": None, "python": "python3", "claude_dir": None,
              "ssh_options": list(cfg.get("ssh_options") or [])}
        if args[0] == "--dir":
            pc["name"], pc["dir"] = args[1], os.path.expanduser(args[1])
        else:
            pc["ssh"] = args
    try:
        if pc["ssh"]:
            sd = connect(pc, lambda *a: None, 1, False)
            where = sd.host
        else:
            if not os.path.isdir(pc["dir"]):
                raise SyncError("%s is not there" % pc["dir"])
            sd, where = LocalSide(pc["name"], home=pc["dir"]), pc["dir"]
        ri = call(sd, "info", {"claude_dir": pc["claude_dir"]} if pc["claude_dir"] else {})
        li = call(LocalSide("here"), "info", {})
    except SyncError as e:
        msg = str(e)
        hint = next((h for k, h in HINTS if k.lower() in msg.lower()), None)
        print("%s: %s" % ("Not reachable" if e.unreachable else "Problem", msg))
        if hint:
            print("What to do: %s" % hint)
        return 1
    print("Reachable as %s: %s, home %s, python %s, rsync %s." % (
        where, ri["host"], ri["home"], ri["python"], "yes" if ri["rsync"] else "no"))
    print("Claude Code folder there: %s, %d projects." % (ri["claude_dir"], ri.get("projects", 0)))
    bad = False
    if sd.remote and not ri["rsync"]:
        print("Problem: rsync is missing there. Install it (dnf install rsync, apt install rsync).")
        bad = True
    if ri["machine_id"] == li["machine_id"]:
        print("Problem: that is this machine, or a clone of it with the same "
              "~/.claude/ccsync/machine-id. On a clone delete that file.")
        bad = True
    return 1 if bad else 0


def cmd_log(args):
    n = int(args[0]) if args and args[0].isdigit() else 40
    d = os.path.join(data_dir(), "logs")
    logs = sorted(f for f in os.listdir(d) if f.endswith(".log")) if os.path.isdir(d) else []
    if not logs:
        print("No log yet.")
        return 0
    with open(os.path.join(d, logs[-1]), encoding="utf-8") as f:
        lines = f.read().splitlines()
    print("%s:\n%s" % (short(os.path.join(d, logs[-1])), "\n".join(lines[-n:])))
    return 0


def cmd_hook(args):
    event = args[0] if args else ""
    try:
        sys.stdin.read()
    except OSError:
        pass
    try:
        raw = load_raw()
    except UserError:
        return 0
    if problems(raw):
        return 0
    cfg = effective(raw)
    if not cfg["peers"]:
        return 0
    auto = cfg["auto"]
    if (event == "session-start" and auto["on_start"]) or (event == "session-end" and auto["on_end"]):
        spawn(["run", "--auto", "--reason", event, "--quiet"])
    if event != "session-start":
        return 0
    notes = []
    for name in cfg["peers"]:
        last = read_last(name)
        if not last or last.get("notified"):
            continue
        if not last.get("ok") and not last.get("unreachable") and not last.get("busy"):
            notes.append("sync with %s failed: %s" % (name, last.get("error")))
        elif last.get("ok") and conflicts_of(last):
            notes.append("%d conflict(s) with %s, see /ccsync status"
                         % (len(conflicts_of(last)), name))
        last["notified"] = True
        side.write_json(last_path(name), last)
    if notes:
        print(json.dumps({"systemMessage": "ccsync: " + "; ".join(notes)}))
    return 0


# ---- timer

def install_bin():
    b = os.path.join(data_dir(), "bin")
    os.makedirs(b, exist_ok=True)
    for fn in ("ccsync.py", "ccsync_side.py"):
        src, dst = os.path.join(HERE, fn), os.path.join(b, fn)
        with open(src, "rb") as f:
            data = f.read()
        try:
            with open(dst, "rb") as f:
                if f.read() == data:
                    continue
        except FileNotFoundError:
            pass
        side.write_file(dst, data, mode=0o755)
    return os.path.join(b, "ccsync.py")


def refresh_bin():
    # the timer runs a copy that does not move when the plugin updates
    b = os.path.join(data_dir(), "bin")
    if os.path.isdir(b) and os.path.realpath(b) != os.path.realpath(HERE):
        install_bin()


def timer_python():
    # a versioned interpreter path breaks on the next upgrade, the PATH one does not
    return shutil.which("python3") or sys.executable


def set_timer(minutes):
    if sys.platform == "darwin":
        return launchd_timer(minutes)
    if sys.platform.startswith("linux"):
        return systemd_timer(minutes)
    raise UserError("the timer is set up on macOS and Linux only. Elsewhere run "
                    "`python3 %s run --auto` from your own scheduler" % short(install_bin()))


def launchd_timer(minutes):
    plist = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % TIMER_LABEL)
    domain = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", domain, plist],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not minutes:
        if os.path.exists(plist):
            os.remove(plist)
        return "Timer removed."
    prog = install_bin()
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"}
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    with open(plist, "wb") as f:
        plistlib.dump({
            "Label": TIMER_LABEL,
            "ProgramArguments": [timer_python(), "-I", prog, "run", "--auto", "--reason", "timer",
                                 "--quiet"],
            "StartInterval": minutes * 60,
            "RunAtLoad": False,
            "ProcessType": "Background",
            "EnvironmentVariables": env,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": os.path.join(data_dir(), "logs", "timer.err"),
        }, f)
    p = subprocess.run(["launchctl", "bootstrap", domain, plist],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode:
        raise UserError("launchctl bootstrap failed: %s" % tail(p.stdout.decode(errors="replace")))
    return "Timer set: every %d min (launchd agent %s)." % (minutes, short(plist))


def systemd_timer(minutes):
    d = os.path.expanduser("~/.config/systemd/user")
    svc, tim = os.path.join(d, "ccsync.service"), os.path.join(d, "ccsync.timer")

    def ctl(*a):
        return subprocess.run(["systemctl", "--user"] + list(a),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    if not minutes:
        ctl("disable", "--now", "ccsync.timer")
        for p in (svc, tim):
            if os.path.exists(p):
                os.remove(p)
        ctl("daemon-reload")
        return "Timer removed."
    prog = install_bin()
    env = ""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env = 'Environment="CLAUDE_CONFIG_DIR=%s"\n' % os.environ["CLAUDE_CONFIG_DIR"]
    os.makedirs(d, exist_ok=True)
    with open(svc, "w", encoding="utf-8") as f:
        f.write('[Unit]\nDescription=ccsync\n\n[Service]\nType=oneshot\n%s'
                'ExecStart="%s" -I "%s" run --auto --reason timer --quiet\n'
                % (env, timer_python(), prog))
    with open(tim, "w", encoding="utf-8") as f:
        f.write("[Unit]\nDescription=ccsync every %d min\n\n[Timer]\nOnBootSec=5min\n"
                "OnUnitActiveSec=%dmin\n\n[Install]\nWantedBy=timers.target\n" % (minutes, minutes))
    ctl("daemon-reload")
    p = ctl("enable", "--now", "ccsync.timer")
    if p.returncode:
        raise UserError("systemctl failed: %s" % tail(p.stdout.decode(errors="replace")))
    return "Timer set: every %d min (systemd user timer ccsync.timer)." % minutes


USAGE = """usage: ccsync [status]
       ccsync run [<peer>...] [--dry] [--full]
       ccsync peer add <name> <user@host> [<user@host>...] | peer add <name> --dir <path>
       ccsync peer rm <name>
       ccsync auto start on|off | auto end on|off | auto every <minutes>|off | auto gap <minutes>
       ccsync config [check] | config set '<JSON merge patch>'
       ccsync test <peer> | test <user@host> [<user@host>...] | test --dir <path>
       ccsync detect
       ccsync log [<lines>]"""


def main(argv):
    from_command = argv[:1] == ["--from-command"]
    if from_command:
        argv = argv[1:]
    cmd, args = (argv[0], argv[1:]) if argv else ("status", [])
    try:
        refresh_bin()
        if cmd == "status":
            return cmd_status()
        if cmd == "run":
            return cmd_run(args, from_command)
        if cmd == "peer":
            return cmd_peer(args)
        if cmd == "auto":
            return cmd_auto(args)
        if cmd == "config":
            return cmd_config(args)
        if cmd == "log":
            return cmd_log(args)
        if cmd == "detect":
            return cmd_detect()
        if cmd == "test":
            return cmd_test(args)
        if cmd == "hook":
            return cmd_hook(args)
        if cmd in ("help", "-h", "--help"):
            print(USAGE)
            return 0
        raise UserError("unknown command %r\n%s" % (cmd, USAGE))
    except UserError as e:
        print("ccsync: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
