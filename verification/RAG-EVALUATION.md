# RAG evaluation contract

RAG is evaluated as three isolated products, not as one undifferentiated search index:

| Corpus role | Product use | Formal failure |
| --- | --- | --- |
| `literature_evidence` | research evidence and source inspection | missing expected evidence, wrong source, or style/help leakage |
| `stata_help` | command discovery and error diagnosis | missing command/help markers or leakage into research evidence |
| `style_exemplar` | section-specific few-shot writing style | use as factual evidence or retrieval from an unselected paper |

The deterministic suite has four layers:

1. **Parser quality** — every PDF first receives a complete page-addressable pypdf FAST parse.
   Page diagnostics may then escalate only structurally difficult pages to local MinerU. Evaluation
   checks semantic node-kind recall, required-content recovery, page-locator coverage, the exact
   escalation plan, and that all non-escalated pages remain byte-for-byte unchanged.
2. **Single-hop retrieval** — Recall@K, Precision@K, MRR, source diversity, evidence-group
   coverage, and zero corpus-role leakage.
3. **Multi-hop trace** — bounded hop count, new canonical nodes after the first hop, evidence-group
   coverage across all hops, explicit terminal reason, and zero role leakage. The Agent is free to
   choose subquestions; the evaluator grades the persisted public trace rather than hard-coding a
   research workflow.
4. **Grounded output** — claim citation coverage, exact citation-node validity, required support
   markers in cited nodes, evidence-role leakage, and long contiguous copying from selected style
   exemplars. This is a deterministic safety gate; semantic quality still needs held-out human or
   model-assisted evaluation.
5. **Runtime boundaries** — retrieved nodes retain Source Revision, Parse Revision, Node, Session,
   Hop, rank, and score identities when inserted into model context. Style and Stata Help cannot be
   silently promoted to research evidence.

The checked-in example gold set is a schema example, not a claim that two examples constitute a
release-quality benchmark. A release candidate must replace or extend it with versioned cases from
real projects while preserving a holdout set that is not used for query tuning.

## Run the retrieval gate

Run against a Workspace whose canonical indexes have already been synchronized:

```powershell
uv run python tools/run_rag_evaluation.py `
  --workspace-root "C:\path\to\workspace" `
  --workspace-id "ws_example" `
  --gold verification/rag-gold.example.json `
  --output verification/runs/rag-evaluation.json
```

The command exits `0` only when all configured thresholds pass and corpus-role leakage is zero. It
exits `2` for a quality regression. Thresholds are policy inputs, not architectural constants.

## Automated evidence

```powershell
uv run pytest tests/unit/test_knowledge_parsing.py -q
uv run pytest tests/unit/test_rag_evaluation.py -q
uv run pytest tests/integration/test_canonical_knowledge_runtime.py -q
uv run pytest tests/integration/test_rag_evaluation_baseline.py -q
```

The integration baseline builds real canonical Literature, Stata Help, and Style indexes, creates a
versioned dense index through the embedding contract, proves a dense-only result can enter RRF,
checks all three corpus roles, and evaluates an actual two-hop Retrieval Session.

## Real-paper baseline

`verification/corpora/did-methods-v1` is the first fixed external corpus. It contains ten public
papers (535 pages) covering modern difference-in-differences and event-study methods. The corpus
manifest freezes source URLs, local filenames, page counts, byte sizes, SHA-256 identities, and
benchmark roles. Nine papers enter `literature_evidence`; the explicitly selected practitioner
guide enters `style_exemplar`.

Build the corpus and run the real benchmark with:

```powershell
.\.venv\Scripts\python.exe tools\build_did_paper_corpus.py `
  --output-root verification\corpora\did-methods-v1

.\.venv\Scripts\python.exe tools\run_real_rag_benchmark.py `
  --corpus-root verification\corpora\did-methods-v1 `
  --output-root verification\runs\rag-real-did-20260920-v2
```

The frozen V2 observation used 10 papers plus 71 query-targeted Stata Help sources. Across 12
retrieval cases, the current FTS5 backend measured Recall@8 `1.000`, Precision@8 `0.740`, MRR
`0.875`, evidence-group coverage `1.000`, and zero corpus-role leaks. The two-hop method-comparison
trace passed. This is a transparent in-sample baseline, not a held-out release claim.

## Adaptive PDF parsing baseline

MinerU is a precision escalation path, not the default whole-document parser. The production path
is:

```text
all pages → pypdf FAST parse → page diagnostics
                               ├── ordinary page → retain FAST text
                               └── difficult page → local MinerU basic → replace same page locator
```

The diagnostic policy currently considers missing/scanned text, corrupt or undecoded glyphs,
formula-dense layout, table-dense layout, low text density, and cross-page continuation. A
versioned policy caps ordinary enrichment by page count and document ratio; hard extraction
failures remain eligible even when the cap is reached. OCR is therefore limited to pages that
actually need image text recognition. Formula, table, and cross-page cases use MinerU for layout
reconstruction rather than being mislabeled as OCR.

Run the real local evaluation with:

```powershell
$runtime = ".e2e-runtime\rag-real"
$env:MINERU_HOME = Join-Path $runtime "mineru-home"
$env:PYTHONPATH = "$PWD\src"
& "$runtime\mineru-venv\Scripts\python.exe" tools\run_real_mineru_evaluation.py `
  --mineru-executable "$runtime\mineru-venv\Scripts\mineru.exe" `
  --corpus-root verification\corpora\did-methods-v1 `
  --output-root verification\runs\rag-adaptive-pdf-20260920-v2
```

The frozen V2 observation processed 174 pages from four real papers. It retained pypdf output for
146 pages and sent 28 pages (`16.1%`) to local MinerU basic. All page locators were complete, all
non-escalated pages were unchanged, required markers were retained, and no remote parser was used.
On the previously unseen 51-page Sun–Abraham paper, the actual MinerU request was only
`2,5-8,37-39`; the eight enriched pages added three display equations, four headings, and forty
Markdown table rows that were absent from their FAST page text. MinerU internally also created its
own flash bootstrap parse for pages 1–10; this is an implementation cost of MinerU 4 and is reported
rather than hidden. See
`verification/runs/rag-adaptive-pdf-20260920-v2/adaptive-pdf-report.json` for the complete record.

## Release evidence still required

The following are intentionally not disguised as complete merely because the deterministic suite
passes:

- installer/SBOM packaging for the pinned multilingual local embedding model;
- a larger held-out PDF parser gold set with human-labeled table, equation, scan, and cross-page
  failures beyond the current real-paper structural baseline;
- a web-capture adapter and online-search provenance tests;
- model-output grounding and citation correctness on held-out research tasks;
- style-transfer preference tests and long-span copying/factual-contamination checks;
- latency, peak-memory, and 1 GiB incremental-index stress evidence.

These are release gates. The current contracts make them replaceable components, while preserving
the canonical source, retrieval-session, context-use, and evaluation identities already stored in
SQLite.
