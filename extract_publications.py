#!/usr/bin/env python3
"""
Walk PDFs under ``.librarian/data`` (by default), send extracted text to OpenRouter
(openai/gpt-oss-120b), and write structured extractions to a JSON file.

Requires: OPENROUTER_API_KEY in the environment; install deps with ``python -m pip install -r requirements.txt``.

Usage:
  python extract_publications.py
  python extract_publications.py --limit 2 --output ./sample.json

**Default is full status output**: timestamped ``[librarian HH:MM:SS]`` lines for each
phase (startup, discovery, per-PDF read/API/save), plus a tqdm bar. Use ``--quiet`` /
``-q`` only if you want minimal output.

OpenRouter uses a **read timeout** (default 10s; use ``--timeout`` if responses need more time) and a short connect timeout;
every 25s a ``still waiting`` line is printed unless ``--no-wait-hints`` is set.

**Privacy:** the full extracted paper text (up to a character cap) is sent to OpenRouter. Do not use
on PDFs you are not allowed to send to a third party.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import json_repair  # type: ignore[import-untyped]
except ImportError:
    json_repair = None  # optional; install json-repair for better LLM JSON recovery

import requests
from pypdf import PdfReader
from tqdm import tqdm

def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no-op if file missing)."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            k = key.strip()
            v = value.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_TEXT_CHARS = 140_000
# TCP/handshake limit (seconds); read timeout is separate (see openrouter_extract).
CONNECT_TIMEOUT_S = 30
# Read timeout for each HTTP response (increase with --timeout if the model or network is slow).
DEFAULT_READ_TIMEOUT_S = 10
WAIT_HINT_INTERVAL_S = 25.0


EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "abstract",
        "doi",
        "taxonomy",
        "motivation",
        "contribution",
        "research_gap",
        "method_delineation",
        "results",
        "discussion_points",
    ],
    "properties": {
        "title": {"type": "string"},
        "abstract": {"type": "string"},
        "doi": {
            "anyOf": [
                {"type": "string"},
                {"type": "null"},
            ]
        },
        "taxonomy": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "primary_domain",
                "subdomains",
                "methods_tags",
                "application_areas",
                "keywords",
            ],
            "properties": {
                "primary_domain": {"type": "string"},
                "subdomains": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "methods_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "application_areas": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "motivation": {"type": "string"},
        "contribution": {"type": "string"},
        "research_gap": {"type": "string"},
        "method_delineation": {"type": "string"},
        "results": {"type": "string"},
        "discussion_points": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


def default_pdf_dir() -> Path:
    """Default folder for PDFs: ``.librarian/data`` (next to this script)."""
    return Path(__file__).resolve().parent / "data"


def safe_pdf_path(root: Path, source_pdf: str) -> Path:
    """Join *source_pdf* under *root*; reject absolute paths and ``..`` escapes."""
    s = (source_pdf or "").strip()
    if not s:
        raise ValueError("empty source_pdf")
    p = Path(s)
    if p.is_absolute():
        raise ValueError("source_pdf must be a relative path under the PDF root")
    root_r = root.resolve()
    candidate = (root_r / s).resolve()
    if not candidate.is_relative_to(root_r):
        raise ValueError("source_pdf path resolves outside the PDF root")
    return candidate


def _http_err_snippet(text: str | None, limit: int = 500) -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ").strip()
    if not t:
        return ""
    return t[:limit] + ("…" if len(t) > limit else "")


def _safe_log_payload(obj: object, limit: int = 800) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(obj)
    return s[:limit] + ("…" if len(s) > limit else "")


def extract_first_json_object(text: str) -> str | None:
    """Return the first top-level `{ ... }` slice, respecting quoted strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    i = start
    in_str = False
    esc = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def parse_llm_json_response(content: str) -> dict[str, Any]:
    """Parse JSON from an LLM message: fences, strict json.loads, brace slice, json_repair."""
    if content is None or not str(content).strip():
        raise ValueError("empty model content")

    s = str(content).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s)
    if m:
        s = m.group(1).strip()

    errors: list[str] = []
    candidates: list[str] = [s]
    balanced = extract_first_json_object(s)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            errors.append(f"json.loads: {e}")

    if json_repair is not None:
        for cand in candidates:
            try:
                obj = json_repair.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except Exception as e:
                errors.append(f"json_repair.loads: {e!s}")

    tail = "; ".join(errors[-4:]) if errors else "no parse attempts"
    raise ValueError(f"could not parse JSON object ({tail})")


def extract_pdf_text(
    path: Path, max_chars: int = MAX_TEXT_CHARS
) -> tuple[str, dict[str, Any]]:
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    chunks: list[str] = []
    n = 0
    pages_with_text = 0
    truncated = False
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if not t.strip():
            continue
        pages_with_text += 1
        if n + len(t) > max_chars:
            chunks.append(t[: max(0, max_chars - n)])
            truncated = True
            break
        chunks.append(t)
        n += len(t)
    text = "\n\n".join(chunks).strip()
    stats: dict[str, Any] = {
        "total_pages": total_pages,
        "pages_with_text": pages_with_text,
        "char_count": len(text),
        "truncated": truncated,
    }
    return text, stats


