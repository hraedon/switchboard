"""Unit tests for the read-only UsageObserver (Plan 012 §4.5)."""

from __future__ import annotations

import json

from switchboard.usage_observer import UsageObserver


class TestSSEUsageParsing:
    def test_extracts_usage_from_final_chunk(self) -> None:
        observer = UsageObserver(is_sse=True)
        chunk1 = (
            b'data: {"id":"1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
        )
        chunk2 = (
            b'data: {"id":"1","choices":[],"usage":'
            b'{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
        )
        chunk3 = b"data: [DONE]\n\n"
        observer.feed_chunk(chunk1)
        assert observer.usage is None
        observer.feed_chunk(chunk2)
        assert observer.usage == (10, 20)
        observer.feed_chunk(chunk3)
        assert observer.usage == (10, 20)

    def test_handles_split_chunks(self) -> None:
        observer = UsageObserver(is_sse=True)
        full_line = (
            b'data: {"usage":{"prompt_tokens":5,'
            b'"completion_tokens":7}}\n\n'
        )
        # Feed byte by byte
        for i in range(len(full_line)):
            observer.feed_chunk(full_line[i : i + 1])
        assert observer.usage == (5, 7)

    def test_no_usage_in_stream(self) -> None:
        observer = UsageObserver(is_sse=True)
        observer.feed_chunk(
            b'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
        )
        observer.feed_chunk(b"data: [DONE]\n\n")
        assert observer.usage is None

    def test_malformed_json_silently_ignored(self) -> None:
        observer = UsageObserver(is_sse=True)
        observer.feed_chunk(b"data: {broken json\n\n")
        observer.feed_chunk(
            b'data: {"usage":{"prompt_tokens":1,'
            b'"completion_tokens":2}}\n\n'
        )
        assert observer.usage == (1, 2)

    def test_only_prompt_tokens(self) -> None:
        observer = UsageObserver(is_sse=True)
        observer.feed_chunk(
            b'data: {"usage":{"prompt_tokens":42}}\n\n'
        )
        assert observer.usage == (42, 0)

    def test_non_data_lines_ignored(self) -> None:
        observer = UsageObserver(is_sse=True)
        observer.feed_chunk(b": comment\n\n")
        observer.feed_chunk(b"event: ping\n\n")
        observer.feed_chunk(
            b'data: {"usage":{"prompt_tokens":3,'
            b'"completion_tokens":4}}\n\n'
        )
        assert observer.usage == (3, 4)


class TestNonStreamingUsageParsing:
    def test_extracts_usage_from_json_body(self) -> None:
        observer = UsageObserver(is_sse=False)
        body = json.dumps(
            {
                "id": "1",
                "choices": [{"message": {"content": "Hi"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 200,
                    "total_tokens": 300,
                },
            }
        ).encode()
        observer.feed_non_streaming(body)
        assert observer.usage == (100, 200)

    def test_no_usage_field(self) -> None:
        observer = UsageObserver(is_sse=False)
        body = json.dumps({"id": "1", "choices": []}).encode()
        observer.feed_non_streaming(body)
        assert observer.usage is None

    def test_malformed_json(self) -> None:
        observer = UsageObserver(is_sse=False)
        observer.feed_non_streaming(b"{not json")
        assert observer.usage is None
