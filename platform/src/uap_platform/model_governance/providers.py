"""Provider boundary for deterministic and external model adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .contracts import ModelProvider, ModelRequest, PromptVersion, ProviderResponse


class StaticProvider:
    """Deterministic provider used by tests and the isolated runtime probe."""

    name = "static"

    def __init__(self, response: Mapping[str, object]) -> None:
        self._response = dict(response)

    def complete(self, request: ModelRequest, prompt: PromptVersion) -> ProviderResponse:
        del request, prompt
        raw = json.dumps(self._response, sort_keys=True, separators=(",", ":")).encode()
        return ProviderResponse(
            structured=self._response,
            raw_response=raw,
            provider_response_id="static-response",
            input_tokens=1,
            output_tokens=1,
            cost_minor_units=0,
            currency="USD",
        )


class ProviderRegistry:
    """Explicit allow-list; no network-capable provider is auto-discovered."""

    def __init__(self, providers: Mapping[str, ModelProvider]) -> None:
        self._providers = dict(providers)

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as error:
            raise ValueError(f"unsupported model provider: {name}") from error
