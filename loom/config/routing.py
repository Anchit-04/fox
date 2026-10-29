

from providers.config import MODEL_REGISTRY, get_tier
from . import vault


class RoutingError(Exception):
    """No model satisfies the requested role, given what's currently in the vault."""


def pick_orchestrator() -> str:
    strong_keys = [k for k in MODEL_REGISTRY if get_tier(k) == "strong"]
    for key in strong_keys:
        if vault.validate_key(key):
            return key
    raise RoutingError(
        f"No 'strong' tier model has a working API key — the orchestrator "
        f"role needs one of {strong_keys} configured and valid. Set the "
        "corresponding key in .env."
    )


def pick_executor(preferred: str | None = None) -> str:
    if preferred is not None:
        if not vault.is_present(preferred):
            raise RoutingError(f"requested executor {preferred!r} has no API key configured")
        return preferred

    cheap_candidates = [k for k in MODEL_REGISTRY if get_tier(k) == "cheap" and vault.is_present(k)]
    if cheap_candidates:
        return cheap_candidates[0]

    fallback_candidates = [k for k in MODEL_REGISTRY if vault.is_present(k)]
    if fallback_candidates:
        return fallback_candidates[0]

    raise RoutingError(
        "No model with a present API key is available for an executor. "
        "Configure at least one key in .env."
    )
