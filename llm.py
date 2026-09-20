"""Week 5 -- Trusted LLM judgement for a single workflow step.

"Integrate an LLM" here means one small, useful job done in code instead of by
a person: an endpoint takes messy receipt text, an AI model extracts the
structured fields, and -- crucially -- the answer is only trusted after it
passes through four gates:

  schema      every answer must match the Pydantic `Receipt` model exactly
  timeout     the model call has a hard deadline, it cannot hang forever
  retries     transient failures retry with backoff, but know when to stop:
              capped attempts, fenced retries, and no retry on auth errors
  tests       offline test cases prove each failure mode is handled

The provider is any OpenAI-compatible HTTP endpoint, configured via .env:
    LLM_API_KEY   (falls back to OPENAI_API_KEY)
    LLM_BASE_URL  (default https://api.openai.com/v1)
    LLM_MODEL     (default gpt-4o-mini)
    LLM_TIMEOUT   (seconds, default 20)
    LLM_MAX_RETRIES (default 3)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Type, TypeVar

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip() or os.getenv(
    "OPENAI_API_KEY", ""
).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
T = TypeVar("T", bound=BaseModel)


class JudgementError(Exception):
    """Base class for every failure this module can raise."""


class LLMNotConfiguredError(JudgementError):
    """No API key is available to call the model."""


class LLMUnavailableError(JudgementError):
    """The model provider was unreachable, timed out, or refused the call."""


class InvalidModelOutputError(JudgementError):
    """The model kept producing output that failed extraction/schema checks."""


class _RetryableRequestError(JudgementError):
    """Transient transport/HTTP failure worth retrying."""


class _FatalRequestError(JudgementError):
    """Provider refused the call (e.g. bad key) -- retrying would not help."""


class _JsonExtractError(JudgementError):
    """The model reply could not be parsed as JSON."""


class LineItem(BaseModel):
    description: str = Field(min_length=1)
    amount: float = Field(gt=0)

    @field_validator("amount")
    @classmethod
    def _round_amount(cls, value: float) -> float:
        return round(value, 2)


class Receipt(BaseModel):
    """Schema that every LLM answer must match before it is returned."""

    merchant: str = Field(min_length=1)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    currency: str = Field(default="GBP", pattern=r"^[A-Z]{3}$")
    total: float = Field(gt=0)
    line_items: list[LineItem] = Field(default_factory=list)

    @field_validator("total")
    @classmethod
    def _round_total(cls, value: float) -> float:
        return round(value, 2)

    def matches_line_items(self, tolerance: float = 0.05) -> bool:
        """Optional consistency gate: total should equal the sum of the items."""
        return abs(self.total - sum(item.amount for item in self.line_items)) <= tolerance


class ReceiptRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _text_is_usable(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("provide some receipt text")
        return cleaned


SYSTEM_PROMPT = (
    "You are an exact receipt parser. Follow the supplied JSON schema exactly "
    "and respond with a single JSON object and nothing else -- no commentary, "
    "no markdown code fences."
)


def extraction_prompt(schema: Type[BaseModel], text: str) -> str:
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    return (
        "Parse the following receipt text into a JSON object that conforms to "
        f"this schema:\n{schema_json}\n"
        'Rules:\n'
        '- "line_items" must be an array; use [] if no items are listed.\n'
        '- "total" must be the receipt total as a number.\n'
        '- "date" must be YYYY-MM-DD or null.\n'
        '- "currency" must be a 3-letter code (default GBP).\n'
        f"Receipt text:\n{text}"
    )


def extract_json(reply: str) -> dict[str, Any]:
    """Pull the first JSON object out of a raw model reply.

    Handles markdown fences, surrounding prose and trailing commas the way a
    trusted pipeline should: try the raw text, then the first {...} block.
    """
    raw = (reply or "").strip()
    if not raw:
        raise _JsonExtractError("model returned an empty reply")

    candidates = [raw]
    fenced = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", raw, flags=re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start and raw[start:end + 1] != raw:
        candidates.append(raw[start:end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise _JsonExtractError("model reply did not contain a valid JSON object")


class LLMClient:
    """Small, trustworthy front door to an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or LLM_API_KEY).strip()
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model = model or LLM_MODEL
        self.timeout = timeout if timeout is not None else LLM_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else LLM_MAX_RETRIES
        self.retry_backoff = retry_backoff
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            transport=transport,
        )

    def _complete(self, prompt: str) -> str:
        """One model call. Fatal on auth errors, retryable on blips."""
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise _RetryableRequestError(
                f"LLM request timed out after {self.timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise _RetryableRequestError(f"LLM request failed: {exc}") from exc

        if response.status_code in RETRYABLE_STATUSES:
            raise _RetryableRequestError(f"LLM responded HTTP {response.status_code}")
        if response.status_code >= 400:
            raise _FatalRequestError(f"LLM responded HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise _RetryableRequestError("LLM returned a non-JSON body") from exc
        try:
            content: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise _RetryableRequestError(
                'LLM response missing choices[0].message.content'
            ) from exc
        return content

    def judge_text(self, text: str, schema: Type[T]) -> T:
        """Ask the model for a structured judgement and only trust valid ones.

        Gates: schema, timeout, and bounded retries that know when to stop:
        at most `max_retries` model calls, no retries on fatal auth errors,
        backoff between attempts.
        """
        if not self.api_key:
            raise LLMNotConfiguredError(
                "LLM_API_KEY / OPENAI_API_KEY is not set (see .env.example)"
            )

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                reply = self._complete(extraction_prompt(schema, text))
                payload = extract_json(reply)
                return schema.model_validate(payload)
            except (ValidationError, _JsonExtractError) as exc:
                last_error = exc  # bad content -> re-ask
            except _RetryableRequestError as exc:
                last_error = exc  # transient -> retry
            except _FatalRequestError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff * attempt)

        if isinstance(last_error, _RetryableRequestError):
            raise LLMUnavailableError(str(last_error)) from last_error
        raise InvalidModelOutputError(
            f"model never produced output that passed the schema "
            f"({self.max_retries} attempts; last error: {last_error})"
        )


# Re-exported exceptions used by the API layer.
__all__ = [
    "LLMClient",
    "LLMNotConfiguredError",
    "LLMUnavailableError",
    "InvalidModelOutputError",
    "Receipt",
    "ReceiptRequest",
    "extract_json",
    "extraction_prompt",
]