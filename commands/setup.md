---
description: Set up ccsync step by step, or change where, what and when it syncs
argument-hint: "[what to change]"
allowed-tools: Bash(python3:*), AskUserQuestion
---

What ccsync sees on this machine (output of its `detect` command):

!`python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/ccsync.py" detect`

The user wants to set up ccsync or change its settings. Their words, if any: $ARGUMENTS

Walk them through it as a short conversation. For the whole walk-through:

- Use the user's language and plain words. One step at a time.
- Ask every choice with the AskUserQuestion tool: up to 4 questions per call,
  2 to 4 options each, what an option means goes in its description. The user
  can always type an answer of their own.
- Run the CLI exactly as the `cli` field above shows, for example
  `<cli> test me@laptop.local`.
- Change settings only with `<cli> config set '<JSON merge patch>'` (`null`
  removes a key). Never edit config.json directly: `config set` checks the
  result and refuses a broken config.
- Never show JSON to the user, describe settings in words.
- If `problems` above is not empty, explain those first and offer to fix them.

If `config.peers` already lists machines, first ask what to change (add a
machine, what is synced, which way, when, remove a machine, check that it
works) and do only those steps. Otherwise go through all of them.

## 1. The other machine

Ask how to reach it. Build the options only from the `detect` output, never
from memory or earlier conversation: the user is choosing, not confirming a
guess. Offer up to three hosts from `ssh_hosts` (label: the alias,
description: user@hostname) and say they can type `user@host` or an IP
address instead. With no `ssh_hosts`, the options are "Another machine over
ssh" (they type the address next) and "A folder or a disk". A machine can have more than one address (its name on the
home network, a VPN address); ccsync tries them in order, so ask whether there
is a second one.

Check it before saving anything: `<cli> test <address> [<second address>]`.
If it fails, show its "What to do" line and ask: try another address, fix it
and test again, or save anyway (for example a laptop that is asleep right now).
The other machine needs only ssh with a key, python3 and rsync. ccsync copies
its helper there by itself, nothing has to be installed.

Name the machine with one short word, suggest the alias or the host name in
lower case. A mounted disk or any folder can be a peer too:
`<cli> test --dir /path`, saved as `"dir"` instead of `"ssh"`.

## 2. What to sync

Ask with multiSelect, split so that no question has more than 4 options:

- Claude Code data: sessions (`claude --resume` finds them on both machines),
  memory notes (what Claude remembers per project), prompt history (what the
  up arrow brings back). Mention the sizes from `claude_data`.
- The user's setup: items from `candidates`, each with its size and the `what`
  text. Keep the warnings: settings.json holds paths and hooks that must work
  on the other machine too; hooks are code that runs by itself.
- If `other_in_claude_dir` has folders the user evidently made (scripts, a
  status line), offer them in another question. Skip plugin data and backups.

Then ask whether deleting a memory note or a synced file on one machine should
delete it on the other too. A deleted or replaced copy is kept for 30 days in
`~/.claude/ccsync/replaced/` either way.

Config: `sync.transcripts`, `sync.memory`, `sync.history`,
`sync.memory_deletions` (true or false). Each chosen item becomes a set under
`files`: a folder `{"path": "skills"}`, a single file `{"file": "CLAUDE.md"}`,
plus `"delete": true` when deletions should carry over. Paths are relative to
`~/.claude`, or start with `~/` for anything else in the home folder. A set can
narrow itself with `include` / `exclude` glob lists.

## 3. Which way

Options: both ways (the default); only from here to there (`"direction":
"push"`, the other machine is a backup); only from there to here
(`"direction": "pull"`).

## 4. Which projects

Options: all projects; only some (ask for folders such as `~/work/app` or
`~/work/*`, they go to `projects.include`); all except some
(`projects.exclude`).

A project is matched by its path under the home folder: `~/work/app` here is
`~/work/app` there, even with another user name or OS. Only if the user says a
project lives somewhere else on the other machine, map it on the peer:
`"paths": {"~": "~", "~/work": "~/src"}`.

`sync`, `files` and `projects` are shared by all machines. When one machine
needs something different, put it on the peer: `peers.<name>.sync` overrides
single keys, `peers.<name>.files` lists the sets to use, `peers.<name>.projects`
replaces the shared filter.

## 5. When

multiSelect: when a Claude Code session starts; when a session ends; on a
timer; only when I run `/ccsync run`. For the timer ask how often (15, 30 or 60
minutes). Automatic syncs run in the background, at most once in 15 minutes
per machine, and stay quiet when the other machine is off.

Config: `auto.on_start`, `auto.on_end`, `auto.every_minutes` (0 means no
timer; `config set` installs or removes the timer by itself).

## 6. Save

Sum up the choices in a few plain lines and ask to confirm. Then save them with
one `config set`, for example:

    <cli> config set '{"peers": {"laptop": {"ssh": ["me@laptop.local", "me@10.0.0.4"]}},
      "sync": {"transcripts": true, "memory": true, "history": true, "memory_deletions": true},
      "files": {"skills": {"path": "skills", "delete": true}, "claude-md": {"file": "CLAUDE.md"}},
      "auto": {"on_start": true, "every_minutes": 30}}'

If it answers "not saved", fix what it names and try again.

## 7. Try it

Run `<cli> run <name> --dry` and tell the user in two or three lines what would
happen. Then ask whether to sync for real now: `<cli> run <name> --wait 100`
with a Bash timeout of 120000. The first sync compares everything, so it can
take a few minutes; if it is still running after that, it carries on in the
background. End with one line: `/ccsync` shows the state, `/ccsync:setup`
changes it.
