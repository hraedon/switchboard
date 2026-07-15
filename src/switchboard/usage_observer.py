"""Read-only response-stream usage observer (Plan 012 §4.5).

Parses the ``usage`` object from response bytes **as they stream through** —
read-only, zero modification.  The client receives exactly what the upstream
sent.  This is the streaming equivalent of the proxy already observing the
response status code and headers (for 429 classification); the ``usage``
field is the one additional structural field, and only when the operator
opts in via ``[token_budget]``.

**SSE parsing** (streaming responses, ``Content-Type: text/event-stream``):
OpenAI-compatible SSE is a sequence of ``data: <json>\\n\\n`` lines.  The
``usage`` object appears in the final chunk (when the client set
``stream_options.include_usage: true``) or in a trailing chunk adjacent to
``data: [DONE]``.  The observer buffers the *current* ``data:`` line
incrementally (at most a few KB — SSE chunks are small), parses it as JSON,
and extracts ``usage.prompt_tokens`` / ``usage.completion_tokens``.  The line
buffer is discarded after parsing — no full-body buffering.

**Non-streaming** (``Content-Type: application/json``): the response body is
buffered (it's small and non-streaming by definition), parsed for ``usage``,
then the original bytes forwarded unchanged.

**Fail-safe**: if parsing fails or no ``usage`` is found, the observer
silently returns ``None`` — token tracking is best-effort.
"""

from __future__ import annotations

import json
from typing import Any


class UsageObserver:
    """Read-only SSE/JSON usage extractor.  Never modifies bytes.

    Feed response chunks via :meth:`feed_chunk` (SSE) or
    :meth:`feed_non_streaming` (complete JSON body).  After the stream
    completes, read :attr:`usage` for ``(prompt_tokens, completion_tokens)``
    or ``None``.
    """

    def __init__(self, *, is_sse: bool = True) -> None:
        self._is_sse = is_sse
        self._line_buf = bytearray()
        self._prompt: int | None = None
        self._completion: int | None = None

    def feed_chunk(self, chunk: bytes) -> None:
        """Feed a response chunk for incremental SSE parsing.

        Accumulates bytes into a line buffer.  When a complete ``data:`` line
        is found, parses it for the ``usage`` object.  Read-only — the chunk
        is forwarded to the client by the caller unchanged.
        """
        if not self._is_sse:
            return
        self._line_buf.extend(chunk)
        if len(self._line_buf) > 65536:
            self._line_buf.clear()
            return
        # Process complete lines (terminated by \n).
        while b"\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split(b"\n", 1)
            self._process_sse_line(bytes(line))

    def feed_non_streaming(self, body: bytes) -> None:
        """Parse a complete non-streaming JSON response body for usage.

        Read-only — the caller forwards the original bytes unchanged.
        """
        try:
            data: Any = json.loads(body)
        except (ValueError, TypeError):
            return
        self._extract_usage(data)

    @property
    def usage(self) -> tuple[int, int] | None:
        """``(prompt_tokens, completion_tokens)`` if found, else None."""
        if self._prompt is not None and self._completion is not None:
            return (self._prompt, self._completion)
        if self._prompt is not None:
            return (self._prompt, 0)
        return None

    # -- internals -----------------------------------------------------------

    def _process_sse_line(self, line: bytes) -> None:
        """Parse one SSE line for the usage object."""
        stripped = line.strip()
        if not stripped:
            return
        # SSE data lines start with "data: " (or "data:").
        if stripped.startswith(b"data:"):
            payload = stripped[5:].strip()
            if payload == b"[DONE]":
                return
            try:
                data: Any = json.loads(payload)
            except (ValueError, TypeError):
                return
            self._extract_usage(data)

    def _extract_usage(self, data: Any) -> None:
        """Extract prompt/completion tokens from a parsed JSON object."""
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, int):
            self._prompt = prompt
        if isinstance(completion, int):
            self._completion = completion
