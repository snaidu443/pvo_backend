"""Settings, from environment variables, loaded from the `.env` in the project
folder (copy `env.sample.txt`) by explicit path, so it works from any working
directory. Standalone: nothing is read from docparse. Secrets only ever come
from the environment / `.env`, never from CLI arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import certifi
from dotenv import load_dotenv

from structx import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

# This environment has `pip_system_certs` patching `ssl.SSLContext` to
# `truststore.SSLContext`; the `openai` SDK's HTTP stack builds a
# `truststore.SSLContext` itself, which then recurses infinitely on every
# HTTPS call. With `SSL_CERT_FILE` set it takes a plain
# `ssl.create_default_context(cafile=...)` path instead. Only set if the
# environment hasn't chosen a CA bundle already. (Same fix as docparse's.)
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

REASONING_EFFORTS = ("minimal", "low", "medium", "high")
PPTX_MODES = ("pdf", "file")
CALL_MODES = ("per_field", "single")


class ConfigError(ValueError):
    pass


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    try:
        return int(value) if value else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number.") from exc


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    try:
        return float(value) if value else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number.") from exc


@dataclass(frozen=True)
class StructConfig:
    base_url: str
    api_key: str = ""  # never shown: see __repr__
    model: str = ""
    # Headroom for hidden reasoning tokens plus the JSON answer.
    max_output_tokens: int = 16_000
    timeout: float = 600.0
    # Abort and retry a call that sends nothing for this long. Healthy calls
    # never go quiet for more than ~16 s (measured), but the gateway
    # sometimes holds a request ~5 min and then answers HTTP 500 -- a retry
    # then succeeds in seconds.
    stall_seconds: float = 20.0
    max_retries: int = 2
    # "per_field": one extraction call per top-level spec field, all at once
    # (the wall time is the slowest field's); "single": one call for the
    # whole spec (one answer, so names are consistent across fields, but it
    # is written in one stream -- measured slower, see PROGRESS.md).
    calls: str = "per_field"
    # Parallel extraction calls in per_field mode.
    extract_concurrency: int = 10
    # "low" by default: at the model's default effort a 13-slide CDP spent
    # ~3 min reasoning before writing anything. None = the model's default.
    reasoning_effort: str | None = "low"
    # How a .pptx reaches the model: "pdf" = saved as PDF by the local
    # PowerPoint (page images); "file" = the .pptx itself (the gateway gives
    # the model its text only). Either way hidden slides are left out and
    # speaker notes are never sent (no settings for these).
    pptx_mode: str = "pdf"

    def __repr__(self) -> str:
        return f"StructConfig(base_url={self.base_url!r}, model={self.model!r}, api_key=<redacted>)"

    @classmethod
    def from_env(cls) -> "StructConfig":
        base_url = (os.environ.get("STRUCTX_BASE_URL") or "").strip()
        model = (os.environ.get("STRUCTX_MODEL") or "").strip()
        api_key = os.environ.get("STRUCTX_API_KEY") or ""
        if not base_url or not model or not api_key:
            # Fail closed: no default endpoint, model or credential.
            raise ConfigError("STRUCTX_BASE_URL, STRUCTX_MODEL and STRUCTX_API_KEY must all be set (see env.sample.txt).")
        if not base_url.startswith("https://"):
            raise ConfigError("STRUCTX_BASE_URL must be an https:// URL.")
        max_output = _env_int("STRUCTX_MAX_OUTPUT_TOKENS", 16_000)
        extract_concurrency = _env_int("STRUCTX_EXTRACT_CONCURRENCY", 10)
        max_retries = _env_int("STRUCTX_MAX_RETRIES", 2)
        if max_output < 1 or extract_concurrency < 1 or max_retries < 0:
            raise ConfigError(
                "STRUCTX_MAX_OUTPUT_TOKENS and STRUCTX_EXTRACT_CONCURRENCY must be >= 1, "
                "STRUCTX_MAX_RETRIES >= 0."
            )
        effort = (os.environ.get("STRUCTX_REASONING_EFFORT") or "low").strip().lower()
        if effort not in REASONING_EFFORTS + ("default",):
            raise ConfigError(f"STRUCTX_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)} or default.")
        stall = _env_float("STRUCTX_STALL_SECONDS", 20.0)
        timeout = _env_float("STRUCTX_TIMEOUT", 600.0)
        if stall <= 0 or timeout <= 0:
            raise ConfigError("STRUCTX_STALL_SECONDS and STRUCTX_TIMEOUT must be > 0.")
        pptx_mode = (os.environ.get("STRUCTX_PPTX_MODE") or "pdf").strip().lower()
        if pptx_mode not in PPTX_MODES:
            raise ConfigError(f"STRUCTX_PPTX_MODE must be one of {', '.join(PPTX_MODES)}.")
        calls = (os.environ.get("STRUCTX_CALLS") or "per_field").strip().lower()
        if calls not in CALL_MODES:
            raise ConfigError(f"STRUCTX_CALLS must be one of {', '.join(CALL_MODES)}.")
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_output_tokens=max_output,
            timeout=timeout,
            stall_seconds=stall,
            max_retries=max_retries,
            calls=calls,
            extract_concurrency=extract_concurrency,
            reasoning_effort=None if effort == "default" else effort,
            pptx_mode=pptx_mode,
        )
