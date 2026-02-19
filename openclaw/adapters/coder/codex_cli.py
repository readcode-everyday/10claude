"""
Adapter for code generation via OpenAI (gpt-4o).
Used as an alternative to Claude CLI.
"""
import os
import logging

log = logging.getLogger(__name__)


def run(prompt: str, cwd: str = "/app/workspace/repo") -> str:
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    log.info(f"codex | cwd={cwd} | prompt={prompt[:100]}...")

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert software engineer. "
                    "Write complete, correct, production-quality code. "
                    "Output code only — no markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )

    output = response.choices[0].message.content
    log.info(f"codex | done | output_len={len(output)}")
    return output
