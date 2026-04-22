# Case Study — `fix-git`

A smoke run through the Terminal-Bench 2.0 `fix-git` task that illustrates what
each layer of our pipeline does and where failure can occur. The pipeline and
the routing decisions are clean; the worker model makes a subtle semantic
mistake while resolving a git merge conflict. Reward = 0.

- **Task**: the user made changes to their personal site on some non-master
  branch, then checked out `master`. The hidden work must be found (in
  `reflog`) and merged back into `master` such that the resulting files
  `/app/personal-site/_includes/about.md` and
  `/app/personal-site/_layouts/default.html` match the reference versions in
  `/app/resources/patch_files/`.
- **Pipeline config (Direct-Qwen-7B baseline)**:
  - Planner model: `Qwen/Qwen2.5-7B-Instruct` (untrained, local vLLM)
  - Router model: `Qwen/Qwen2.5-7B-Instruct` (same)
  - Worker pool: full 9-model heterogeneous pool
- **Max attempts**: 15 planner steps; 15 SubAgent shell steps per delegation.

## Planner decomposition (3 subtasks)

The Qwen-7B planner produced a sensible decomposition:

| Subtask | Router pick `(model, skill)` | Purpose |
|---|---|---|
| `t1` | `gemini-2.5-flash / read_document` | Look up generic git recovery guidance |
| `t2` | `gemini-3-flash-preview / direct_answer` | Narrate the recovery steps |
| `t3` | **`claude-sonnet-4-6 / execute_shell`** | Actually run shell commands inside the container |

Note the router correctly pushes the only environment-affecting subtask to a
shell-capable model. The two earlier subtasks are advisory; they contribute
context to the planner but do not touch the container.

## SubAgent execution (Docker, 12 steps)

See `commands.log` for the raw transcript. Compressed narrative:

1. `git status` → clean working tree.
2. `git branch --all --verbose` → single `master` branch; nothing obvious.
3. `git log --graph --all` → master has normal history.
4. `git reflog` → **finds** `650dba4 — "Move to Stanford"`, a dangling commit
   that was abandoned by an earlier `checkout master`.
5. `git show --stat 650dba4` → confirms it touches `_includes/about.md`
   and `_layouts/default.html`, exactly the two files the task cares about.
6. `git branch recover-changes 650dba4` → anchors the commit under a name.
7. `git merge 650dba4` → **conflict** in `_includes/about.md`. The commit
   introduces text at the same location as existing master content.
8. SubAgent hand-authors a Python script that rewrites the file's contents.
9. It commits `3255abe — "Restore Stanford content"` and reports `status=done`.

## Why the verifier failed

The manual resolution is **semantically wrong**. The test expects the
`_includes/about.md` file to read:

```
I am a Postdoctoral Researcher at Stanford CS.
```

because this is the content encoded in commit `650dba4`. The SubAgent's script
instead kept the OLD text ("I am a sixth year PhD student at …"), rewording
it slightly as "I am a sixth PhD candidate at …". MD5-hash comparison in
`test_outputs.py` therefore fails and `reward.txt` is written as `0`.

The correct resolution was to accept the incoming version wholesale, e.g.:

```bash
git merge -X theirs 650dba4
# or
git checkout 650dba4 -- _includes/about.md _layouts/default.html
git commit -m "Restore Stanford content"
```

## What this tells us

1. **Routing is correct.** Qwen-7B's router directs every subtask to a
   plausible `(model, skill)` pair; the only "real" work goes to a
   shell-capable model (Sonnet) as expected.
2. **Planner decomposition is correct.** Three steps, escalating from doc
   lookup to narrated plan to execution.
3. **Worker execution is fragile on subtle git semantics.** Even a strong
   model (Claude-Sonnet-4.6) got the conflict resolution wrong. This is the
   kind of failure our RL fine-tuning targets: on tasks with a clear
   "accept the other side" cue, the trained router could preferentially route
   to `claude-opus-4-6` (which in our experience handles git/merge edge cases
   better) or instruct the worker explicitly to prefer `-X theirs`.
4. **Verifier behavior is honest.** `test.sh` correctly gives reward=0 even
   though the agent "looked" successful — the files don't match.

## Artefacts

- [`trajectory.json`](./trajectory.json) — full planner + routing +
  sub-result trace for this run.
- [`commands.log`](./commands.log) — raw SubAgent / Docker command transcript.
