"""Offline tests for the trusted LLM judgement (Week 5) -- no network required.

Every model call is replaced by an httpx.MockTransport with canned replies, so
the tests prove the trust machinery (schema, timeout, retries that stop, error
mapping) deterministically without spending credit.

Run with:  python test_llm_receipt.py
"""

import json

import httpx
from fastapi.testclient import TestClient

import llm
from llm import (
    InvalidModelOutputError,
    LLMClient,
    LLMUnavailableError,
    Receipt,
)

VALID_RECEIPT_JSON = json.dumps(
    {
        "merchant": "The Corner Cafe",
        "date": "2026-09-20",
        "currency": "GBP",
        "total": 9.74,
        "line_items": [
            {"description": "Flat white", "amount": 3.5},
            {"description": "Toast", "amount": 4.24},
            {"description": "Tip", "amount": 2.0},
        ],
    }
)


def make_handler(*replies):
    """Build an httpx.MockTransport handler returning canned replies in order.

    A reply may be: a string (model content), an int (HTTP status), or an
    Exception (transport failure such as a timeout).
    """
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        reply = replies[min(state["calls"], len(replies) - 1)]
        state["calls"] += 1
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "refused"}}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": reply}}]},
            request=request,
        )

    return handler, state


def make_client(handler) -> LLMClient:
    return LLMClient(
        api_key="sk-test-key",
        base_url="https://llm.test/v1",
        model="test-model",
        max_retries=3,
        retry_backoff=0,
        transport=httpx.MockTransport(handler),
    )


def test_valid_receipt_is_validated_and_returned():
    handler, state = make_handler(VALID_RECEIPT_JSON)
    receipt = make_client(handler).judge_text("Corner Cafe / flat white 3.5 / toast 4.24 / tip 2", Receipt)
    assert state["calls"] == 1
    assert receipt.merchant == "The Corner Cafe"
    assert receipt.total == 9.74
    assert receipt.currency == "GBP"
    assert len(receipt.line_items) == 3
    assert receipt.matches_line_items()


def test_markdown_fenced_reply_is_parsed():
    fenced = "```json\n" + VALID_RECEIPT_JSON + "\n```"
    handler, _ = make_handler(fenced)
    receipt = make_client(handler).judge_text("any", Receipt)
    assert receipt.merchant == "The Corner Cafe"


def test_prose_around_json_is_tolerated():
    prose = "Here you go, sir: " + VALID_RECEIPT_JSON + " -- thank you"
    handler, _ = make_handler(prose)
    receipt = make_client(handler).judge_text("any", Receipt)
    assert receipt.total == 9.74


def test_gibberish_then_reasonable_reply_retries_once():
    handler, state = make_handler("this is absolutely not json", VALID_RECEIPT_JSON)
    receipt = make_client(handler).judge_text("any", Receipt)
    assert state["calls"] == 2
    assert receipt.total == 9.74


def test_schema_violation_triggers_retry_not_crash():
    bad = json.dumps({"merchant": "X", "total": -5, "currency": "GBP", "line_items": []})
    handler, state = make_handler(bad, VALID_RECEIPT_JSON)
    receipt = make_client(handler).judge_text("any", Receipt)
    assert state["calls"] == 2
    assert receipt.total == 9.74


def test_garbage_output_stops_after_max_retries():
    handler, state = make_handler("nonsense forever")
    try:
        make_client(handler).judge_text("any", Receipt)
        raise AssertionError("garbage should have been rejected")
    except InvalidModelOutputError:
        assert state["calls"] == 3  # exactly max_retries, then stop


def test_timeout_raises_unavailable_and_stops():
    handler, state = make_handler(httpx.ReadTimeout("slow as molasses"))
    try:
        make_client(handler).judge_text("any", Receipt)
        raise AssertionError("timeout should have been rejected")
    except LLMUnavailableError as exc:
        assert "timed out" in str(exc)
        assert state["calls"] == 3  # capped retries, no infinite loop


def test_auth_error_fails_fast_without_retrying():
    handler, state = make_handler(401)
    try:
        make_client(handler).judge_text("any", Receipt)
        raise AssertionError("401 should not be retried")
    except LLMUnavailableError:
        assert state["calls"] == 1  # one call, then give up


def test_endpoint_behaviour():
    app_client = TestClient(llm.app if hasattr(llm, "app") else __import__("main").app)

    empty = app_client.post("/ai/parse-receipt", json={"text": ""})
    assert empty.status_code == 400

    blank = app_client.post("/ai/parse-receipt", json={"text": "   "})
    assert blank.status_code == 400

    original_key = llm.LLM_API_KEY
    llm.LLM_API_KEY = ""
    try:
        unconfigured = app_client.post("/ai/parse-receipt", json={"text": "receipt text"})
        assert unconfigured.status_code == 503
        assert "not configured" in unconfigured.json()["error"]
    finally:
        llm.LLM_API_KEY = original_key


def main() -> None:
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_"))
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"\n{len(tests)} LLM judgement tests passed")


if __name__ == "__main__":
    main()