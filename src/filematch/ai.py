"""Optional AI column-mapping suggestions (the `[ai]` extra).

Only header names and up to 5 short sample values per column are sent, only when
ANTHROPIC_API_KEY is set in the server environment and the user clicks the button.
"""

from __future__ import annotations

import importlib.util
import json
import os
from typing import Any

import polars as pl

from filematch.mapping import Candidate

MAX_SAMPLES = 5
MAX_SAMPLE_CHARS = 50
TIMEOUT_SECONDS = 30.0

INSTRUCTIONS = (
    "You map columns between two tabular files from a data migration. File A is the "
    "legacy export and File B is the target system's import. Using the column names "
    "and sample values below, propose one-to-one pairs (a File A column to a File B "
    "column) that hold the same data. Leave a column out if nothing fits. Use only "
    "column names that appear in the data. Give each pair a confidence from 0 to 100."
)

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "confidence": {"type": "integer"},
                },
                "required": ["a", "b", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}


class AIError(RuntimeError):
    """Short, user-facing description of why the AI call did not produce pairs."""


def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def sdk_installed() -> bool:
    return importlib.util.find_spec("anthropic") is not None


def available() -> bool:
    return api_key_present() and sdk_installed()


def column_samples(df: pl.DataFrame) -> list[dict[str, Any]]:
    out = []
    for name in df.columns:
        values = (
            df.lazy()
            .select(pl.col(name))
            .filter(pl.col(name).is_not_null() & (pl.col(name).str.strip_chars() != ""))
            .unique(maintain_order=True)
            .head(MAX_SAMPLES)
            .collect()
            .to_series()
            .to_list()
        )
        out.append({"name": name, "samples": [v[:MAX_SAMPLE_CHARS] for v in values]})
    return out


def build_payload(df_a: pl.DataFrame, df_b: pl.DataFrame) -> dict[str, Any]:
    return {"file_a_columns": column_samples(df_a), "file_b_columns": column_samples(df_b)}


def request_text(payload: dict[str, Any]) -> str:
    """The exact user message sent to the API (also shown to the user beforehand)."""
    return INSTRUCTIONS + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def make_client():  # pragma: no cover - exercised only with a real key
    import anthropic

    return anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"], timeout=TIMEOUT_SECONDS, max_retries=0
    )


def suggest_pairs(
    payload: dict[str, Any], model: str, columns_a: list[str], columns_b: list[str], client=None
) -> list[Candidate]:
    if not available():
        raise AIError("AI suggestions are not configured.")
    client = client or make_client()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            messages=[{"role": "user", "content": request_text(payload)}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        )
    except Exception as exc:  # timeouts, connection and API errors all end up here
        raise AIError(f"The AI request failed ({type(exc).__name__}).") from None
    if getattr(response, "stop_reason", None) not in ("end_turn", "stop_sequence"):
        raise AIError(f"The AI response was incomplete ({response.stop_reason}).")
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        raise AIError("The AI response had no text.")
    return parse_pairs(text, columns_a, columns_b)


def parse_pairs(text: str, columns_a: list[str], columns_b: list[str]) -> list[Candidate]:
    """Strictly parse the JSON response; drop pairs that name unknown columns."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise AIError("The AI response was not valid JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        raise AIError("The AI response did not have the expected shape.")
    known_a, known_b = set(columns_a), set(columns_b)
    out: list[Candidate] = []
    for item in data["pairs"]:
        if not isinstance(item, dict):
            continue
        a, b, conf = item.get("a"), item.get("b"), item.get("confidence")
        if not (isinstance(a, str) and isinstance(b, str)) or isinstance(conf, bool):
            continue
        if not isinstance(conf, int | float):
            continue
        if a not in known_a or b not in known_b:
            continue
        out.append(Candidate(a, b, max(0, min(100, int(conf))), "AI"))
    return out
