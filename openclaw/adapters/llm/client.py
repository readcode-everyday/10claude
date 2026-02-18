"""
Single entry point for all LLM calls.
Routes to provider based on config/llm_backends.yaml.
"""
import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

_cfg = None


def _config() -> dict:
    global _cfg
    if _cfg is None:
        _cfg = yaml.safe_load(Path("/app/config/llm_backends.yaml").read_text())
    return _cfg


def call(prompt: str, system: str = None) -> str:
    cfg = _config()
    provider = cfg.get("default_provider", "openai")
    log.debug(f"LLM call via provider={provider}, prompt_len={len(prompt)}")

    try:
        if provider == "anthropic":
            from openclaw.adapters.llm.anthropic import generate
            return generate(prompt, system=system)
        else:
            from openclaw.adapters.llm.openai import generate
            return generate(prompt, system=system)
    except Exception as e:
        log.warning(f"LLM call failed ({e}), returning empty string")
        return ""
