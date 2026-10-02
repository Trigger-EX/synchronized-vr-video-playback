---
name: handoff
description: Write docs/handoff.md so a fresh session can continue this work. Use when the user says handoff or wrap up, or when the session-hygiene rules in CLAUDE.md say to end the session.
---
1. Write docs/handoff.md, under 300 words, with these headings:
   - Goal (1-2 sentences)
   - Decisions and constraints (exact names, versions, values)
   - Current state (what's done, branch name, PR link if any, files touched)
   - Open questions
   - Next step (the first thing the next session should do)
   Leave out dead ends and corrected mistakes.
2. Commit "Update handoff" and push the current branch.
3. Reply with only:
   - Branch: <branch>
   - Suggested model: sonnet if the next step is implementing an existing plan or routine work; opus if it needs design decisions or subtle debugging. Say to send /model <name> as the first message of the next session.
   - Start the next session on that branch and send: continue: read docs/handoff.md and resume from Next step
   - Or from a terminal: git fetch && git checkout <branch> && claude --cloud "Read docs/handoff.md and resume from Next step"
