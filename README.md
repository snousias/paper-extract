# paper-extract

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Batch-extract structured metadata from a folder of academic PDFs using an LLM via [OpenRouter](https://openrouter.ai/). Each PDF is parsed locally, then sent to the model, which returns a typed JSON record (title, abstract, DOI, taxonomy, motivation, contribution, research gap, methods, results, and discussion points).

---

## Project layout

Paths below are relative to the folder that **contains** `.librarian` (your “repo root” or project folder).

| Path | Role |
|--------|------|
| `.librarian/data/` | **Default input** — place `.pdf` files here. An empty `data` folder is kept in git via `data/.gitkeep`. |
| `.librarian/output/publications_extractions.json` | **Default output** — one JSON array; updated after every paper. |
| `.librarian/.env` | **Credentials** — create from `.env.example`; not committed. |
| `.librarian/extract_publications.py` | Main extraction script. |
| `.librarian/extract_failed_cases.py` | Re-run only failed records in the output JSON. |
| `.librarian/run_extract.ps1` / `run_extract.sh` | Optional: same as `python extract_publications.py` with args forwarded. |

If you keep PDFs somewhere else, pass `--dir` / PowerShell `-Dir` to point at that folder. You are not required to use `data/`; it is only the default.

---

## Defaults

| Item | Value |
|------|--------|
| PDF folder | `.librarian/data` (same as the `data` directory next to the scripts) |
| Output file | `.librarian/output/publications_extractions.json` |
| API key | Read from the environment, or from `.librarian/.env` (see [Setup](#setup)) |

---

## What it does

1. Scans the chosen folder for `*.pdf`.
2. Extracts text locally with `pypdf` (up to a character cap per paper).
3. Sends the text to an OpenRouter chat model and requests structured JSON.
4. Appends each result to the output JSON, writing after every paper.
5. Failures are stored with an `source_pdf` + `error` stub; re-run with `extract_failed_cases.py` when you want to retry them.

---

## Setup

1. **Python 3.10+** and an [OpenRouter](https://openrouter.ai/) account with a key.

2. **Install dependencies** (from `.librarian`):

   ```bash
   cd .librarian
   python -m pip install -r requirements.txt
   ```

3. **API key** — copy the example file and set your key:

   ```bash
   cp .env.example .env
   ```

   Edit `.env`:

   ```env
   OPENROUTER_API_KEY=sk-or-your-key-here
   ```

4. The Python tools **auto-load** `.librarian/.env` on startup (default `--env-file`). You do not need to `export` the key for normal runs, as long as that file exists.

5. **Optional: load `.env` in the shell** (e.g. for one-off tools):

   - Bash: `set -a && source .env && set +a`
   - PowerShell:  
     `Get-Content .env | ForEach-Object { $k,$v = $_ -split '=',2; [System.Environment]::SetEnvironmentVariable($k,$v) }`

6. **Put PDFs in the default input folder** (or use `--dir` later):

   ```text
   .librarian/data/your_paper.pdf
   ```

---

## Run extraction

Run the script with **`python`** (from `.librarian`, or pass the full path from the repo root):

```bash
cd .librarian
python extract_publications.py
```

More examples (same defaults as in [Defaults](#defaults)):

```bash
python extract_publications.py --skip-existing
python extract_publications.py --limit 5
python extract_publications.py -q
python extract_publications.py --dir "D:\path\to\other\pdfs"
python extract_publications.py --output ".\output\custom_name.json"
```

From the **parent** of `.librarian`:

```bash
python .librarian/extract_publications.py
```

Optional helpers (identical to `python extract_publications.py …`): `run_extract.ps1` and `run_extract.sh` forward all arguments; PowerShell also accepts `-Dir` as an alias for `--dir`.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--dir PATH` | `.librarian/data` | Folder containing PDFs. |
| `--output PATH` | `.librarian/output/publications_extractions.json` | Output JSON file. |
| `--model MODEL` | `openai/gpt-oss-120b` | OpenRouter model id. |
| `--limit N` | 0 (no limit) | Max PDFs this run. |
| `--skip-existing` | off | Skip PDFs whose `source_pdf` is already in the output. |
| `--sleep SEC` | 1.0 | Pause between API calls. |
| `--timeout SEC` | 2 | HTTP read timeout for each OpenRouter response. |
| `--no-wait-hints` | off | No periodic “still waiting” lines on long calls. |
| `--env-file PATH` | `.librarian/.env` | Env file to load. |
| `-q` / `--quiet` | off | Minimal output. |

---

## Retry failed cases

`extract_failed_cases.py` reads the same JSON as the main extractor. It only retries items that have both `source_pdf` and `error`. Successful rows are left unchanged. By default, PDFs are resolved under **`.librarian/data`** unless you pass `--dir`.

```bash
cd .librarian
python extract_failed_cases.py --dry-run
python extract_failed_cases.py --backup
python extract_failed_cases.py --only "Mesh Denoising"
```

From the repo parent:

```bash
python .librarian/extract_failed_cases.py --dry-run
```

If you see `No matching failed records (nothing to retry).`, there are no failed stubs to process. Override the JSON path with `--json` if needed.

---

## Extracted schema

| Field | Type | Description |
|-------|------|-------------|
| `source_pdf` | string | Filename of the source PDF. |
| `title` | string | Paper title. |
| `abstract` | string | Full abstract. |
| `doi` | string \| null | DOI if found in the text. |
| `taxonomy.primary_domain` | string | Top-level domain. |
| `taxonomy.subdomains` | string[] | Sub-areas. |
| `taxonomy.methods_tags` | string[] | Method tags. |
| `taxonomy.application_areas` | string[] | Application areas. |
| `taxonomy.keywords` | string[] | Keywords. |
| `motivation` | string | Motivation. |
| `contribution` | string | Contribution. |
| `research_gap` | string | Research gap. |
| `method_delineation` | string | How the method works. |
| `results` | string | Results. |
| `discussion_points` | string[] | Discussion bullets. |

---

## Output format

The file is a JSON array. Each element is either a full record or a failure stub:

```json
[
  {
    "source_pdf": "example_paper.pdf",
    "title": "...",
    "abstract": "...",
    "doi": "10.1000/xyz123",
    "taxonomy": { },
    "motivation": "...",
    "contribution": "...",
    "research_gap": "...",
    "method_delineation": "...",
    "results": "...",
    "discussion_points": ["...", "..."]
  },
  {
    "source_pdf": "broken_scan.pdf",
    "error": "very little text extracted (43 chars)"
  }
]
```

The file is written after every paper, so it stays valid JSON if you interrupt the run.

---

## Dependencies

```
pypdf>=5.0.0
requests>=2.31.0
tqdm>=4.66.0
json-repair>=0.30.0
```

`json-repair` helps when the model returns slightly malformed JSON.
