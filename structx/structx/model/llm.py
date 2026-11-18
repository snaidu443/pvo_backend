"""Responses API calls with strict JSON-schema output.

`store=False`: nothing sent is kept on the provider side (the gateway does
store responses by default -- probed 2026-11-02). Fails closed on a missing
key, an incomplete or refused response, or a reply that isn't the schema's
JSON object; nothing is guessed. Only metadata (model, token counts) is
logged, never document text or the key.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx
from openai import OpenAI

from structx.settings.config import StructConfig

_log = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    pass


_HEARTBEAT_SECONDS = 30


def _stream(client: OpenAI, kwargs: dict, name: str):
    """One streamed Responses call; returns the final response object.

    Streamed, not a plain request: a long reasoning call sends nothing for
    minutes, and something between here and the gateway drops a silent
    connection after ~5 min (seen 2026-11-02: a request died at 5:06 and was
    retried) -- a 13-slide deck then looked like a hang. Streaming keeps
    events flowing, and a heartbeat logs progress while waiting. Only event
    types and sizes are logged, never content.
    """
    start = time.monotonic()
    state = {"chars": 0, "last_event": start, "longest_gap": 0.0}
    done = threading.Event()

    def heartbeat() -> None:
        while not done.wait(_HEARTBEAT_SECONDS):
            now = time.monotonic()
            _log.info(
                "%s: still waiting for the model (%.0fs elapsed, %d characters of answer so far, last event %.0fs ago)...",
                name, now - start, state["chars"], now - state["last_event"],
            )

    threading.Thread(target=heartbeat, daemon=True).start()
    final = None
    try:
        stream = client.responses.create(stream=True, **kwargs)
        try:
            for event in stream:
                now = time.monotonic()
                state["longest_gap"] = max(state["longest_gap"], now - state["last_event"])
                state["last_event"] = now
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    state["chars"] += len(event.delta or "")
                elif kind in ("response.completed", "response.incomplete", "response.failed"):
                    final = event.response
                elif kind == "error":
                    raise ExtractionError(f"Model stream error: {getattr(event, 'message', '') or getattr(event, 'code', '')}")
        finally:
            stream.close()
    finally:
        done.set()
    if final is None:
        raise ConnectionError("stream ended without a final response")
    _log.info("%s: done in %.0fs (longest silence between events %.0fs).", name, time.monotonic() - start, state["longest_gap"])
    return final


def make_client(cfg: StructConfig) -> OpenAI:
    if not cfg.api_key:
        raise ExtractionError("No API key configured (STRUCTX_API_KEY); refusing to call the model.")
    # Retries are done per streamed call in respond_json (logged), not
    # silently by the SDK. `read` is the stall limit: the longest wait for the
    # next bytes (response headers or a stream event) before giving up.
    timeout = httpx.Timeout(cfg.timeout, connect=15.0, read=cfg.stall_seconds)
    return OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=timeout, max_retries=0)


def respond_json(
    client: OpenAI,
    cfg: StructConfig,
    *,
    instructions: str,
    content: list[dict],
    name: str,
    schema: dict,
    max_output_tokens: int | None = None,
    run_log=None,
) -> dict:
    """One strict-schema Responses call; recorded in `run_log` (runlog.RunLog)
    when given, whatever the outcome."""
    rec = None
    if run_log is not None:
        rec = run_log.start_call(
            name=name, instructions=instructions, content=content, schema=schema,
            max_output_tokens=max_output_tokens or cfg.max_output_tokens, reasoning_effort=cfg.reasoning_effort,
        )
    try:
        data, usage = _respond(client, cfg, instructions=instructions, content=content, name=name, schema=schema,
                               max_output_tokens=max_output_tokens, rec=rec)
    except ExtractionError as exc:
        if rec is not None:
            rec.status, rec.error = "failed", str(exc)
        raise
    finally:
        if rec is not None:
            rec.seconds = time.monotonic() - rec.started
    if rec is not None:
        rec.status, rec.usage = "completed", usage
    return data


def _respond(client, cfg, *, instructions, content, name, schema, max_output_tokens, rec) -> tuple[dict, dict | None]:
    kwargs: dict = dict(
        model=cfg.model,
        instructions=instructions,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
        max_output_tokens=max_output_tokens or cfg.max_output_tokens,
        store=False,
    )
    if cfg.reasoning_effort:
        kwargs["reasoning"] = {"effort": cfg.reasoning_effort}
    response = None
    for attempt in range(cfg.max_retries + 1):
        if rec is not None:
            rec.attempts = attempt + 1
        try:
            response = _stream(client, kwargs, name)
            break
        except ExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - dropped connection / transport error: retried, then normalized
            if attempt == cfg.max_retries:
                raise ExtractionError(f"Model request failed ({type(exc).__name__}): {exc}") from exc
            _log.warning("%s: request failed (%s); retrying (%d of %d).", name, type(exc).__name__, attempt + 1, cfg.max_retries)

    if response.status != "completed":
        reason = getattr(response.incomplete_details, "reason", None)
        hint = " -- raise STRUCTX_MAX_OUTPUT_TOKENS" if reason == "max_output_tokens" else ""
        raise ExtractionError(f"Model response {response.status} ({reason}){hint}.")
    for item in response.output:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                raise ExtractionError("Model refused the request.")
    usage = response.usage
    usage_info = None
    if usage is not None:
        details = getattr(usage, "output_tokens_details", None)
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None)
        usage_info = {"input": usage.input_tokens, "output": usage.output_tokens,
                      "reasoning": getattr(details, "reasoning_tokens", None), "cached": cached}
        _log.info(
            "%s: %d input (%s cached) / %d output tokens (%s reasoning).",
            name, usage.input_tokens, cached, usage.output_tokens, getattr(details, "reasoning_tokens", "?"),
        )
    try:
        data = json.loads(response.output_text)
    except ValueError as exc:
        raise ExtractionError("Model reply is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise ExtractionError("Model reply is not a JSON object.")
    return data, usage_info
