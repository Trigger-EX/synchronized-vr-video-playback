---
name: cp
description: Save a checkpoint to docs/handoff.md so a fresh session can continue this work. Use when the user types cp, checkpoint, handoff, or wrap up, or when the session-hygiene rules in CLAUDE.md say to end the session.
---
1. Write docs/handoff.md, under 300 words, with these headings:
   - Goal (1-2 sentences)
   - Decisions and constraints (exact names, versions, values)
   - Current state (what's done, branch name, PR link if any, files touched)
   - Open questions
   - Next step (the first thing the next session should do)
   Leave out dead ends and corrected mistakes.
2. Commit "Checkpoint".
3. If the environment variable CLAUDE_CODE_REMOTE is not "true", this is a local session: don't push unless I ask, and use the Local reply.
4. If CLAUDE_CODE_REMOTE is "true", this is a cloud session:
   - Push the session branch.
   - The working branch is the branch this session started from. If it's the repository's default branch, or you can't tell which branch it was, don't merge; use the Cloud, not merged reply.
   - Otherwise open a PR from the session branch into the working branch titled "Checkpoint: <goal>", or reuse the open one, and merge it with: gh pr merge --merge --delete-branch. Never merge into the default branch. If merging fails, leave the PR open and use the Cloud, not merged reply.
5. Reply in exactly one of these formats and nothing else. Suggested model: sonnet if the next step is implementing an existing plan or routine work; opus if it needs design decisions or subtle debugging.

Local:
# **CHECKPOINT SAVED**
- Suggested model: <model>
- Next: /clear, then send "go". Send /model <name> first if switching models.

Cloud, merged:
# **CHECKPOINT SAVED**
- Merged into: <working branch>
- Suggested model: <model>
- Next: start a new cloud session from <working branch> and send "go". From your terminal, stay on <working branch> and run: claude --cloud "go"

Cloud, not merged:
# **CHECKPOINT SAVED**
- Branch: <session branch> (PR #<n> into <working branch>, if one was opened)
- Suggested model: <model>
- Next: start a new cloud session from <session branch> and send "go". It becomes your working branch, and later checkpoints merge into it.
