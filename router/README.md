# Router Workspace

This folder is the clean, Git-friendly workspace for the selective-delegation
router project.

## Included Files

- `experiment_plan_v3.md`: current locked experiment plan
- `EXPERIMENT_GUIDE_explained.md`: explanation-oriented project guide
- `RUNBOOK_local_to_server.md`: operational runbook from local development to
  remote execution
- `data/schema_v1_1.md`: locked trajectory schema
- `data/preflight_checklist.md`: gated checklist before paid distillation

## Sync Rule

- If a file should be easy to push and keep public, place it under `router/`
- Root `README.md` only acts as the repository entry point
- Local experiments, baselines, scripts, and scratch artifacts can stay outside
  this folder without affecting the clean public snapshot

## Suggested Daily Workflow

```bash
git status
git add router README.md .gitignore
git commit -m "docs: update router workspace"
git push
```
