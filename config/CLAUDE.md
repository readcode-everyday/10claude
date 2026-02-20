# Agent Rules

## Conflict Handling

**Rebase failure flow:**
1. If "unstaged changes" error — commit or stash current changes first
2. If merge conflicts:
   - Check conflict files: `git status`
   - Read conflict content, understand intent of both sides
   - Manually resolve (keep the correct code)
   - `git add <resolved-files>`
   - `git rebase --continue`
3. Repeat until rebase is complete

**Test failure flow:**
1. Run tests
2. If failed, analyze the error messages
3. Fix the bug in code
4. Re-run tests until all pass
5. Commit the fix: `git commit -m "fix: ..."`

**Never give up:** When encountering rebase or test failures, resolve the problem before continuing. Do not mark a task as failed without attempting to fix it.

---

## Worker Git Contract

The worker (not Claude) handles two things automatically:

- **Before your task starts:** the worker runs `git fetch && git reset --hard origin/main`
  so you always begin from the latest pushed state. Do NOT pull at the start — it is already done.
- **After your task ends:** the worker runs `git push origin main` as a safety net.

**Your job:** make changes, commit, push. The worker guarantees the push eventually lands
even if you forget, but you should still push explicitly.

## Parallel Development — Goal Sub-Tasks

When executing a sub-task from a decomposed goal, multiple workers may commit to the
same branch simultaneously. Follow this flow **every time** before committing:

1. Complete all file changes.
2. `git add -A`
3. `git pull --rebase origin main` — incorporate any commits from parallel workers.
4. Resolve conflicts if any (`git status` → edit → `git add` → `git rebase --continue`).
5. `git add -A`
6. `git commit -m "feat: <task title>"`
7. `git push origin main` (retry up to 3× if rejected — pull --rebase between attempts)

**Never leave changes uncommitted.** Dependent tasks read the repo state directly.

## Parallel Development (Git Worktree)

Multiple workers operate in parallel, each in an isolated git worktree.

- Each worker has its own worktree and isolated `data/` directory
- Shared files are accessed via the main repo (not symlinks): task queue, locks, API keys
- **Do not symlink `PROGRESS.md`** — edit it directly in the main repo using `git -C <repo-root>`

---

## Lessons Learned

After encountering a problem or completing a major change, record it in `PROGRESS.md`:
- What problem was encountered
- How it was solved
- How to avoid it in the future
- **Must include the git commit ID**

**Do not make the same mistake twice.**
