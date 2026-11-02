# ccsync

Keeps Claude Code in step between your machines. Sessions, memory notes,
prompt history and any files you pick go both ways, straight over ssh: no
cloud bucket, no git repository in between, nothing to install on the other
machine but `python3` and `rsync`.

- A session continued on two machines is merged, not overwritten: each side
  gets the lines it lacks.
- Project paths are rewritten for the other machine, so a session from
  `/Users/me/work/app` resumes as `/home/me/work/app` on the Linux box.
- A memory note changed on one machine since the last sync replaces the other
  copy; a note changed on both is left alone and reported.
- Deleting a note or a synced file can carry over to the other machine.
- Nothing is lost silently: every replaced or deleted copy is kept for 30 days
  in `~/.claude/ccsync/replaced/`.

## Install

```
/plugin marketplace add epogonii/ccsync
/plugin install ccsync@ccsync
```

Then run `/ccsync:setup`. It asks which machine to sync with and checks that it
answers, what to sync, which way, which projects and when, then shows a dry run
before anything is written.

The other machine needs ssh with a key (no password prompt), `python3` 3.9 or
newer and `rsync`. ccsync copies a small helper to `~/.cache/ccsync/` there by
itself. Install the plugin on the other machine as well only if you want to
start syncs from there too.

## Commands

| Command | What it does |
|---|---|
| `/ccsync` | State of every peer: last sync, what it brought, conflicts |
| `/ccsync run [<peer>] [--dry]` | Sync now; `--dry` shows what would happen and writes nothing |
| `/ccsync run <peer> --full` | Compare every file, not only those changed since the last sync |
| `/ccsync test <peer>` | Check that the peer answers and has what it needs |
| `/ccsync auto start on`, `auto end on` | Sync in the background when a session starts or ends |
| `/ccsync auto every 30`, `auto every off` | A timer (launchd on macOS, a systemd user timer on Linux) |
| `/ccsync log` | The end of today's log |
| `/ccsync:setup [what to change]` | Guided setup, also for later changes |

Outside Claude Code the same commands work from a terminal:
`python3 ~/.claude/plugins/cache/ccsync/ccsync/<version>/scripts/ccsync.py run`.

## How the merge works

Each run mirrors the selected part of one machine into
`~/.cache/ccsync/mirror/<machine id>/` on the other with rsync, so only what
changed travels, and then merges the mirror in:

| | Rule |
|---|---|
| A file only one side has | copied over |
| A session that grew on one side | the new tail is appended |
| A session continued on both sides | the lines a side lacks are appended |
| A memory note changed on one side since the last sync | that version wins, the old one goes to `replaced/` |
| A memory note changed on both sides | left alone, reported as a conflict |
| `MEMORY.md` | entries the other side has are added; a dangling entry is not |
| `history.jsonl` | union of both, sorted by time |
| Files in your sets | the newer copy wins, the one it replaces goes to `replaced/` |
| A note or set file deleted on one side | deleted on the other, unless it changed there since the last sync |

Two machines never sync at the same time: each run holds a lock on both.

## Config

`/ccsync:setup` writes `~/.claude/ccsync/config.json`. It can also be edited by
hand; `/ccsync config check` validates it. A full example:

```json
{
  "peers": {
    "laptop": {
      "ssh": ["me@laptop.local", "me@10.0.0.4"],
      "direction": "both"
    },
    "backup-disk": {
      "dir": "/Volumes/backup/me",
      "direction": "push",
      "files": ["skills"]
    }
  },
  "sync": {"transcripts": true, "memory": true, "history": true, "memory_deletions": true,
           "plugins": true},
  "projects": {"include": [], "exclude": ["~/clients/*"]},
  "files": {
    "skills": {"path": "skills", "delete": true},
    "claude-md": {"file": "CLAUDE.md"},
    "scripts": {"path": "~/bin", "include": ["*.sh", "*.py"], "exclude": ["__pycache__"]}
  },
  "after": [],
  "auto": {"on_start": true, "on_end": false, "every_minutes": 0, "min_gap_minutes": 15},
  "keep_replaced_days": 30
}
```

**peers.** `ssh` is one address or a list tried in order (aliases from
`~/.ssh/config` work). `dir` makes a peer of a folder holding a home folder,
such as a mounted disk. `direction` is `both`, `push` (only to the peer) or
`pull` (only from it). `paths` maps folders here to folders there and defaults
to `{"~": "~"}`; add `"~/work": "~/src"` when projects live elsewhere on the
other machine. `sync`, `files` (a list of set names) and `projects` set on a
peer override the shared ones. Also: `python` (the interpreter there),
`claude_dir` (when it is not `~/.claude` there), `after`, `ssh_options`.

**sync.** What of Claude Code's own data travels. `memory_deletions` carries
deleted memory notes over. `plugins` (off by default) makes each machine
install the plugins the other one has, with `claude plugin install` and the
marketplace they come from; it never uninstalls, and a plugin whose
marketplace is a local folder is left out.

**projects.** Globs over project paths, like `~/work/*`. Claude Code names a
project folder after its path with `/` turned into `-`, so `*` also matches
deeper folders.

**files.** Named sets. `path` is a folder, `file` a single file, both relative
to `~/.claude` or starting with `~/`. `include` and `exclude` are globs matched
against the path inside the set and against each name in it. `delete` carries
deletions over, `direction` narrows the peer's direction for this set.
`.credentials.json` and `.claude.json` never travel.

**after.** Shell commands run on both machines after each sync, for example to
reload something the sync changed.

**auto.** `on_start` and `on_end` start a background sync from the session
hooks, `every_minutes` keeps a timer, `min_gap_minutes` keeps automatic syncs
apart. An automatic sync tries a sleeping peer once and stays quiet about it;
a real failure or a new conflict shows up when the next session starts.

## Good to know

- ssh runs with `BatchMode=yes` (no password prompts) and
  `StrictHostKeyChecking=accept-new`, so the first connection to a new host
  trusts its key. Put `StrictHostKeyChecking=yes` in `ssh_options` to refuse
  that.
- `after` commands and synced `hooks` are code that runs on the other machine.
- Clocks should roughly agree: "newer" means a later modification time, with
  five minutes of slack for the rules that use the last sync time.
- A cloned machine shares `~/.claude/ccsync/machine-id` with its original;
  delete that file on the clone.
- macOS and Linux. Windows is not supported.

## Tests

```
python3 -m unittest discover -s test
```

The tests sync two throwaway home folders on one machine, no ssh needed.

## License

MIT
