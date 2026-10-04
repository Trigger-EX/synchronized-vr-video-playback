---
name: cp
description: Save a checkpoint to docs/handoff.md so a fresh session can continue this work. Use only when the user types cp, checkpoint, handoff or wrap up, or when a hook message says to.
---
1. Write docs/handoff.md, under 300 words, with these headings:
   - Goal (1-2 sentences)
   - Decisions and constraints (exact names, versions, values)
   - Current state (what's done, branch name, files touched)
   - Open questions
   - Next step (the first thing the next session should do)
   Leave out dead ends and corrected mistakes.
2. Commit "Checkpoint" on the current branch. In a cloud session (environment variable CLAUDE_CODE_REMOTE is "true"), push it to the current branch. In a local session, don't push unless I ask. Never create branches or open pull requests.
3. Reply with exactly this, then carry on normally if I keep working:

# **CHECKPOINT SAVED**
- Branch: <current branch>
- Optional: for a fresh start, run /clear (local) or start a new session on this branch (cloud), then send "go". Or just keep going here.
