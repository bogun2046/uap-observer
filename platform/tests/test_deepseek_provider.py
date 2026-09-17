from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from uap_platform.model_governance import (
    DeepSeekPricing,
    DeepSeekProvider,
    ModelRequest,
    ModelTaskType,
    PromptVersion,
    ProviderError,
)
from uap_platform.model_governance.contracts import json_sha256, sha256_bytes


class Response:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def prompt() -> PromptVersion:
    values = {
        "task_type": "summary",
        "version": "v1",
        "system_template": "Return JSON.",
        "user_template": "Summarize as JSON: {{input}}",
        "output_schema": {"type": "object"},
    }
    return PromptVersion(
        id=uuid.uuid4(),
        task_type=ModelTaskType.SUMMARY,
        version="v1",
        system_template="Return JSON.",
        user_template="Summarize as JSON: {{input}}",
        output_schema={"type": "object"},
        content_sha256=json_sha256(values),
        active=True,
    )


def model_request(prompt_id: uuid.UUID) -> ModelRequest:
    text = "source text"
    return ModelRequest(
        document_version_id=uuid.uuid4(),
        job_attempt_id=uuid.uuid4(),
        task_type=ModelTaskType.SUMMARY,
        prompt_version_id=prompt_id,
        provider="deepseek",
        model="deepseek-flash",
        input_text=text,
        input_sha256=sha256_bytes(text.encode()),
        semantic_idempotency_key="semantic",
        idempotency_key="attempt",
    )


def test_deepseek_records_returned_usage_and_price_snapshot() -> None:
    selected_prompt = prompt()
    envelope = {
        "id": "response-1",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"summary":"x","bullets":["y"]}'},
            }
        ],
        "usage": {
            "prompt_tokens": 110,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 100,
        },
    }
    provider = DeepSeekProvider(
        "not-a-real-key",
        clock=lambda: datetime(2026, 9, 16, 2, tzinfo=UTC),
        opener=lambda *_args, **_kwargs: Response(envelope),
    )

    result = provider.complete(model_request(selected_prompt.id), selected_prompt)

    assert result.input_tokens == 110
    assert result.output_tokens == 20
    assert result.currency == "CNY"
    assert result.cost_microunits == 361
    assert result.cost_minor_units == 1
    assert result.pricing_snapshot["version"] == "deepseek-flash-cny-2026-09-14"


def test_deepseek_rejects_truncated_json_response() -> None:
    selected_prompt = prompt()
    provider = DeepSeekProvider(
        "not-a-real-key",
        opener=lambda *_args, **_kwargs: Response(
            {
                "id": "response-2",
                "choices": [{"finish_reason": "length", "message": {"content": "{}"}}],
            }
        ),
    )
    with pytest.raises(ProviderError) as raised:
        provider.complete(model_request(selected_prompt.id), selected_prompt)
    assert raised.value.code == "output_truncated"


def test_pricing_uses_beijing_peak_windows() -> None:
    pricing = DeepSeekPricing()
    assert pricing.is_peak(datetime(2026, 9, 16, 2, tzinfo=UTC))
    assert not pricing.is_peak(datetime(2026, 9, 16, 5, tzinfo=UTC))
