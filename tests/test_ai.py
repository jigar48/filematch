import json
from types import SimpleNamespace

import markupsafe
import polars as pl
import pytest

from filematch import ai
from tests.conftest import make_csv, make_xlsx, upload

LONG = "x" * 80
A_CSV = make_csv(
    [["CustID", "FName", "Secret"]]
    + [[f"{i:03d}", f"Name{i}", LONG + str(i)] for i in range(12)]
    + [["", "", ""]]
)
B_XLSX = make_xlsx([["entity_id", "firstname"]] + [[f"{i:03d}", f"Name{i}"] for i in range(12)])


class FakeMessages:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.text = text
        self.stop_reason = stop_reason
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=self.text)],
        )


class FakeClient:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.messages = FakeMessages(text, stop_reason)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to build a real client fails the test."""

    def boom():
        raise AssertionError("a real Anthropic client must never be constructed in tests")

    monkeypatch.setattr(ai, "make_client", boom)


def test_without_key_no_button_and_no_client(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    assert "Suggest with AI" not in client.get("/mapping").text
    r = client.post("/mapping/ai", follow_redirects=False)
    assert r.status_code == 303
    assert "not configured" in client.get("/mapping").text
    with pytest.raises(ai.AIError):
        ai.suggest_pairs({}, "m", [], [])


def test_payload_is_headers_and_short_samples_only():
    df = pl.DataFrame(
        {
            "id": [str(i) for i in range(20)],
            "note": [LONG] * 3 + ["", "  ", None] + [f"v{i}" for i in range(10)] + [None] * 4,
        }
    )
    payload = ai.build_payload(df, df)
    assert set(payload) == {"file_a_columns", "file_b_columns"}
    for col in payload["file_a_columns"]:
        assert set(col) == {"name", "samples"}
        assert len(col["samples"]) <= 5
        assert len(set(col["samples"])) == len(col["samples"])
        assert all(s.strip() and len(s) <= 50 for s in col["samples"])
    note = payload["file_a_columns"][1]["samples"]
    assert note == ["x" * 50, "v0", "v1", "v2", "v3"]


def test_with_key_mocked_client_gets_only_headers_and_samples(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ai, "sdk_installed", lambda: True)
    fake = FakeClient(
        json.dumps(
            {
                "pairs": [
                    {"a": "Secret", "b": "firstname", "confidence": 40},
                    {"a": "Ghost", "b": "firstname", "confidence": 99},
                    {"a": "CustID", "b": "nope", "confidence": 99},
                ]
            }
        )
    )
    monkeypatch.setattr(ai, "make_client", lambda: fake)
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))

    page = client.get("/mapping").text
    assert "Suggest with AI" in page
    preview = client.get("/mapping/ai").text
    assert "CustID" in preview and "entity_id" in preview
    assert "Name11" not in preview  # only the first 5 distinct samples

    r = client.post("/mapping/ai", follow_redirects=False)
    assert r.status_code == 303
    assert len(fake.messages.calls) == 1
    call = fake.messages.calls[0]
    assert set(call) == {"model", "max_tokens", "messages", "output_config"}
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    content = call["messages"][0]["content"]
    sent = json.loads(content[content.index("{") :])
    columns = sent["file_a_columns"] + sent["file_b_columns"]
    assert [c["name"] for c in columns] == ["CustID", "FName", "Secret", "entity_id", "firstname"]
    for col in columns:
        assert len(col["samples"]) <= 5
        assert all(len(s) <= 50 for s in col["samples"])
    assert LONG not in content
    # the preview showed exactly what was sent
    assert str(markupsafe.escape(content.split("\n")[0])) in preview

    page = client.get("/mapping").text
    assert "AI suggested 1 pair(s)" in page  # unknown-column pairs were dropped


def test_ai_error_keeps_fuzzy_results(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ai, "sdk_installed", lambda: True)
    monkeypatch.setattr(ai, "make_client", lambda: FakeClient("not json"))
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    client.post("/mapping/ai", follow_redirects=False)
    page = client.get("/mapping").text
    assert "not valid JSON" in page
    assert "synonym" in page


def test_ai_api_exception_is_short_and_has_no_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ai, "sdk_installed", lambda: True)

    class Exploding:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                raise TimeoutError("test-key-not-real timed out")

    with pytest.raises(ai.AIError) as info:
        ai.suggest_pairs({}, "m", ["a"], ["b"], client=Exploding())
    assert "TimeoutError" in str(info.value)
    assert "test-key-not-real" not in str(info.value)


@pytest.mark.parametrize(
    "text",
    ["[]", '{"pairs": "x"}', '{"other": []}', "{"],
)
def test_strict_parsing_rejects_bad_shapes(text):
    with pytest.raises(ai.AIError):
        ai.parse_pairs(text, ["a"], ["b"])


def test_parsing_drops_bad_items_and_clamps():
    text = json.dumps(
        {
            "pairs": [
                {"a": "a", "b": "b", "confidence": 150},
                {"a": "a", "b": "b", "confidence": True},
                {"a": 1, "b": "b", "confidence": 5},
                "junk",
            ]
        }
    )
    pairs = ai.parse_pairs(text, ["a"], ["b"])
    assert [(p.a, p.b, p.score, p.source) for p in pairs] == [("a", "b", 100, "AI")]


def test_incomplete_response_is_an_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ai, "sdk_installed", lambda: True)
    with pytest.raises(ai.AIError, match="incomplete"):
        ai.suggest_pairs({}, "m", [], [], client=FakeClient("{}", stop_reason="max_tokens"))


def test_model_from_env(monkeypatch):
    from filematch.config import Settings

    monkeypatch.setenv("FILEMATCH_AI_MODEL", "claude-other")
    assert Settings.from_env().ai_model == "claude-other"
    monkeypatch.delenv("FILEMATCH_AI_MODEL")
    assert Settings.from_env().ai_model == "claude-haiku-5-5"
