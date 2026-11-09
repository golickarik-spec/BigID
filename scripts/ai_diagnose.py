#!/usr/bin/env python3
"""Diagnose a failed CI run with Claude and render a polished markdown report.

Reads the failed-job logs from a file, asks the Anthropic Messages API for a
STRUCTURED JSON diagnosis, then renders it into a fixed markdown template
(confidence badge + context table + sections + collapsible log excerpt) so the
output looks consistent regardless of the model's phrasing. Falls back to a
prose rendering if the response isn't valid JSON.

Degrades gracefully: if ANTHROPIC_API_KEY is absent or the API call fails, it
writes a clear notice instead of crashing, so the CI step never masks the real
failure. Uses only the Python standard library (urllib) — no SDK needed in CI.

Usage:
    python scripts/ai_diagnose.py <logs_file> [output_file]

Context is read from the standard GitHub Actions environment variables
(GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SHA,
GITHUB_REF_NAME) plus FAILED_JOBS exported by the workflow.
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
MAX_LOG_CHARS = 16000       # sent to the model (tail of the logs)
EXCERPT_CHARS = 2500        # shown in the collapsible <details> block
TIMEOUT_SECONDS = 60

PROMPT = """You are a senior DevOps/SRE engineer triaging a failed CI pipeline.

Analyse the failed-job logs below and respond with ONLY a JSON object (no prose,
no markdown, no code fences) with exactly these keys:

{{
  "failing_step": "which step/command failed (one line)",
  "root_cause": "the most likely root cause, stated plainly (1-3 sentences)",
  "suggested_fix": ["concrete actionable step", "another step"],
  "confidence": "high | medium | low",
  "category": "short kebab-case label, e.g. test-failure, lint, dependency, build, deploy"
}}

Base everything strictly on the logs. If they are truncated or inconclusive, say
so in root_cause and set confidence to "low".

--- BEGIN LOGS ---
{logs}
--- END LOGS ---
"""

BADGES = {
    "high": "🟢 **High confidence**",
    "medium": "🟡 **Medium confidence**",
    "low": "🔴 **Low confidence**",
}


def write_output(out_path: str, text: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace") + b"\n")


def call_claude(logs: str, api_key: str) -> str:
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 800,
            "messages": [{"role": "user", "content": PROMPT.format(logs=logs)}],
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
    return "".join(parts).strip()


def parse_json(text: str) -> dict | None:
    """Best-effort extraction of the JSON object from the model's reply."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate[candidate.find("{"):]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(candidate[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def context() -> dict:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    sha = os.environ.get("GITHUB_SHA", "")
    return {
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
        "run_id": run_id,
        "commit_url": f"{server}/{repo}/commit/{sha}" if repo and sha else "",
        "short_sha": sha[:7] if sha else "unknown",
        "branch": os.environ.get("GITHUB_REF_NAME", "unknown"),
        "failed_jobs": os.environ.get("FAILED_JOBS", "unknown"),
    }


def render_table(ctx: dict) -> str:
    run_cell = f"[#{ctx['run_id']}]({ctx['run_url']})" if ctx["run_url"] else "—"
    short = ctx["short_sha"]
    commit_cell = f"[`{short}`]({ctx['commit_url']})" if ctx["commit_url"] else f"`{short}`"
    return (
        "| | |\n|---|---|\n"
        f"| **Run** | {run_cell} |\n"
        f"| **Commit** | {commit_cell} |\n"
        f"| **Branch** | `{ctx['branch']}` |\n"
        f"| **Failed job(s)** | {ctx['failed_jobs']} |\n"
        f"| **Model** | `{MODEL}` |\n"
    )


def collapsible_logs(logs: str) -> str:
    excerpt = logs[-EXCERPT_CHARS:].strip()
    if not excerpt:
        return ""
    return (
        "\n<details><summary>📄 Log excerpt (tail)</summary>\n\n"
        "```text\n" + excerpt + "\n```\n</details>\n"
    )


def render(diag: dict, ctx: dict, logs: str) -> str:
    confidence = str(diag.get("confidence", "medium")).lower().strip()
    badge = BADGES.get(confidence, BADGES["medium"])
    category = str(diag.get("category", "unknown")).strip() or "unknown"

    fixes = diag.get("suggested_fix", [])
    if isinstance(fixes, str):
        fixes = [fixes]
    steps = "\n".join(f"{i}. {str(f).strip()}" for i, f in enumerate(fixes, 1))
    fix_md = steps or "_None provided._"

    return (
        "## 🤖 CI Failure Diagnosis\n\n"
        f"{badge} · **Category:** `{category}`\n\n"
        f"{render_table(ctx)}\n"
        "### Failing step\n"
        f"{str(diag.get('failing_step', 'Unknown')).strip()}\n\n"
        "### Likely root cause\n"
        f"{str(diag.get('root_cause', 'Unknown')).strip()}\n\n"
        "### Suggested fix\n"
        f"{fix_md}\n"
        f"{collapsible_logs(logs)}"
        "\n---\n"
        f"*Automated diagnosis by Claude `{MODEL}` — verify before acting.*"
    )


def render_fallback(raw: str, ctx: dict, logs: str) -> str:
    return (
        "## 🤖 CI Failure Diagnosis\n\n"
        "🟡 **Medium confidence** · **Category:** `unstructured`\n\n"
        f"{render_table(ctx)}\n"
        f"{raw.strip() or '_No diagnosis text returned._'}\n"
        f"{collapsible_logs(logs)}"
        "\n---\n"
        f"*Automated diagnosis by Claude `{MODEL}` — verify before acting.*"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ai_diagnose.py <logs_file> [output_file]", file=sys.stderr)
        return 2

    logs_file = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "diagnosis.md"
    ctx = context()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        write_output(
            out_path,
            "## 🤖 CI Failure Diagnosis\n\n"
            "_Skipped: `ANTHROPIC_API_KEY` is not configured for this repository. "
            "Add the secret to enable automated failure diagnosis._",
        )
        return 0

    try:
        with open(logs_file, encoding="utf-8", errors="replace") as fh:
            logs = fh.read().strip()
    except OSError as exc:
        write_output(out_path, f"## 🤖 CI Failure Diagnosis\n\nCould not read logs: {exc}")
        return 0

    if not logs:
        write_output(out_path, "## 🤖 CI Failure Diagnosis\n\nNo failed-job logs were captured.")
        return 0

    model_logs = logs[-MAX_LOG_CHARS:]
    try:
        raw = call_claude(model_logs, api_key)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        msg = f"## 🤖 CI Failure Diagnosis\n\nAnthropic API error {exc.code}: {detail}"
        write_output(out_path, msg)
        return 0
    except Exception as exc:  # noqa: BLE001 - never let diagnosis mask the real failure
        write_output(out_path, f"## 🤖 CI Failure Diagnosis\n\nDiagnosis failed: {exc}")
        return 0

    diag = parse_json(raw)
    output = render(diag, ctx, logs) if diag else render_fallback(raw, ctx, logs)
    write_output(out_path, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
