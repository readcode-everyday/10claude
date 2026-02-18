import os
import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)


def generate(prompt: str, system: str = None) -> str:
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    cfg = yaml.safe_load(Path("/app/config/llm_backends.yaml").read_text())
    model = cfg["providers"]["openai"]["model"]

    client = OpenAI(api_key=api_key)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    log.debug(f"openai.generate model={model}")
    response = client.chat.completions.create(model=model, messages=messages)
    return response.choices[0].message.content
