---
description: Show sync state, or sync now with your other machines
argument-hint: "[status | run [<peer>] [--dry] [--full] | test <peer> | auto start|end on|off | auto every <minutes>|off | config | log]"
allowed-tools: Bash(python3:*)
disable-model-invocation: true
---

!`python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/ccsync.py" --from-command $ARGUMENTS`

The command above already did the work and its output is the answer. Reply
with that text exactly as printed: same words, same line breaks, no code
fence, no summary, no rewording, no follow-up suggestions, nothing added
before or after. The only exception: if it printed an error, copy the error
and add one line saying what to run instead (`/ccsync:setup` changes the
settings).
