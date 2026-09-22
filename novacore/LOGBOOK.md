# NovaCore implementation log

## 2026-09-22 — work order created

**Decision:** Create a new isolated `novacore/` directory and leave all existing
kernel, build, documentation, and trust files untouched.

**Reason:** Other agents are actively changing those paths. The requested model
runtime needs no kernel modification at this stage, and the project's standing
brief requires one scoped work order at a time.

**Status:** Documentation and implementation are pending. Test results will be
recorded in `VALIDATION.md` only after commands are run.