def openrouter_extract(
    api_key: str,
    model: str,
    pdf_text: str,
    source_name: str,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call OpenRouter chat completions and return parsed JSON plus response metadata.

    ``read_timeout_s`` is the maximum time to wait for each HTTP *response* (large
    prompts can take many minutes). Connection setup uses ``CONNECT_TIMEOUT_S``.
    """
    req_timeout = (CONNECT_TIMEOUT_S, read_timeout_s)
    system = (
        "You are a research assistant. Read the scholarly paper text provided by the user. "
        "Infer title, abstract, and DOI when present in the text; use null for doi only if "
        "no DOI appears. Fill every required JSON field carefully from the paper. "
        "For taxonomy, assign a concise primary_domain, lists of subdomains, method tags, "
        "application areas, and keywords. discussion_points should be short bullets suitable "
        "for a reading group (limitations, implications, open questions)."
    )
    user = (
        f"Source filename (for disambiguation only): {source_name}\n\n"
        "--- BEGIN PAPER TEXT ---\n"
        f"{pdf_text}\n"
        "--- END PAPER TEXT ---"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/local/librarian",
        "X-Title": "Publications librarian extractor",
    }

    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "paper_extraction",
                "strict": True,
                "schema": EXTRACTION_JSON_SCHEMA,
            },
        },
    }

    used_json_object_fallback = False
    t0 = time.perf_counter()
    r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=req_timeout)
    if r.status_code >= 400:
        # Retry once with basic JSON mode if schema is rejected
        if r.status_code in (400, 422):
            body.pop("response_format", None)
            body["response_format"] = {"type": "json_object"}
            used_json_object_fallback = True
            r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=req_timeout)
        if r.status_code >= 400:
            snip = _http_err_snippet(r.text, 500)
            raise RuntimeError(
                f"OpenRouter error {r.status_code}" + (f": {snip}" if snip else "")
            )

    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {_safe_log_payload(data)}")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError(f"Empty model content: {_safe_log_payload(data)}")

    try:
        parsed = parse_llm_json_response(content)
    except ValueError as e:
        raise RuntimeError(f"Model JSON parse failed: {e}") from e

    elapsed = time.perf_counter() - t0
    meta: dict[str, Any] = {
        "elapsed_s": round(elapsed, 2),
        "response_model": data.get("model"),
        "usage": data.get("usage"),
        "finish_reason": choices[0].get("finish_reason"),
        "json_object_fallback": used_json_object_fallback,
    }
    return parsed, meta


def load_existing(path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    if not path.is_file():
        return [], set()
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict) and "papers" in raw:
        items = [x for x in raw["papers"] if isinstance(x, dict)]
    else:
        items = []
    seen = {str(x.get("source_pdf", "")) for x in items}
    return items, {s for s in seen if s}


def main() -> int:
    p = argparse.ArgumentParser(description="Extract structured metadata from PDFs via OpenRouter.")
    p.add_argument(
        "--dir",
        type=Path,
        default=None,
        dest="root",
        help="Folder containing PDFs (default: .librarian/data)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (default: .librarian/output/publications_extractions.json)",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model id")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N PDFs (0 = no limit)",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip PDFs whose basename already appears as source_pdf in the output file",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to sleep between API calls",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Minimal output only (disables progress bar and timestamped status lines)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT_S,
        metavar="SEC",
        help=(
            f"Max seconds to wait for each OpenRouter HTTP response (default: {DEFAULT_READ_TIMEOUT_S}); "
            f"TCP connect timeout is {CONNECT_TIMEOUT_S}s"
        ),
    )
    p.add_argument(
        "--no-wait-hints",
        action="store_true",
        help="Disable periodic 'still waiting' messages during long API calls",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parent / ".env",
        metavar="PATH",
        help="Path to a .env file to load credentials from (default: .librarian/.env)",
    )
    args = p.parse_args()
    load_env_file(args.env_file)
    quiet = args.quiet

    def status(msg: str) -> None:
        """Timestamped line; uses tqdm.write so the progress bar stays usable."""
        if quiet:
            return
        tqdm.write(f"[librarian {time.strftime('%H:%M:%S')}] {msg}")

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("Missing OPENROUTER_API_KEY in environment.", file=sys.stderr)
        return 1

    root = (args.root or default_pdf_dir()).resolve()
    out_path = (args.output or (Path(__file__).resolve().parent / "output" / "publications_extractions.json")).resolve()

    if not quiet:
        status(f"starting — model `{args.model}`")
        status(f"PDF directory: {root}")
        status(f"output JSON: {out_path}")
        status(
            f"HTTP timeouts: connect {CONNECT_TIMEOUT_S}s, read {args.timeout:g}s "
            f"(override read with --timeout)"
        )
        if args.sleep > 0:
            status(f"pause between PDFs: {args.sleep:g}s (--sleep)")
        if args.no_wait_hints:
            status("periodic API wait hints: off (--no-wait-hints)")

    pdfs = sorted(root.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found under {root}", file=sys.stderr)
        return 1

    if not quiet:
        status(f"discovered {len(pdfs)} PDF file(s) in folder")

    records, seen_sources = load_existing(out_path) if args.skip_existing else ([], set())
    if args.skip_existing and not quiet:
        status(
            f"skip-existing: loaded {len(records)} record(s) from output "
            f"({len(seen_sources)} source_pdf basename(s) to skip)"
        )

    work: list[Path] = []
    for pdf in pdfs:
        key = pdf.name
        if args.skip_existing and key in seen_sources:
            status(f"skip (already in output): {key}")
            continue
        work.append(pdf)
        if args.limit and len(work) >= args.limit:
            break

    if not work:
        if quiet:
            print("Nothing to do.", flush=True)
        else:
            status(
                "nothing to do — no PDFs queued after filters (check --limit / skip-existing)."
            )
        return 0

    if not quiet:
        lim = f", limit this run: {args.limit}" if args.limit else ""
        status(
            f"queued {len(work)} PDF(s) to process this run (of {len(pdfs)} in folder{lim})"
        )

    bar = tqdm(
        work,
        desc="Publications",
        unit="pdf",
        disable=quiet,
        dynamic_ncols=True,
        mininterval=0.25,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
    )

    for idx, pdf in enumerate(bar, start=1):
        key = pdf.name
        if not quiet:
            bar.set_postfix_str(key[:48] + ("..." if len(key) > 48 else ""), refresh=True)
            status(f"--- [{idx}/{len(work)}] current file: {key} ---")

        status("phase: extracting text from PDF (local, no network) …")
        try:
            text, pdf_stats = extract_pdf_text(pdf)
        except Exception as e:
            print(f"  PDF read failed: {e}", file=sys.stderr)
            record = {
                "source_pdf": key,
                "error": f"pdf_extract_failed: {e}",
            }
            records.append(record)
            status(f"[err] skipped — PDF read failed: {e}")
            continue

        trunc = " (truncated to max text)" if pdf_stats.get("truncated") else ""
        status(
            f"phase: PDF text ready — {pdf_stats['char_count']:,} chars, "
            f"{pdf_stats['pages_with_text']}/{pdf_stats['total_pages']} pages with text{trunc}"
        )

        if len(text) < 200:
            msg = f"very little text extracted ({len(text)} chars)"
            print(f"Warning: {msg}", file=sys.stderr)
            status(f"[warn] {msg}")

        prompt_chars = len(text) + len(key) + 200
        status(
            f"phase: calling OpenRouter — read timeout {args.timeout:g}s, "
            f"connect {CONNECT_TIMEOUT_S}s; rough prompt size ~{prompt_chars:,} chars …"
        )

        stop_hint = threading.Event()

        def wait_hints_loop() -> None:
            n = 0
            while not stop_hint.wait(WAIT_HINT_INTERVAL_S):
                n += 1
                sec = int(n * WAIT_HINT_INTERVAL_S)
                line = (
                    f"[librarian {time.strftime('%H:%M:%S')}] "
                    f"still waiting on OpenRouter ({sec}s elapsed) — "
                    f"model processing; hard read limit {args.timeout:g}s"
                )
                if quiet:
                    print(line, file=sys.stderr, flush=True)
                else:
                    tqdm.write(line)

        if not args.no_wait_hints:
            threading.Thread(target=wait_hints_loop, daemon=True).start()

        try:
            extracted, api_meta = openrouter_extract(
                api_key,
                args.model,
                text,
                key,
                read_timeout_s=args.timeout,
            )
        except Exception as e:
            print(f"  API failed: {e}", file=sys.stderr)
            records.append({"source_pdf": key, "error": str(e)})
            status(f"[err] OpenRouter request failed: {e}")
            time.sleep(args.sleep)
            continue
        finally:
            stop_hint.set()

        usage = api_meta.get("usage") or {}
        pt = usage.get("prompt_tokens", "?")
        ct = usage.get("completion_tokens", "?")
        cost = usage.get("cost")
        cost_s = f", cost≈{cost}" if cost is not None else ""
        fr = api_meta.get("finish_reason") or "?"
        fb = (
            " [response_format fallback: json_object]"
            if api_meta.get("json_object_fallback")
            else ""
        )
        status(
            f"phase: response received — {api_meta.get('elapsed_s')}s wall time, "
            f"tokens in/out {pt}/{ct}{cost_s}, finish={fr}{fb}"
        )
        title_preview = (extracted.get("title") or "")[:80]
        if title_preview:
            t_full = str(extracted.get("title") or "")
            status(f"parsed title: {title_preview}{'...' if len(t_full) > 80 else ''}")

        row = {
            "source_pdf": key,
            **extracted,
        }
        records.append(row)
        seen_sources.add(key)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

        status(f"phase: wrote JSON — {out_path} ({len(records)} record(s) in file)")

        if args.sleep > 0:
            status(f"phase: sleeping {args.sleep:g}s before next PDF (--sleep) …")
        time.sleep(args.sleep)

    if not quiet:
        status(f"finished — {len(records)} record(s) total in {out_path}")
    else:
        print(f"Done. {len(records)} record(s) → {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
