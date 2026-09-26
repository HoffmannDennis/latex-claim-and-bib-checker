# latex-claim-and-bib-checker

Checks the references of a LaTeX document in one run:

1. **Citation consistency:** every `\cite`-style key must have a bibliography entry, and every entry should be cited.
2. **Metadata:** the title, authors, year, venue, volume, issue and pages of each BibTeX entry are compared with the record that Crossref, OpenAlex or DataCite holds for it.
3. **Claims:** for every citing sentence the tool looks up the abstract of the cited work and asks a language model whether the abstract supports the sentence. By default the model runs on your own machine (LM Studio or another local server).

All three results go into one report, as Markdown or as an Excel workbook.

## 1. Purpose and limits

The tool is a **verification aid**. It helps you find typos, wrong DOIs, outdated years, missing entries and citations worth a second look before a manuscript goes out. The decision stays with you.

- **Abstract-only claims.** Verdicts rest on the abstract of the cited work. A claim can be correct although the abstract does not mention it (it may be in the full text), so `UNVERIFIABLE` and `PLAUSIBLE` are expected for many correct citations. The report header states once that verdicts rest on abstracts only.
- **Verdicts:** `SUPPORTED` (a quoted passage of the abstract supports the sentence), `PLAUSIBLE` (consistent, but not stated in the abstract), `SUSPICIOUS` (the abstract contradicts or does not support the sentence), `UNVERIFIABLE` (not enough information, or no abstract found).
- **Quote check.** `SUPPORTED` requires a passage copied from the abstract. The tool checks that the passage really occurs in the abstract (ignoring case, typographic quotes and dashes) and downgrades the verdict to `UNVERIFIABLE` if it does not.
- **No abstract, no model call.** If no usable abstract is found, the claim is `UNVERIFIABLE` with evidence type `none` and nothing about it is sent to a model.
- **Right work only.** An abstract is used only if the title of its record matches the title of the bibliography entry as in the metadata check (formatting differences and a subtitle on one side are tolerated), otherwise the next source is tried. A title-search hit must also pass the metadata check without a substantive difference (for example in authors or year) and must not carry a DOI other than the entry's.
- **Not found is not missing.** `NOT_FOUND_IN_ENABLED_SOURCES` means that the sources have no matching record. Books, reports, software and working papers are often missing from them, and many publishers deposit no abstracts.
- **Tolerant matching.** Diacritics, LaTeX markup, subtitles and a year that is off by one are tolerated. Remaining differences are graded `MISMATCH_MINOR` or `MISMATCH_MAJOR`, and both deserve a look. `MATCH` requires at least one compared field.
- **Embedded bibliographies** (`\begin{thebibliography}`) are checked for consistency only. Metadata and claims need a `.bib` file. Without one, the claim check is skipped with a note in the report.
- `.tex` and `.bib` files are read as UTF-8 and never modified.

## 2. Quickstart

