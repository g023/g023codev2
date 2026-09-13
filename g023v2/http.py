"""Responses API HTTP layer with retry and SSE parsing."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterator

import requests

from g023v2.constants import (
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    MAX_RETRIES,
    RESPONSES_URL,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
)
from g023v2.util import get_api_key, log


def _sleep_with_cancel(seconds: float, cancel_event: threading.Event | None) -> bool:
    """Sleep in 100ms slices, returning False if cancelled during the sleep."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel_event is not None and cancel_event.is_set():
            return False
        time.sleep(min(0.1, end - time.monotonic()))
    return True


def _post_with_retry(
    payload: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled before request")

        try:
            r = requests.post(
                RESPONSES_URL,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            )
        except requests.RequestException as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
                log(f"[retry] network error, retry {attempt + 1} in {delay:.1f}s: {e}")
                if not _sleep_with_cancel(delay, cancel_event):
                    raise RuntimeError("cancelled during retry backoff")
                continue
            raise

        if r.status_code == 200:
            log("[http] 200, reading stream")
            return r

        if r.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            retry_after = r.headers.get("Retry-After", "")
            body = r.text[:400]
            r.close()
            delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
            if retry_after:
                try:
                    delay = max(delay, min(RETRY_MAX_DELAY, float(retry_after)))
                except ValueError:
                    pass
            log(f"[retry] HTTP {r.status_code}, retry {attempt + 1} in {delay:.1f}s: {body}")
            if not _sleep_with_cancel(delay, cancel_event):
                raise RuntimeError("cancelled during retry backoff")
            continue

        body = r.text[:2000]
        r.close()
        raise RuntimeError(f"HTTP {r.status_code}: {body}")

    raise RuntimeError(f"exhausted retries: {last_exc}")


def stream_response(
    payload: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    r = _post_with_retry(payload, cancel_event=cancel_event)
    try:
        event_type = ""
        for raw in r.iter_lines(decode_unicode=True):
            if cancel_event is not None and cancel_event.is_set():
                break
            if raw is None:
                continue
            if raw.startswith("event: "):
                event_type = raw[7:].strip()
            elif raw.startswith("data: "):
                data_str = raw[6:]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                yield event_type, data
    finally:
        r.close()
