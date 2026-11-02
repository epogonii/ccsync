#!/usr/bin/env python3
# The half of ccsync that reads and writes one machine's Claude Code directory.
# The runner starts it on this machine and, over ssh, on the other one, which
# then needs nothing but python3 and rsync.
#
#   ccsync_side.py <op>            JSON request on stdin, JSON reply on stdout
#   ccsync_side.py pack            request on stdin, tar stream on stdout
#   ccsync_side.py unpack <b64>    request in argv, tar stream on stdin
import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import traceback
import uuid

PROTOCOL = 1
LOCK_STALE = 3600
SLACK = 300  # clocks of two machines can be a few minutes apart
DENY = {".credentials.json", ".claude.json"}
SKIP = {".DS_Store"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def home():
    return os.path.expanduser("~")


def claude_dir(req):
    d = req.get("claude_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return os.path.abspath(os.path.expanduser(d))


def data_dir(cdir):
    return os.path.join(cdir, "ccsync")


def encode(path, keep=""):
    # Claude Code names a project folder after its path with everything except
    # ASCII letters and digits turned into "-". JavaScript counts a character
    # outside the BMP twice, so it becomes two dashes.
    out = []
    for c in path:
        if ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c in keep:
            out.append(c)
        else:
            out.append("--" if ord(c) > 0xFFFF else "-")
    return "".join(out)


def map_name(name, pairs):
    # pairs of encoded prefixes (from, to), the longest "from" first
    for f, t in pairs:
        if name == f or name.startswith(f + "-"):
            return t + name[len(f):]
    return None


def matches(rel, include, exclude):
    parts = rel.split("/")
    if parts[-1] in DENY or parts[-1] in SKIP:
        return False
    for p in exclude:
        if fnmatch.fnmatchcase(rel, p) or any(fnmatch.fnmatchcase(c, p) for c in parts):
            return False
    if not include:
        return True
    return any(fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(parts[-1], p) for p in include)


def set_root(cdir, s):
    p = s["path"]
    if p == "~":
        return home()
    if p.startswith("~/"):
        return os.path.join(home(), p[2:])
    return os.path.normpath(os.path.join(cdir, p))


def safe_rel(rel):
    return bool(rel) and not rel.startswith("/") and all(
        p not in ("", ".", "..") for p in rel.split("/"))


def resolve(cdir, path, sets):
    """Local file for a manifest path: projects/<name>/memory/... or files/<set>/..."""
    if not safe_rel(path):
        return None
    parts = path.split("/")
    if parts[0] == "projects" and len(parts) >= 4 and parts[2] == "memory":
        return os.path.join(cdir, *parts)
    if parts[0] == "files" and len(parts) >= 3 and parts[1] in sets:
        s = sets[parts[1]]
        if s.get("only") and "/".join(parts[2:]) != s["only"]:
            return None
        return os.path.join(set_root(cdir, s), *parts[2:])
    return None


def file_info(path, with_hash):
    st = os.stat(path)
    digest = None
    if with_hash:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
    return [st.st_mtime, st.st_size, digest, st.st_mode & 0o777]


def scan_set(cdir, s, with_hash):
    root = set_root(cdir, s)
    inc, exc = s.get("include") or [], s.get("exclude") or []
    out = {}
    if s.get("only"):
        p = os.path.join(root, s["only"])
        if os.path.isfile(p) and not os.path.islink(p):
            out[s["only"]] = file_info(p, with_hash)
        return out
    for dirpath, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = sorted(
            d for d in dirs
            if not os.path.islink(os.path.join(dirpath, d))
            and not any(fnmatch.fnmatchcase(d, p) or fnmatch.fnmatchcase(prefix + d, p) for p in exc))
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = prefix + fn
            if os.path.islink(full) or not os.path.isfile(full) or not matches(rel, inc, exc):
                continue
            out[rel] = file_info(full, with_hash)
    return out


def scan_projects(cdir):
    proj = os.path.join(cdir, "projects")
    try:
        names = sorted(n for n in os.listdir(proj) if os.path.isdir(os.path.join(proj, n)))
    except FileNotFoundError:
        names = []
    memory = []
    for n in names:
        for dirpath, dirs, files in os.walk(os.path.join(proj, n, "memory")):
            dirs.sort()
            for fn in sorted(files):
                if fn == "MEMORY.md" or fn in SKIP:
                    continue
                memory.append(os.path.relpath(os.path.join(dirpath, fn), cdir).replace(os.sep, "/"))
    return names, memory


def state_path(cdir, peer_id):
    if not ID_RE.match(peer_id or ""):
        raise ValueError("bad machine id %r" % peer_id)
    return os.path.join(data_dir(cdir), "state", peer_id + ".json")


def read_state(cdir, peer_id):
    try:
        with open(state_path(cdir, peer_id), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def write_file(path, data, like=None, mode=None, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".ccsync-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if like:
        shutil.copystat(like, tmp)
    if mode is not None:
        os.chmod(tmp, mode)
    if mtime is not None:
        os.utime(tmp, (mtime, mtime))
    os.replace(tmp, path)


def backup(cdir, path, saved, rel=None):
    """Copy a file into replaced/<stamp>/ before it is changed. The first copy
    in a run is the one kept, it is the version from before the run."""
    t = os.path.join(saved, rel or os.path.relpath(path, cdir))
    if os.path.exists(t):
        return
    os.makedirs(os.path.dirname(t), exist_ok=True)
    shutil.copy2(path, t)


def rewriter(key, maps):
    """Rewrites "key":"<from>..." to "key":"<to>..." for every path mapping."""
    maps = sorted(((f, t) for f, t in maps), key=lambda m: -len(m[0]))
    if not maps or all(f == t for f, t in maps):
        return lambda x: x
    alts = "|".join(re.escape(f) for f, _ in maps)
    rx = re.compile('("%s"\\s*:\\s*")(%s)(?=[/"])' % (re.escape(key), alts))
    brx = re.compile(rx.pattern.encode())
    table = dict(maps)
    btable = {f.encode(): t.encode() for f, t in maps}

    def fix(x):
        if isinstance(x, bytes):
            return brx.sub(lambda m: m.group(1) + btable[m.group(2)], x)
        return rx.sub(lambda m: m.group(1) + table[m.group(2)], x)
    return fix


# ---- ops

def op_info(req):
    cdir = claude_dir(req)
    dd = data_dir(cdir)
    os.makedirs(dd, exist_ok=True)
    os.makedirs(os.path.join(home(), ".cache", "ccsync", "mirror"), exist_ok=True)
    idf = os.path.join(dd, "machine-id")
    try:
        with open(idf, encoding="utf-8") as f:
            mid = f.read().strip()
    except FileNotFoundError:
        mid = ""
    if not ID_RE.match(mid):
        mid = uuid.uuid4().hex
        with open(idf + ".tmp", "w", encoding="utf-8") as f:
            f.write(mid + "\n")
        os.replace(idf + ".tmp", idf)
    proj = os.path.join(cdir, "projects")
    return {
        "protocol": PROTOCOL,
        "machine_id": mid,
        "home": home(),
        "claude_dir": cdir,
        "projects": len(os.listdir(proj)) if os.path.isdir(proj) else 0,
        "host": socket.gethostname().split(".")[0],
        "python": sys.version.split()[0],
        "rsync": bool(shutil.which("rsync")),
    }


def op_lock(req):
    dd = data_dir(claude_dir(req))
    lock = os.path.join(dd, "lock")
    os.makedirs(dd, exist_ok=True)
    try:
        if time.time() - os.path.getmtime(lock) > LOCK_STALE:
            shutil.rmtree(lock, ignore_errors=True)
    except FileNotFoundError:
        pass
    try:
        os.mkdir(lock)
    except FileExistsError:
        try:
            with open(os.path.join(lock, "holder"), encoding="utf-8") as f:
                holder = f.read().strip()
        except OSError:
            holder = "?"
        return {"ok": False, "holder": holder}
    with open(os.path.join(lock, "holder"), "w", encoding="utf-8") as f:
        f.write(req.get("holder", "?"))
    return {"ok": True}


def op_unlock(req):
    lock = os.path.join(data_dir(claude_dir(req)), "lock")
    try:
        with open(os.path.join(lock, "holder"), encoding="utf-8") as f:
            if f.read().strip() != req.get("holder"):
                return {"ok": False}
    except OSError:
        return {"ok": False}
    shutil.rmtree(lock, ignore_errors=True)
    return {"ok": True}


def op_scan(req):
    cdir = claude_dir(req)
    sets = req.get("sets") or {}
    reply = {}
    if req.get("projects", True):
        reply["projects"], reply["memory"] = scan_projects(cdir)
    reply["sets"] = {sid: scan_set(cdir, s, req.get("hash", True)) for sid, s in sets.items()}
    if req.get("peer_id"):
        st = read_state(cdir, req["peer_id"])
        reply["state"] = {"base": st.get("base"), "scope": st.get("scope")}
        missing = []
        for p in st.get("manifest") or []:
            d = resolve(cdir, p, sets)
            if d and not os.path.lexists(d):
                missing.append(p)
        reply["missing"] = missing
    return reply


def drop_index_line(cdir, p, saved):
    parts = p.split("/")
    memdir = os.path.join(cdir, *parts[:3])
    target = "/".join(parts[3:])
    index = os.path.join(memdir, "MEMORY.md")
    if not os.path.exists(index):
        return
    with open(index, encoding="utf-8") as f:
        lines = f.read().splitlines()
    keep = []
    for line in lines:
        m = LINK_RE.search(line)
        if not (m and m.group(1) == target):
            keep.append(line)
    if len(keep) == len(lines):
        return
    backup(cdir, index, saved)
    write_file(index, ("\n".join(keep) + "\n").encode())


def op_delete(req):
    cdir = claude_dir(req)
    base = float(req["base"])
    dry = bool(req.get("dry"))
    sets = req.get("sets") or {}
    saved = os.path.join(data_dir(cdir), "replaced", req["stamp"])
    deleted, kept = [], []
    for p in req["paths"]:
        d = resolve(cdir, p, sets)
        if not d or not os.path.isfile(d):
            continue
        if os.path.getmtime(d) > base:
            kept.append(p)
            continue
        deleted.append(p)
        if dry:
            continue
        t = os.path.join(saved, *p.split("/"))
        os.makedirs(os.path.dirname(t), exist_ok=True)
        shutil.move(d, t)
        parts = p.split("/")
        if parts[0] == "projects":
            drop_index_line(cdir, p, saved)
            stop = os.path.join(cdir, *parts[:3])
        else:
            stop = set_root(cdir, sets[parts[1]])
        # folders the deletion left empty go too, up to the set or memory folder
        parent = os.path.dirname(d)
        while parent.startswith(stop + os.sep):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
    return {"deleted": deleted, "kept": kept}


def op_merge(req):
    """Merges a mirror of the other machine's projects/ and history.jsonl into
    this machine. Only adds, never loses:
      - a missing file is copied
      - a transcript that grew over there gets the new tail appended here
      - a transcript continued on both machines gets the lines it lacks
      - a memory note is replaced only if the copy here was not touched since
        the last sync; MEMORY.md takes the entries it lacks
      - history.jsonl becomes the union of both, sorted by time
    Anything else that differs is a conflict and stays as it is."""
    cdir = claude_dir(req)
    mirror = req["mirror"]
    base = float(req.get("base") or 0)
    skip_before = float(req.get("skip_before") or 0)
    dry = bool(req.get("dry"))
    want = req["want"]
    maps = [tuple(m) for m in req["maps"]]
    saved = os.path.join(data_dir(cdir), "replaced", req["stamp"])
    pairs = sorted(((encode(f), encode(t)) for f, t in maps), key=lambda p: -len(p[0]))
    cwd_fix = rewriter("cwd", maps)
    project_fix = rewriter("project", maps)
    stats = dict.fromkeys(
        ("copied", "same", "skipped", "appended", "replaced", "merged", "index_lines"), 0)
    conflicts, merged, unmapped = [], [], []

    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def merge_index(s, d, replaced):
        with open(d, encoding="utf-8") as f:
            have = f.read().splitlines()
        by_target = {}
        for i, line in enumerate(have):
            m = LINK_RE.search(line)
            if m:
                by_target.setdefault(m.group(1), i)
        have_set = set(have)
        changed = False
        with open(s, encoding="utf-8") as f:
            theirs = f.read().splitlines()
        for line in theirs:
            if not line.strip() or line in have_set:
                continue
            m = LINK_RE.search(line)
            t = m.group(1) if m else None
            # an entry whose note is gone over there would only dangle here
            if t and safe_rel(t) and not os.path.exists(os.path.join(os.path.dirname(s), *t.split("/"))):
                continue
            if t in by_target:
                if t in replaced:
                    have[by_target[t]] = line
                    changed = True
                    stats["index_lines"] += 1
                continue
            have.append(line)
            have_set.add(line)
            if t:
                by_target[t] = len(have) - 1
            changed = True
            stats["index_lines"] += 1
        if changed and not dry:
            backup(cdir, d, saved)
            write_file(d, ("\n".join(have) + "\n").encode())

    src_proj = os.path.join(mirror, "projects")
    names = sorted(os.listdir(src_proj)) if os.path.isdir(src_proj) else []
    for name in names:
        new_name = map_name(name, pairs)
        if new_name is None:
            unmapped.append(name)
            continue
        sroot = os.path.join(src_proj, name)
        droot = os.path.join(cdir, "projects", new_name)
        index, replaced = None, set()
        for dirpath, dirs, files in os.walk(sroot):
            dirs.sort()
            rel_dir = os.path.relpath(dirpath, sroot)
            parts = [] if rel_dir == "." else rel_dir.split(os.sep)
            in_memory = bool(parts) and parts[0] == "memory"
            if not parts:
                dirs[:] = [d for d in dirs if (want["memory"] if d == "memory" else want["transcripts"])]
                if not want["transcripts"]:
                    files = []
            for fn in sorted(files):
                if fn in SKIP:
                    continue
                s = os.path.join(dirpath, fn)
                d = os.path.normpath(os.path.join(droot, rel_dir, fn))
                if in_memory and len(parts) == 1 and fn == "MEMORY.md" and os.path.exists(d):
                    index = (s, d)
                    continue
                if os.path.exists(d) and os.path.getmtime(s) < skip_before - SLACK:
                    stats["skipped"] += 1
                    continue
                jsonl = fn.endswith(".jsonl")
                new = read(s)
                if jsonl:
                    new = cwd_fix(new)
                if not os.path.exists(d):
                    if not dry:
                        write_file(d, new, like=s)
                    stats["copied"] += 1
                    continue
                old = read(d)
                if old == new or (jsonl and old.startswith(new)):
                    stats["same"] += 1
                elif jsonl and new.startswith(old):
                    if not dry:
                        with open(d, "ab") as f:
                            f.write(new[len(old):])
                    stats["appended"] += 1
                elif in_memory and os.path.getmtime(d) <= base < os.path.getmtime(s):
                    if not dry:
                        backup(cdir, d, saved)
                        write_file(d, new, like=s)
                    replaced.add("/".join(parts[1:] + [fn]))
                    stats["replaced"] += 1
                # a live session may have half a line at the end, leave it be
                elif jsonl and old.endswith(b"\n"):
                    have = set(old.splitlines())
                    extra = [line for line in new.splitlines() if line and line not in have]
                    if extra:
                        if not dry:
                            backup(cdir, d, saved)
                            with open(d, "ab") as f:
                                f.write(b"\n".join(extra) + b"\n")
                        stats["merged"] += 1
                        merged.append(os.path.relpath(d, cdir))
                    else:
                        stats["same"] += 1
                else:
                    conflicts.append(os.path.relpath(d, cdir))
        if index:
            merge_index(index[0], index[1], replaced)

    history = None
    if want["history"]:
        history = merge_history(cdir, mirror, project_fix, dry)
    return {"stats": stats, "conflicts": conflicts, "merged": merged,
            "unmapped": unmapped, "history": history}


def merge_history(cdir, mirror, fix, dry):
    hs = os.path.join(mirror, "history.jsonl")
    hd = os.path.join(cdir, "history.jsonl")
    if not os.path.exists(hs):
        return None

    def ts(line):
        try:
            return json.loads(line).get("timestamp", 0) or 0
        except Exception:
            return 0

    def stamp_of(path):
        try:
            st = os.stat(path)
            return st.st_size, st.st_mtime_ns
        except FileNotFoundError:
            return None

    # open sessions append to history.jsonl at any time: if it changed while
    # we were merging, start over rather than drop what they wrote
    for _ in range(5):
        seen = stamp_of(hd)
        lines = {}
        before = 0
        if os.path.exists(hd):
            with open(hd, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if line:
                        before += 1
                        lines.setdefault(line, None)
        with open(hs, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    lines.setdefault(fix(line), None)
        merged = sorted(lines, key=ts)
        if dry or len(merged) == before:
            return [before, len(merged)]
        tmp = hd + ".ccsync-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(merged) + "\n")
        if stamp_of(hd) == seen:
            os.replace(tmp, hd)
            return [before, len(merged)]
        os.remove(tmp)
    return "busy"


def op_pack(req):
    cdir = claude_dir(req)
    sets = req["sets"]
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for sid, rel in req["files"]:
            p = resolve(cdir, "files/%s/%s" % (sid, rel), sets)
            if not p or os.path.islink(p) or not os.path.isfile(p):
                continue
            ti = tar.gettarinfo(p, arcname=sid + "/" + rel)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            with open(p, "rb") as f:
                tar.addfile(ti, f)


def op_unpack(req):
    cdir = claude_dir(req)
    sets = req["sets"]
    saved = os.path.join(data_dir(cdir), "replaced", req["stamp"])
    written = []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
        for m in tar:
            if not m.isfile():
                continue
            sid, _, rel = m.name.partition("/")
            d = resolve(cdir, "files/%s/%s" % (sid, rel), sets)
            if not d or os.path.islink(d):
                continue
            data = tar.extractfile(m).read()
            if os.path.isfile(d):
                backup(cdir, d, saved, rel=os.path.join("files", sid, *rel.split("/")))
            write_file(d, data, mode=m.mode & 0o777, mtime=m.mtime)
            written.append(sid + "/" + rel)
    return {"written": written}


def op_after(req):
    cdir = claude_dir(req)
    env = dict(os.environ, CCSYNC_CLAUDE_DIR=cdir)
    results = []
    for cmd in req.get("commands") or []:
        try:
            p = subprocess.run(cmd, shell=True, cwd=home(), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
            results.append({"cmd": cmd, "code": p.returncode,
                            "out": p.stdout.decode(errors="replace")[-2000:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "code": -1, "out": "timed out after 300 s"})
    return {"results": results}


def op_prune(req):
    cdir = claude_dir(req)
    now = time.time()
    cutoff = now - float(req.get("keep_days", 30)) * 86400
    removed = 0
    for sub in ("replaced", "logs"):
        root = os.path.join(data_dir(cdir), sub)
        for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            p = os.path.join(root, n)
            if os.path.getmtime(p) >= cutoff:
                continue
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            removed += 1
    helpers = os.path.join(home(), ".cache", "ccsync")
    me = os.path.basename(__file__)
    for n in os.listdir(helpers) if os.path.isdir(helpers) else []:
        p = os.path.join(helpers, n)
        if (n.startswith("side-") and n.endswith(".py") and n != me
                and os.path.getmtime(p) < now - 30 * 86400):
            os.remove(p)
    return {"removed": removed}


def op_save(req):
    cdir = claude_dir(req)
    st = read_state(cdir, req["peer_id"])
    st.update(req["state"])
    write_json(state_path(cdir, req["peer_id"]), st)
    return {}


OPS = {
    "info": op_info, "lock": op_lock, "unlock": op_unlock, "scan": op_scan,
    "delete": op_delete, "merge": op_merge, "after": op_after, "prune": op_prune,
    "save": op_save,
}


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if op == "unpack":
            req = json.loads(base64.b64decode(sys.argv[2]).decode())
            reply = op_unpack(req)
        else:
            raw = sys.stdin.read()
            req = json.loads(raw) if raw.strip() else {}
            if op == "pack":
                op_pack(req)
                return 0
            if op not in OPS:
                sys.stderr.write("ccsync_side: unknown op %r\n" % op)
                return 2
            reply = OPS[op](req)
    except Exception:
        traceback.print_exc()
        return 1
    sys.stdout.write(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
