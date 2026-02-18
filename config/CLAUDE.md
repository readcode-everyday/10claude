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
