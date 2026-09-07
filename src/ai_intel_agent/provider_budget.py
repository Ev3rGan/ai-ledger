from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files
from typing import Protocol


class ProviderBudgetReservation(Protocol):
    def settle_usd(self, actual_cost_usd: Decimal) -> None: ...

    def commit_reserved(self) -> None: ...

    def release(self) -> None: ...


class MeteredProviderBudget(Protocol):
    def reserve(self) -> ProviderBudgetReservation | None: ...


@dataclass(frozen=True)
class ProviderTokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ProviderBudgetRate:
    model_id: str
    cache_hit_per_million_usd: Decimal
    cache_miss_per_million_usd: Decimal
    output_per_million_usd: Decimal


@dataclass(frozen=True)
class ProviderBudgetPricing:
    version: str
    pricing_checked_at: str
    accounting_policy: str
    pricing_source: str
    rates: Mapping[str, ProviderBudgetRate]


class ProviderBudgetPricingError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_provider_budget_pricing() -> ProviderBudgetPricing:
    resource = files("ai_intel_agent").joinpath("data/provider_budget_pricing.v1.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    try:
        raw_rates = payload["models"]
        rates = {
            model_id: ProviderBudgetRate(
                model_id=model_id,
                cache_hit_per_million_usd=Decimal(str(values["cache_hit_per_million_usd"])),
                cache_miss_per_million_usd=Decimal(str(values["cache_miss_per_million_usd"])),
                output_per_million_usd=Decimal(str(values["output_per_million_usd"])),
            )
            for model_id, values in raw_rates.items()
        }
        pricing = ProviderBudgetPricing(
            version=str(payload["version"]),
            pricing_checked_at=str(payload["pricing_checked_at"]),
            accounting_policy=str(payload["accounting_policy"]),
            pricing_source=str(payload["pricing_source"]),
            rates=rates,
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ProviderBudgetPricingError("Provider budget pricing is invalid") from error
    if (
        not pricing.version
        or pricing.accounting_policy != "peak-rate-ceiling"
        or not pricing.pricing_source.startswith("https://api-docs.deepseek.com/")
        or not pricing.rates
        or any(
            value < 0
            for rate in pricing.rates.values()
            for value in (
                rate.cache_hit_per_million_usd,
                rate.cache_miss_per_million_usd,
                rate.output_per_million_usd,
            )
        )
    ):
        raise ProviderBudgetPricingError("Provider budget pricing contract is invalid")
    return pricing


def provider_token_usage(value: object) -> ProviderTokenUsage | None:
    if not isinstance(value, dict):
        return None
    prompt_tokens = value.get("prompt_tokens")
    completion_tokens = value.get("completion_tokens")
    prompt_details = value.get("prompt_tokens_details")
    if prompt_details is not None and not isinstance(prompt_details, dict):
        return None
    details_cached_tokens = (
        prompt_details.get("cached_tokens", 0)
        if isinstance(prompt_details, dict)
        else 0
    )
    cached_tokens = value.get(
        "prompt_cache_hit_tokens",
        value.get("cached_tokens", details_cached_tokens),
    )
    values = (prompt_tokens, cached_tokens, completion_tokens)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in values):
        return None
    if cached_tokens > prompt_tokens:
        return None
    return ProviderTokenUsage(
        input_tokens=prompt_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=completion_tokens,
    )


def estimate_provider_usage_usd(
    model_id: str,
    usage: ProviderTokenUsage,
    *,
    pricing: ProviderBudgetPricing | None = None,
) -> Decimal:
    pricing = pricing or load_provider_budget_pricing()
    try:
        rate = pricing.rates[model_id]
    except KeyError as error:
        raise ProviderBudgetPricingError(
            f"Provider budget pricing has no rate for model {model_id}"
        ) from error
    uncached_tokens = usage.input_tokens - usage.cached_input_tokens
    return (
        Decimal(usage.cached_input_tokens) * rate.cache_hit_per_million_usd
        + Decimal(uncached_tokens) * rate.cache_miss_per_million_usd
        + Decimal(usage.output_tokens) * rate.output_per_million_usd
    ) / Decimal(1_000_000)


def settle_provider_response_usage(
    reservation: ProviderBudgetReservation,
    *,
    model_id: str,
    usage: object,
) -> bool:
    normalized = provider_token_usage(usage)
    if normalized is None:
        reservation.commit_reserved()
        return False
    reservation.settle_usd(estimate_provider_usage_usd(model_id, normalized))
    return True