Requirements: Python 3.11 or newer and `git`. [uv](https://docs.astral.sh/uv/) is recommended but optional.

```bash
git clone https://github.com/HoffmannDennis/latex-claim-and-bib-checker.git
cd latex-claim-and-bib-checker

# with uv
uv sync

# or with pip in a virtual environment
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

Prefix the commands below with `uv run` when you use uv.

**Step 1: bibliography only, no language model.**

```bash
latex-claim-and-bib-checker examples/paper.tex --bib-check-only
```

**Step 2: claims with a local model.** Install [LM Studio](https://lmstudio.ai/), download an instruction-tuned model, load it and start the local server (Developer tab, default address `http://127.0.0.1:1234/v1`). Then:

```bash
latex-claim-and-bib-checker examples/paper.tex -o report.md
```

With just-in-time model loading switched on (the LM Studio default), the server lists every downloaded model, not only the loaded one. If you have downloaded more than one model, add `--model NAME` with the name of the model you loaded.

The example document has three deliberate problems: the year of one entry is off by one, one entry is never cited, and one sentence cites a paper for something the paper does not say.

More commands:

```bash
latex-claim-and-bib-checker paper.tex --claim-check-only              # claims only
latex-claim-and-bib-checker references.bib                            # metadata of a single .bib file
latex-claim-and-bib-checker paper.tex -o report.xlsx                  # Excel workbook
latex-claim-and-bib-checker paper.tex --abstract-db abstracts.sqlite  # reuse found abstracts
latex-claim-and-bib-checker paper.tex --cloud gemini --model gemini-2.5-flash
```

`python -m latex_claim_and_bib_checker` works as well. Run with `--help` for all options, environment variables, statuses and exit codes.

| Option | Meaning |
|---|---|
| `input` | the LaTeX file (`\input`/`\include` are followed, `\bibliography{a,b}` and `\addbibresource{...}` are recognised) or a `.bib` file |
| `--bib-check-only` | only citation consistency and metadata, no model |
| `--claim-check-only` | only the claims |
| `--cloud openai\|gemini\|anthropic` | judge the claims with a cloud provider instead of the local server. Needs `--model` and the provider's key. |
| `--model NAME` | model name. Required with `--cloud`. Locally only needed if the server offers several models. |
| `--abstract-db FILE` | read abstracts and metadata from a local SQLite file and add newly found ones |
| `-o, --output FILE` | report file (`.md` or `.xlsx`, an existing file is replaced). Default: Markdown on standard output. |

Exit codes (if several apply, the first in the order 4, 3, 1, 0):

| Code | Meaning |
|---|---|
| 0 | report produced, nothing that needs attention |
| 1 | report produced with findings: a cited key without a bibliography entry, an entry with `MISMATCH_MAJOR` or `NOT_FOUND_IN_ENABLED_SOURCES`, or a `SUSPICIOUS` claim |
| 2 | usage, input or setup error (for example a missing file, or no local model server). Nothing was checked. |
| 3 | report produced, but in a check that ran no source answered any lookup (network down or rate limited) |
| 4 | report produced, but the model provider failed during the run (claims whose model request failed are marked `UNVERIFIABLE`) |

## 3. Setup prompt for an AI coding assistant

Copy the prompt below into your coding assistant. **Never paste API keys or other secrets into a chat.** If you want to use a key, set it yourself as described in [API keys](#api-keys).

```text
Please install and run the command-line tool "latex-claim-and-bib-checker" for me.

1. In a folder of my choice, clone
   https://github.com/HoffmannDennis/latex-claim-and-bib-checker
   and change into it. Python 3.11+ is required.
2. Install it: if `uv` is available, run `uv sync`. Otherwise create a virtual environment
   (`python3 -m venv .venv`), activate it and run `pip install .`
3. Check the installation without a language model:
   `latex-claim-and-bib-checker examples/paper.tex --bib-check-only`
   (prefix with `uv run` when using uv). It prints a Markdown report.
4. API keys are optional. Show me the table in the "API keys" section of the README and
   tell me which keys would help for my document and how I set them myself. Never ask
   me for a key, and never print a key or write it into a file. You only see keys that
   were set before you were started. If I set one now, I restart you or run the
   commands myself.
5. Ask me for the path to my .tex file and run the bibliography check first:
   `latex-claim-and-bib-checker <my.tex> --bib-check-only -o bib-report.md`
6. Ask me whether LM Studio (or another local model server) is running with a model loaded.
   If yes, run `latex-claim-and-bib-checker <my.tex> -o report.md`. If the tool reports
   that no server answers, tell me how to start the LM Studio local server. Do not switch
   to a cloud provider on your own.
7. Summarise the report: cited keys without an entry and MISMATCH_MAJOR entries first,
   then SUSPICIOUS claims, then UNVERIFIABLE ones, with the cited key and the rationale.
   If the report header lists sources not used because a key is missing, name those keys.
   Do not change my .tex or .bib files unless I ask you to.
```

## 4. Sources and providers

The metadata check uses Crossref, OpenAlex and DataCite in this order and stops at the first source with comparable fields. The claim check uses every available abstract source, in the order of the table below. Any single one is enough.

| Name | Order | Environment variable | Coverage | Looked up by | Metadata check |
|---|---|---|---|---|---|
| `arxiv` ([arXiv API](https://info.arxiv.org/help/api/index.html)) | 1 | none | arXiv preprints | arXiv identifier or `10.48550/arXiv.*` DOI | no |
| `crossref` ([Crossref](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)) | 2 | none (optional `CONTACT_EMAIL`) | Crossref DOIs, abstracts only where the publisher deposits them | DOI | yes |
| `openalex` ([OpenAlex](https://docs.openalex.org/)) | 3 | `OPENALEX_API_KEY` (recommended) | broad index of scholarly works | DOI, title search | yes |
| `datacite` ([DataCite](https://support.datacite.org/docs/api)) | 4 | none | DataCite DOIs: arXiv, Zenodo, datasets, software | DOI or arXiv identifier | yes |
| `springer` ([Springer Nature API](https://dev.springernature.com/)) | 5 | `SPRINGER_API_KEY` (the source is off without it) | Springer Nature journals and books | DOI | no |
| `s2` ([Semantic Scholar](https://www.semanticscholar.org/product/api)) | 6 | optional `S2_API_KEY` | broad, cross-disciplinary | DOI or arXiv identifier | no |
| `core` ([CORE](https://core.ac.uk/services/api)) | 7 | optional `CORE_API_KEY` (recommended, the allowance without a key is very small) | open-access repositories | DOI | no |
| `europepmc` ([Europe PMC](https://europepmc.org/RestfulWebService)) | 8 | none | life sciences and biomedicine | DOI | no |

Order: arXiv comes first because it holds the authors' own abstract and answers only for arXiv papers, so other entries cost no request. Crossref and OpenAlex follow: in a default run the metadata check has usually fetched their records already, so their abstracts need no extra request. DataCite covers Zenodo, datasets and software. Springer Nature needs a key. Semantic Scholar, CORE and Europe PMC come last as broad fallbacks. CORE allows few requests without a key and Europe PMC covers only the life sciences.

The report header names the sources that were used and the ones that lacked a key. Both checks share one connection per source: a work that the metadata check already requested is not requested again for its abstract.

The OpenAlex title search runs only for entries without a DOI, or when every DOI lookup came back empty.

Model providers (exactly one per run, there is no automatic switch between them):

| Mode | Provider | Endpoint | Environment variable |
|---|---|---|---|
| local (default) | LM Studio | `http://127.0.0.1:1234/v1` | none |
| local | Ollama, llama.cpp server, vLLM or another OpenAI-compatible server | `LOCAL_LLM_URL` | none |
| `--cloud openai` | OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `--cloud gemini` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` |
| `--cloud anthropic` | Anthropic, native Messages API | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |

**Acknowledgements:** Thank you to arXiv for use of its open access interoperability.

### API keys

The default run needs no key. Keys raise request allowances, switch on the Springer Nature source or enable a cloud provider.

| Variable | Effect | Where to get it |
|---|---|---|
| `OPENALEX_API_KEY` | recommended: OpenAlex allows only occasional use without a key | free account, [openalex.org/settings/api](https://openalex.org/settings/api) |
| `CORE_API_KEY` | recommended: the allowance without a key is very small | registration by e-mail, [core.ac.uk/services/api](https://core.ac.uk/services/api) |
| `S2_API_KEY` | optional: an own request allowance instead of the pool shared by all users without a key | request form, [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api) |
| `SPRINGER_API_KEY` | optional: switches the Springer Nature source on | account, API Management at [dev.springernature.com](https://dev.springernature.com/) |
| `OPENAI_API_KEY` | only with `--cloud openai` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `GEMINI_API_KEY` | only with `--cloud gemini` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `ANTHROPIC_API_KEY` | only with `--cloud anthropic` | [platform.claude.com/settings/keys](https://platform.claude.com/settings/keys) |

The tool reads keys only from environment variables, never from a file, and removes them from every message it prints or writes. In local mode it reads no cloud key, with `--cloud` only the key of the chosen provider. `CONTACT_EMAIL` and `LOCAL_LLM_URL` are set the same way.

Set a key in the terminal from which you run the tool:

```bash
# macOS and Linux, this terminal only. The key is typed without echo and stays out of the shell history.
read -rs OPENALEX_API_KEY && export OPENALEX_API_KEY

# Permanently: add the line  export OPENALEX_API_KEY=<your key>  to ~/.bashrc or ~/.zshrc, then open a new terminal.
```

```powershell
# Windows PowerShell, this window only. The input stays out of the command history.
$env:OPENALEX_API_KEY = Read-Host "OPENALEX_API_KEY"
```

A coding assistant only sees keys that were set before it was started. After setting a key, restart the assistant or run the command yourself.

## 5. Data flows

- **Sources:** for each bibliography entry, its DOI and arXiv identifier are sent to the sources, and for the OpenAlex title search its title and year. No text of your document is sent to any source. Requests carry the generic user agent `latex-claim-and-bib-checker/<version>`. A contact address is added only if `CONTACT_EMAIL` is set. Keys are removed from every message the tool prints or writes.
- **Local mode (default):** citing sentences, the preceding sentence and the abstracts go only to the model server named by `LOCAL_LLM_URL` (default LM Studio on `127.0.0.1`). Loopback and private network addresses are accepted, public addresses are refused. Proxy settings are ignored and redirects are not followed. Local mode reads no cloud key. If the local server does not answer, the run stops before any source is asked and never switches to a cloud provider.
- **Cloud mode:** with `--cloud` the same content goes to the chosen provider. Claims without an abstract are never sent.
- **Optional abstract database:** with `--abstract-db FILE` the tool reads abstracts and metadata from a local SQLite file and adds newly found ones, which is off by default.
- **No usage traces:** no cache, log, history or configuration file is written. The only report file is the one you request with `-o`. Reports name the input by file name only. Progress and errors go to standard error.

### Prompt caching

Every request consists of a fixed instruction part, a block with the cited work (key, title, abstract) and, last, the claim. The first two parts are byte-identical for all claims citing the same work, and claims are judged grouped by cited work, one after another, at temperature 0. The tool guarantees only this identical prefix and order. Whether the server actually reuses its cached prompt prefix depends on the server and the model. Cloud providers with automatic prefix caching benefit in the same way.

### WSL

If the tool runs in WSL and LM Studio on Windows, either enable mirrored networking (`networkingMode=mirrored` in `%UserProfile%\.wslconfig`), so that `127.0.0.1:1234` reaches LM Studio, or let LM Studio listen on the network and set `LOCAL_LLM_URL=http://<Windows host address>:1234/v1`.

## 6. Tested models

Measured on 2026-09-25 with the example document. The run checks that the setup works. It makes no claim about verdict accuracy.

| Mode | Provider | Model | Claims | Result |
|---|---|---|---|---|
| local | LM Studio | `qwen/qwen3.8-27b` | 10 | 10 model requests, every response valid, no repair request |

## 7. Example output

[`examples/example-report.md`](examples/example-report.md) is the report of a default run on the example document with all built-in sources, no key and the model above:

```bash
latex-claim-and-bib-checker examples/paper.tex --model qwen/qwen3.8-27b -o examples/example-report.md
```

It shows the three deliberate problems (`breiman2001random` as `MISMATCH_MINOR`, `unused2020example` as defined but uncited, the second `he2016resnet` sentence as `SUSPICIOUS`). Verdicts can differ between models and runs.

A report starts with a header (tool version, date, input file name, the checks that ran, model and provider, the sources used and the ones not used because a key is missing), followed by a summary, `## References` (citation consistency, one row per entry with status, origin `API` or `DB` and the compared record) and `## Claims` (grouped by verdict, with the evidence source, its origin and how it was found). The Excel workbook has the sheets Summary, References and Claims with the same content.

## 8. License

Source-available, non-commercial: this project is released under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0), see [`LICENSE`](LICENSE). Users are responsible for complying with the terms of the services and data they use.
