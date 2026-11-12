"""LLM provider abstraction (PRD §5). Default Claude; swap by implementing `LLMProvider`."""
from __future__ import annotations

import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from radar.config import LlmConfig

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Transient or per-item failure: skip this item, retry next run."""


class LLMUnavailable(LLMError):
    """Provider unusable for the whole run (auth, config). Stop calling it."""


class LLMProvider(Protocol):
    name: str  # stored as `extracted_by`, e.g. "llm:claude-opus-5-5"

    def extract(self, schema: type[T], system: str, text: str) -> T: ...


class AnthropicProvider:
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, cfg: LlmConfig, client=None):
        import anthropic

        self._anthropic = anthropic
        self.cfg = cfg
        self.client = client or anthropic.Anthropic()  # env key, auth token or `ant` profile
        self.name = f"llm:{cfg.model}"

    def extract(self, schema: type[T], system: str, text: str) -> T:
        a = self._anthropic
        try:
            resp = self.client.beta.messages.parse(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=system,
                messages=[{"role": "user", "content": text}],
                output_format=schema,
                output_config={"effort": self.cfg.effort},
                betas=[self.FALLBACK_BETA],
                fallbacks="default",  # re-run server-side on another model if declined
            )
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as e:
            raise LLMUnavailable(f"{type(e).__name__}: {e}") from e
        except a.BadRequestError as e:
            raise LLMError(f"bad request: {e}") from e
        except a.RateLimitError as e:
            raise LLMError("rate limited") from e
        except a.APIStatusError as e:
            raise LLMError(f"API {e.status_code}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"connection: {e}") from e
        except TypeError as e:  # no credentials resolved at request time
            raise LLMUnavailable(str(e)) from e
        except ValidationError as e:  # output didn't match schema (e.g. truncated / refused text)
            raise LLMError(f"unparseable output: {e.error_count()} errors") from e

        if resp.stop_reason == "refusal":
            raise LLMError("refused")
        if resp.stop_reason == "max_tokens":
            raise LLMError("max_tokens reached; raise llm.max_tokens")
        if resp.parsed_output is None:
            raise LLMError("no parsed output")
        return resp.parsed_output


def build_provider(cfg: LlmConfig) -> LLMProvider | None:
    if not cfg.enabled:
        return None
    if cfg.provider != "anthropic":
        raise ValueError(f"unknown llm.provider {cfg.provider!r}")
    try:
        return AnthropicProvider(cfg)
    except Exception as e:  # SDK missing or client can't be built: rules-only mode
        log.warning("LLM disabled: %s", e)
        return None
