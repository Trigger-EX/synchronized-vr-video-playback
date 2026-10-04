# SyncVR

Synchronized video playback for Oculus Go fleets: Python server + dashboard (`server/`),
native Kotlin/C++ Go player (`player-android/`), docs in `docs/` (start with `docs/EXECUTION_PLAN.md`).
The old Unity project in `headset/` is legacy and being retired; don't build, test or extend it.

## Session hygiene

Commands
- Build: none for the server (Python). Go player APK (signed release, normally built in CI): `(cd player-android && ./gradlew --no-daemon :app:assembleRelease)`
- Test: `(cd server && python3 -m pytest -q)` and `(cd player-android && ./gradlew --no-daemon :core:test)`

Every message re-sends the whole conversation, so keep context small.

Starting
- At the start of every session, if docs/handoff.md exists on the current branch, read it first for context on the project, unless I explicitly say not to. Don't re-explore what it covers. If my first message gives no task (for example "go" or "continue"), resume from its Next step.
- Read files selectively by path. Search before opening large files.

Working
- Delegate verbose work and keep only the summaries here: Explore for code searches and documentation lookups, test-runner for tests, build-runner for builds. Never run builds, full test suites, or large log dumps in this session.
- When delegating to a subagent without its own model, pass one: haiku for searches, lookups and runs; sonnet for code changes; opus only for design or architecture questions.
- Keep replies short: what changed and what's next. No recaps.
- When I send several tasks in one message, do all of them in this session, in order. Don't move any to other sessions unless I ask.
- Don't switch this session's model or effort level, or suggest switching, unless I ask: it re-reads the whole conversation without the cache.

Planning
- Before starting a task, decide whether it needs planning. Plan first with the planner subagent when any of these hold: it touches more than 3 files; it needs a design or architecture decision; the approach is unclear or there are several reasonable ones; it involves concurrency, locking, memory ordering, security, data migrations, or public APIs; or a bug's cause isn't clear after a quick look. When unsure, plan.
- Skip the planner for small, clear changes. If I say "plan this", always use it.
- The planner doesn't see this conversation. Give it a self-contained brief: the goal, constraints and decisions from this conversation, and the relevant file paths.
- Save its plan to docs/plan.md, commit it, then implement it step by step. If you hit the same error twice, or the plan turns out wrong, send the planner what happened and ask for a revised plan.

Checkpoints
- Run the cp skill only when I type cp, checkpoint, handoff or wrap up, or when a hook message tells you to. Never checkpoint, suggest a new session, or end the session on your own, even when a task is finished.

Workflows
- Only run a dynamic workflow when I ask for one (the ultracode keyword, or "use a workflow") or ultracode is on.
- In every workflow, name a model for each stage: haiku for discovery, searches, and build or test runs; sonnet for implementation; opus only for design decisions or a final review. Use lower effort for routine stages where the workflow allows it.
- Keep each workflow as small as the task allows. When starting one, state its stages and roughly how many agents it will use in one line.

Pull requests
- Before a PR into the default branch is marked ready for review, delete docs/handoff.md and docs/plan.md in a final commit.

# Compact instructions
When compacting, keep the goal, decisions made, file paths touched, failing test names and errors, and the next step. Drop exploration that led nowhere.
