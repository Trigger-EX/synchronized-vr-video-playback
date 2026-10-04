---
name: cp
description: Save a checkpoint to docs/handoff.md so a fresh session can continue this work. Use only when the user types cp, checkpoint, handoff or wrap up, or when a hook message says to.
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
   - The working branch is the branch this session started from. If it's the default branch, or you can't tell, don't open a PR; use the Cloud, not merged reply.
   - Otherwise open a PR from the session branch into the working branch titled "Checkpoint: <goal>", or reuse the open one, and merge it. Use a built-in GitHub tool if one can merge pull requests; otherwise, if gh is installed, use the REST API: gh api -X PUT repos/<owner>/<repo>/pulls/<number>/merge -f merge_method=merge. Don't delete the branch. Never merge into the default branch. If merging fails, leave the PR open and use the Cloud, not merged reply.
5. Reply in exactly one of these formats. If I keep working in this session afterwards, carry on normally.

Local:
# **CHECKPOINT SAVED**
- Optional: /clear, then send "go" for a fresh start. Or just keep going here.

Cloud, merged:
# **CHECKPOINT SAVED**
- Merged into: <working branch>
- Optional: start a new cloud session from <working branch> and send "go" for a fresh start. Or just keep going here.

Cloud, not merged:
# **CHECKPOINT SAVED**
- Branch: <session branch> (PR #<n> into <working branch>, if one was opened)
- Optional: start a new cloud session from <session branch> and send "go" for a fresh start; later checkpoints will merge into it. Or just keep going here.
