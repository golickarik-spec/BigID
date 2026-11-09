#!/usr/bin/env python3
"""Diagnose a failed CI run with Claude and emit a short markdown report.

Reads the failed-job logs from a file, sends the tail to the Anthropic Messages
API, and writes a markdown diagnosis to an output file (default: diagnosis.md)
and to stdout. Degrades gracefully: if ANTHROPIC_API_KEY is absent or the API
call fails, it writes a clear notice instead of crashing, so the CI step never
masks the real failure.

Uses only the Python standard library (urllib) — no SDK install needed in CI.

Usage:
    python scripts/ai_diagnose.py <logs_file> [output_file]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"
# Keep well under token limits: send only the tail of the logs, where the
# failure almost always surfaces.
MAX_LOG_CHARS = 16000
TIMEOUT_SECONDS = 60

PROMPT = """You are a senior DevOps/SRE engineer triaging a failed CI pipeline.

Below are the logs of the failed job(s). Respond in GitHub-flavored markdown,
concise (<= ~200 words), with exactly these sections:

**Failing step** — which step/command failed.
**Likely root cause** — the most probable cause, stated plainly.
**Suggested fix** — concrete, actionable next step(s).

Do not invent details not supported by the logs. If the logs are truncated or
inconclusive, say so.

--- BEGIN LOGS ---
{logs}
--- END LOGS ---
"""


def write_output(out_path: str, text: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    # Be robust to non-UTF-8 consoles (e.g. a Windows code page) so emoji in the
    # report never crash the step.
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace") + b"\n")


def diagnose(logs: str, api_key: str) -> str:
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 700,
            "messages": [
                {"role": "user", "content": PROMPT.format(logs=logs)},
            ],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    parts = [block.get("text", "") for block in payload.get("content", [])]
    return "".join(parts).strip() or "_(empty response from model)_"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ai_diagnose.py <logs_file> [output_file]", file=sys.stderr)
        return 2

    logs_file = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "diagnosis.md"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        write_output(
            out_path,
            "### 🤖 AI diagnosis skipped\n\n"
            "`ANTHROPIC_API_KEY` is not configured for this repository, so the "
            "automated failure diagnosis was skipped. Add the secret to enable it.",
        )
        return 0

    try:
        with open(logs_file, encoding="utf-8", errors="replace") as fh:
            logs = fh.read()
    except OSError as exc:
        write_output(out_path, f"### 🤖 AI diagnosis\n\nCould not read logs: {exc}")
        return 0

    logs = logs.strip()
    if not logs:
        write_output(out_path, "### 🤖 AI diagnosis\n\nNo failed-job logs were captured.")
        return 0

    logs = logs[-MAX_LOG_CHARS:]

    try:
        diagnosis = diagnose(logs, api_key)
        write_output(out_path, f"### 🤖 AI failure diagnosis\n\n{diagnosis}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        write_output(
            out_path,
            f"### 🤖 AI diagnosis\n\nAnthropic API error {exc.code}: {detail}",
        )
    except Exception as exc:  # noqa: BLE001 - never let diagnosis mask the real failure
        write_output(out_path, f"### 🤖 AI diagnosis\n\nDiagnosis failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
