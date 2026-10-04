# SyncVR

Synchronized video playback for Oculus Go fleets: Python server + dashboard (`server/`),
Unity 2019.4 headset app (`headset/`), docs in `docs/` (start with `docs/EXECUTION_PLAN.md`).

## Session hygiene

Commands
- Build: none for the server (Python). Headset APK needs a local Unity 2019.4.41f2: `Unity -batchmode -quit -projectPath headset -executeMethod SyncVR.EditorTools.SyncVRBuild.BuildFromCommandLine -logFile build.log`
- Test: `(cd server && python3 -m pytest -q)` and `(cd headset && mcs -out:/tmp/enginetests.exe "Tests~/EngineTests.cs" Assets/SyncVR/Scripts/SyncEngine.cs Assets/SyncVR/Scripts/Messages.cs Assets/SyncVR/Scripts/ClockSync.cs && mono /tmp/enginetests.exe)`

Every message re-sends the whole conversation, so keep context small.

Starting
- At the start of every session, if docs/handoff.md exists on the current branch, read it first for context on the project, unless I explicitly say not to. Don't re-explore what it covers. If my first message gives no task (for example "go" or "continue"), resume from its Next step.
- Read files selectively by path. Search before opening large files.

Working
- Delegate verbose work and keep only the summaries here: Explore for code searches and documentation lookups, test-runner for tests, build-runner for builds. Never run builds, full test suites, or large log dumps in this session.
- When delegating to a subagent without its own model, pass one: haiku for searches, lookups and runs; sonnet for code changes; inherit only for design or architecture questions.
- For work touching more than 3 files, write numbered steps to docs/plan.md first and commit it.
- Keep replies short: what changed and what's next. No recaps.
- Don't suggest switching this session's model or effort level, because that re-reads the whole conversation without the cache.

Workflows
- Only run a dynamic workflow when I ask for one (the ultracode keyword, or "use a workflow") or ultracode is on.
- In every workflow, name a model for each stage: haiku for discovery, searches, and build or test runs; sonnet for implementation; opus only for design decisions or a final review. Use lower effort for routine stages where the workflow allows it.
- Keep each workflow as small as the task allows. When starting one, state its stages and roughly how many agents it will use in one line.

Checkpoints
- Run the cp skill only when I type cp, checkpoint, handoff or wrap up, or when a hook message tells you to. Never checkpoint, suggest a new session, or end the session on your own, even when a task is finished.

Several tasks
- When I send several tasks in one message, do all of them in this session, in order.

# Compact instructions
When compacting, keep the goal, decisions made, file paths touched, failing test names and errors, and the next step. Drop exploration that led nowhere.
