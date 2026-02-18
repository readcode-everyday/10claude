"""
Anthropic LLM adapter — uses the claude CLI binary so browser-based
auth (claude auth login) covers both planning and coding with no API key.
Falls back to the Python SDK if ANTHROPIC_API_KEY is set.
"""
import os
import subprocess
import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)


def generate(prompt: str, system: str = None) -> str:
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    # Prefer Python SDK when API key is present (faster, structured)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return _sdk(full_prompt, api_key)

    # Fall back to claude CLI (covers browser OAuth login)
    return _cli(full_prompt)


def _cli(prompt: str) -> str:
    env = {**os.environ, "CLAUDE_CODE_SKIP_ONBOARDING": "1"}
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if result.returncode != 0:
        log.warning(f"anthropic._cli stderr: {result.stderr[:200]}")
        if not result.stdout.strip():
            raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def _sdk(prompt: str, api_key: str) -> str:
    import anthropic
    cfg = yaml.safe_load(Path("/app/config/llm_backends.yaml").read_text())
    model = cfg["providers"]["anthropic"]["model"]
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text
