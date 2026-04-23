#!/usr/bin/env python3
"""
Re-run OpenRouter extraction for JSON entries that only have ``source_pdf`` + ``error``
(typically ``json.loads`` / truncated LLM JSON). Uses the same pipeline as
``extract_publications.py``, including resilient parsing (brace extraction + json-repair).

Requires: OPENROUTER_API_KEY, dependencies from requirements.txt (incl. json-repair).

Examples:
  python extract_failed_cases.py --dry-run
  python extract_failed_cases.py --backup
  python extract_failed_cases.py --only "Fast Mesh Denoising"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

# Ensure sibling ``extract_publications`` is importable when run as a script.
_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


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
            os.environ.setdefault(key.strip(), value.strip())


def is_failed_record(r: object) -> bool:
    return isinstance(r, dict) and "error" in r and "source_pdf" in r


def load_records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise SystemExit(f"Expected a JSON array in {path}")
    return [x for x in raw if isinstance(x, dict)]


def save_records(path: Path, records: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Retry OpenRouter extraction for failed records in publications_extractions.json."
    )
    p.add_argument(
        "--json",
        type=Path,
        default=_LIB / "output" / "publications_extractions.json",
        help="Path to extractions JSON (array of objects)",
    )
    p.add_argument(
        "--dir",
        type=Path,
        default=None,
        dest="root",
        help="Folder containing PDFs (default: .librarian/data)",
    )
    p.add_argument("--model", default="openai/gpt-oss-120b")
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SEC",
        help="OpenRouter read timeout (default: same as extract_publications)",
    )
    p.add_argument(
        "--only",
        type=str,
        default="",
        help="Substring; retry only if source_pdf contains this (case-insensitive)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max retries this run (0 = no cap)",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds between API calls",
    )
    p.add_argument(
        "--backup",
        action="store_true",
        help="Copy JSON to .bak before overwriting",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List failed entries that would be retried, no API calls",
    )
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument(
        "--env-file",
        type=Path,
        default=_LIB / ".env",
        metavar="PATH",
        help="Path to a .env file to load credentials from (default: .librarian/.env)",
    )
    args = p.parse_args()
    load_env_file(args.env_file)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    jpath = args.json.resolve()
    if not jpath.is_file():
        print(f"Missing JSON file: {jpath}", file=sys.stderr)
        return 1

    records = load_records(jpath)
    only_pat = re.compile(re.escape(args.only), re.I) if args.only.strip() else None

    indices: list[int] = []
    for i, r in enumerate(records):
        if not is_failed_record(r):
            continue
        sp = str(r.get("source_pdf", ""))
        if only_pat and not only_pat.search(sp):
            continue
        indices.append(i)

    if not indices:
        log("No matching failed records (nothing to retry).")
        return 0

    log(f"Found {len(indices)} failed record(s) to retry.")
    for i in indices[:20]:
        log(f"  - {records[i].get('source_pdf')}: {records[i].get('error', '')[:120]}")
    if len(indices) > 20:
        log(f"  ... and {len(indices) - 20} more")

    if args.dry_run:
        return 0

    from extract_publications import (  # noqa: E402
        DEFAULT_READ_TIMEOUT_S,
        default_pdf_dir,
        extract_pdf_text,
        openrouter_extract,
    )

    read_timeout = (
        float(args.timeout) if args.timeout is not None else float(DEFAULT_READ_TIMEOUT_S)
    )
    root = (args.root or default_pdf_dir()).resolve()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("Missing OPENROUTER_API_KEY.", file=sys.stderr)
        return 1

    if args.backup:
        bak = jpath.with_suffix(jpath.suffix + ".bak")
        shutil.copy2(jpath, bak)
        log(f"Backup: {bak}")

    attempted = 0
    for idx in indices:
        if args.limit and attempted >= args.limit:
            break
        attempted += 1
        old = records[idx]
        key = str(old["source_pdf"])
        pdf_path = root / key
        log(f"\n[{attempted}/{len(indices)}] Retrying: {key}")

        if not pdf_path.is_file():
            err = f"PDF not found under root: {pdf_path}"
            log(f"  [err] {err}")
            records[idx] = {"source_pdf": key, "error": err}
            save_records(jpath, records)
            continue

        try:
            text, stats = extract_pdf_text(pdf_path)
        except Exception as e:
            records[idx] = {"source_pdf": key, "error": f"pdf_extract_failed: {e}"}
            log(f"  [err] PDF read: {e}")
            save_records(jpath, records)
            time.sleep(args.sleep)
            continue

        log(
            f"  text: {stats['char_count']:,} chars, "
            f"{stats['pages_with_text']}/{stats['total_pages']} pages"
        )

        try:
            extracted, meta = openrouter_extract(
                api_key,
                args.model,
                text,
                key,
                read_timeout_s=read_timeout,
            )
        except Exception as e:
            records[idx] = {"source_pdf": key, "error": str(e)}
            log(f"  [err] API/parse: {e}")
            save_records(jpath, records)
            time.sleep(args.sleep)
            continue

        records[idx] = {"source_pdf": key, **extracted}
        usage = (meta.get("usage") or {})
        log(
            f"  [ok] {meta.get('elapsed_s')}s, tokens {usage.get('prompt_tokens')}/"
            f"{usage.get('completion_tokens')}"
        )
        save_records(jpath, records)
        time.sleep(args.sleep)

    log(f"\nUpdated {jpath} ({attempted} attempt(s) this run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
