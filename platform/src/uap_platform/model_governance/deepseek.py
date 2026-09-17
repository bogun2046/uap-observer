"""DeepSeek Chat Completions adapter with explicit pricing provenance."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .contracts import ModelRequest, PromptVersion, ProviderError, ProviderResponse

DEEPSEEK_PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


@dataclass(frozen=True, slots=True)
class DeepSeekPricing:
    """CNY per one million tokens, snapshotted from the official price page."""

    version: str = "deepseek-flash-cny-2026-09-14"
    cache_hit_off_peak: str = "0.02"
    cache_hit_peak: str = "0.04"
    cache_miss_off_peak: str = "1"
    cache_miss_peak: str = "2"
    output_off_peak: str = "4"
    output_peak: str = "8"
    source_url: str = DEEPSEEK_PRICING_SOURCE

    def is_peak(self, observed_at: datetime) -> bool:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        beijing = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if beijing.weekday() >= 5:
            return False
        local_time = beijing.time().replace(tzinfo=None)
        return time(9) <= local_time < time(12) or time(14) <= local_time < time(18)

    def cost_microunits(
        self, *, cache_hit_tokens: int, cache_miss_tokens: int, output_tokens: int, peak: bool
    ) -> int:
        from decimal import ROUND_CEILING, Decimal

        rates = (
            self.cache_hit_peak if peak else self.cache_hit_off_peak,
            self.cache_miss_peak if peak else self.cache_miss_off_peak,
            self.output_peak if peak else self.output_off_peak,
        )
        amount = sum(
            (
                Decimal(tokens) * Decimal(rate)
                for tokens, rate in zip(
                    (cache_hit_tokens, cache_miss_tokens, output_tokens), rates, strict=True
                )
            ),
            Decimal("0"),
        )
        # Rates are CNY / 1M tokens; one micro-CNY per token-rate unit is exact.
        return int(amount.to_integral_value(rounding=ROUND_CEILING))

    def snapshot(self, *, peak: bool) -> dict[str, object]:
        return {
            "version": self.version,
            "currency": "CNY",
            "unit": "CNY_per_1M_tokens",
            "peak": peak,
            "cache_hit": self.cache_hit_peak if peak else self.cache_hit_off_peak,
            "cache_miss": self.cache_miss_peak if peak else self.cache_miss_off_peak,
            "output": self.output_peak if peak else self.output_off_peak,
            "source_url": self.source_url,
        }


@dataclass(frozen=True, slots=True)
class DeepSeekProvider:
    """Network-capable provider; callers must inject the API key at runtime."""

    api_key: str = field(repr=False)
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 30.0
    max_tokens: int = 8_000
    pricing: DeepSeekPricing = field(default_factory=DeepSeekPricing)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    opener: Callable[..., Any] = field(default=urlopen, repr=False)
    name: str = "deepseek"

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if self.model not in {"deepseek-flash", "deepseek-v4-pro"}:
            raise ValueError("unsupported DeepSeek model")
        if self.timeout_seconds <= 0 or self.max_tokens < 1:
            raise ValueError("DeepSeek limits must be positive")

    def complete(self, request: ModelRequest, prompt: PromptVersion) -> ProviderResponse:
        user_content = prompt.user_template.replace("{{input}}", request.input_text)
        if "{{input}}" not in prompt.user_template:
            user_content = f"{prompt.user_template}\n\nSOURCE TEXT:\n{request.input_text}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system_template},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": self.max_tokens,
            "stream": False,
            "user_id": "uap-v1-worker",
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        http_request = Request(  # noqa: S310
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "uap-platform-v1/1.0",
            },
            method="POST",
        )
        try:
            with self.opener(http_request, timeout=self.timeout_seconds) as response:
                raw = cast(bytes, response.read())
        except HTTPError as error:
            raw_error = error.read()
            raise ProviderError(
                self._error_code(error.code),
                "DeepSeek request failed",
                http_status=error.code,
                retryable=error.code in {408, 429, 500, 503},
                raw_response=raw_error,
                currency="CNY",
            ) from None
        except (TimeoutError, URLError) as error:
            raise ProviderError(
                "timeout" if isinstance(error, TimeoutError) else "connection_error",
                "DeepSeek request did not complete",
                http_status=504 if isinstance(error, TimeoutError) else 503,
                retryable=True,
                currency="CNY",
            ) from None
        if not raw.strip():
            raise ProviderError(
                "empty_response", "DeepSeek returned an empty response", http_status=502
            )
        try:
            envelope = cast(dict[str, Any], json.loads(raw))
            choice = cast(dict[str, Any], envelope["choices"][0])
            finish_reason = choice.get("finish_reason")
            content = cast(dict[str, Any], choice["message"]).get("content")
            if finish_reason == "length":
                raise ProviderError(
                    "output_truncated",
                    "DeepSeek output was truncated",
                    http_status=422,
                    raw_response=raw,
                )
            if not isinstance(content, str) or not content.strip():
                raise ProviderError(
                    "empty_result",
                    "DeepSeek returned empty content",
                    http_status=422,
                    raw_response=raw,
                )
            structured = json.loads(content)
            if not isinstance(structured, dict):
                raise ValueError("model content is not an object")
            usage = cast(Mapping[str, Any], envelope.get("usage", {}))
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
            hit_tokens = int(usage.get("prompt_cache_hit_tokens", 0))
            miss_tokens = int(usage.get("prompt_cache_miss_tokens", input_tokens - hit_tokens))
            observed_at = self.clock()
            peak = self.pricing.is_peak(observed_at)
            micro_cost = self.pricing.cost_microunits(
                cache_hit_tokens=hit_tokens,
                cache_miss_tokens=miss_tokens,
                output_tokens=output_tokens,
                peak=peak,
            )
            pricing_snapshot = self.pricing.snapshot(peak=peak)
            auditable_raw = json.dumps(
                {
                    "provider_response": envelope,
                    "uap_billing": {
                        "cost_microunits": micro_cost,
                        "pricing_snapshot": pricing_snapshot,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            return ProviderResponse(
                structured=cast(Mapping[str, object], structured),
                raw_response=auditable_raw,
                provider_response_id=str(envelope.get("id") or f"deepseek:{uuid.uuid4()}"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_minor_units=math.ceil(micro_cost / 10_000),
                currency="CNY",
                cost_microunits=micro_cost,
                pricing_snapshot=pricing_snapshot,
            )
        except ProviderError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(
                "invalid_json",
                "DeepSeek response was not valid structured JSON",
                http_status=422,
                raw_response=raw,
            ) from None

    @staticmethod
    def _error_code(status: int) -> str:
        return {
            400: "invalid_request",
            401: "authentication_failed",
            402: "provider_budget_exhausted",
            422: "invalid_parameters",
            429: "rate_limited",
            500: "provider_error",
            503: "provider_overloaded",
        }.get(status, "provider_http_error")
