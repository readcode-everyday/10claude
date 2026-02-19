"""
Adapter for the Claude CLI (claude-code).
Runs `claude -p <prompt>` non-interactively inside a git worktree.
"""
import os
import subprocess
import logging

log = logging.getLogger(__name__)


def run(prompt: str, cwd: str = "/app/workspace/repo") -> str:
    log.info(f"claude_cli | cwd={cwd} | prompt={prompt[:100]}...")

    env = {**os.environ, "CLAUDE_CODE_SKIP_ONBOARDING": "1"}

    result = subprocess.run(
        [
            "claude",
            "-p", prompt,
            "--output-format", "text",
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=600,
    )

    output = result.stdout.strip()
    if result.returncode != 0:
        log.warning(f"claude_cli stderr: {result.stderr[:300]}")
        # Return whatever output we got plus stderr — don't swallow errors
        return (output + "\n" + result.stderr).strip()

    log.info(f"claude_cli | done | output_len={len(output)}")
    return output
