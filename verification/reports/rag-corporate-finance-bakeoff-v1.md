# Corporate Finance RAG Bake-off v1

**Run date:** 2026-09-25  
**Host:** Windows 11, Intel i7-14650HX, 16 GB RAM, RTX 4060 Laptop GPU (8 GB VRAM)  
**Artifact root:** `D:\stata-agent-rag-lab`  
**Status:** completed; default-profile decision supported

## 1. Evaluation corpus

- 30 public NBER corporate-finance working papers;
- 1,511 PDF pages and 19,987,259 source bytes;
- 10,124 canonical knowledge nodes after ingestion;
- topics cover financing constraints, capital/debt structure, liquidity, payout,
  governance, executive incentives, investment, bank relationships, and mergers;
- every PDF has a source URL, SHA-256, byte size, page count, and validation record in
  `D:\stata-agent-rag-lab\corpora\corporate-finance-v1\manifest.json`.

The frozen question set contains 41 cases: 30 single-hop, 6 multi-hop, and 5
no-answer cases. Retrieval metrics below are computed on the 36 positive cases and
47 actual retrieval hops. The present deterministic evaluator records the five
negative cases but does **not** yet claim no-answer-generation accuracy.

## 2. Parser bake-off

The original pypdf-only corpus missed four papers: two had no usable text layer and
two exposed substantial but ciphered/gibberish text. On these four deliberately hard
PDFs, pypdf recovered none of the required semantic markers.

| Paper | pypdf marker recall | selective MinerU marker recall | MinerU selected pages | observed extraction time |
| --- | ---: | ---: | ---: | ---: |
| Kaplan & Zingales (1995) | 0.000 | 1.000 | 42 / 48 | cached: 1.41 s |
| Opler et al. (1997) | 0.000 | 1.000 | 54 / 54 | cached: 1.42 s |
| Ikenberry et al. (1994) | 0.000 | 0.667 | 28 / 33 | fresh: 90.70 s |
| Zingales (1997) | 0.000 | 1.000 | 23 / 23 | fresh: 66.61 s |

Docling was not suitable as the default on this host. It preserved the ciphered text
of the Kaplan-Zingales PDF instead of recovering its meaning, and the Opler PDF hit an
ONNX Runtime `bad_alloc` during layout processing on the 16 GB machine. Those failed
attempts are retained under the D-drive run root rather than hidden.

The first full-corpus MinerU policy was also rejected: ordinary equations/tables
triggered too many expensive pages. The adopted parser policy is therefore:

```text
pypdf fast path
-> page/text diagnostics
-> MinerU only for scan/no-text/ciphered-text critical failures
-> preserve pypdf as fallback
```

This parser recovery changed end-to-end retrieval more than changing the embedding
model: positive-case Hit@8 and source recall rose from 0.889 to 1.000, while lexical
MRR rose from 0.870 to 0.933.

## 3. Retrieval bake-off on the same recovered corpus

All variants use the same source, parse, node, question, candidate-depth, and top-k
inputs. Query latency excludes one-time model loading and index construction.

| Profile | Hit@8 | source recall | marker recall | MRR | multi-hop coverage | mean query | P95 query | index build |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FTS5 / deterministic lexical | 1.000 | 1.000 | 1.000 | 0.933 | 1.000 | 25.0 ms | 50.7 ms | none |
| Hybrid + multilingual-E5-small | 1.000 | 1.000 | 0.986 | **0.962** | 0.917 | 297.2 ms | 444.7 ms | 49.1 s |
| Hybrid + BGE-M3 dense | 1.000 | 1.000 | 1.000 | 0.954 | **1.000** | 506.6 ms | 735.3 ms | 256.6 s |
| Hybrid + Qwen3-Embedding-0.6B | 1.000 | 1.000 | 1.000 | **0.986** | **1.000** | 665.3 ms | 1,323.5 ms | 150.7 s |

The E5 index uses 384-dimensional vectors and a pinned model revision. The BGE-M3
index uses 1,024-dimensional vectors, a pinned revision, CUDA batch size 4, and
`trust_remote_code`. BGE-M3 took 5.22 times as long to build, was 1.70 times slower
per query on average, and did not beat E5 on MRR. Its multi-hop coverage was better,
but the quality gain over the lexical coverage guard was not large enough to justify
it as the Windows default.

Qwen3-Embedding-0.6B produced the strongest retrieval quality. Its research-specific
query instruction, 512-token experiment profile, 1,024-dimensional vectors, and exact
model revision are part of the index identity. Compared with E5-small it improved MRR
from 0.962 to 0.986 and restored full multi-hop coverage, but took 3.07 times as long
to build, was 2.24 times slower per query, and used about 7.4 / 8.2 GB GPU memory while
building the index. It is therefore a supported GPU quality profile rather than the
default lightweight profile.

### Additional embedding candidates reviewed

| Candidate | Outcome | Reason |
| --- | --- | --- |
| `Qwen/Qwen3-Embedding-0.6B` | adopt as optional quality profile | best observed MRR and full multi-hop coverage; materially higher latency/VRAM |
| `Alibaba-NLP/gte-multilingual-base` | reject in current runtime | pinned model still imports a second remote-code repository; CPU and CUDA both failed with RoPE position-index errors |
| `Snowflake/snowflake-arctic-embed-m-v2.0` | hold | promising 305M multilingual model, but the pinned profile requires `xformers` and did not load in the supported runtime |
| `nomic-ai/nomic-embed-text-v2-moe` | hold | Apache-2.0 and attractive 305M-active MoE/Matryoshka design, but Windows custom-code/MoE dependencies add deployment risk and the model is limited to 512 tokens |
| `google/embeddinggemma-300m` | hold | compact on-device candidate, but Gemma terms and gated asset flow need a separate distribution review |
| `intfloat/multilingual-e5-large(-instruct)` | lower-priority benchmark | standard and reproducible, but about 0.6B parameters; Qwen3 already covers the quality-oriented 0.6B slot more strongly in this corpus |
| `jinaai/jina-embeddings-v3` | exclude from product default | CC-BY-NC-4.0 model weights are unsuitable for an unrestricted product default |

The 512-token setting is an embedding-profile input limit for each canonical node,
not an Agent context limit and not a whole-paper limit. Papers remain split into
traceable structural nodes. Longer Qwen3 profiles can be evaluated separately, but
cannot be compared directly with the 512-token E5 baseline without labeling the
changed input contract.

## 4. Neural reranker bake-off

All successful reranker variants used E5-small hybrid retrieval as the unchanged
first stage and reranked only the top 32 fused candidates at a maximum of 512 tokens.

| Second stage | MRR | marker recall | multi-hop coverage | mean query | P95 query | decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| deterministic evidence reranker | **0.962** | 0.986 | 0.917 | **297 ms** | **445 ms** | default |
| BGE-reranker-v2-m3 | 0.949 | 1.000 | 1.000 | 2,137 ms | 2,539 ms | reject as default |
| Qwen3-Reranker-0.6B | 0.918 | 1.000 | 1.000 | 2,043 ms | 2,501 ms | reject as default |
| GTE multilingual reranker | not completed | - | - | - | - | runtime-incompatible |

BGE restored the missed marker/multi-hop coverage but reduced first-hit ranking and
made a query 7.19 times slower on average. Qwen3 was run through its official causal
yes/no scoring contract after the generic CrossEncoder loader incorrectly initialized
a missing classification head; its ranking quality was lower still. GTE failed on
both CPU and CUDA with a pinned-model/remote-code position-encoding error.

The evidence does not support a neural reranker in the always-on default path. The
deterministic reranker and coverage guard remain default. A neural reranker may be
offered later as an explicit high-latency checkpoint for difficult queries, but only
after a larger judged relevance set shows a repeatable benefit.

### Fusion and scheduling follow-up

Replacement-style reranking discarded useful lexical/dense priors. A follow-up run
therefore fused neural rank, deterministic relevance and first-stage rank, then added
a cheap ambiguity scheduler and a separate evidence-sufficiency gate.

| Profile | MRR | marker recall | multi-hop coverage | no-answer accuracy | false abstention |
| --- | ---: | ---: | ---: | ---: | ---: |
| E5 deterministic baseline | 0.962 | 0.986 | 0.917 | - | - |
| BGE v2-m3 always-on fusion | 0.976 | 1.000 | 1.000 | - | - |
| BGE v2-m3 adaptive fusion | 0.976 | 1.000 | 1.000 | 1.000 | 0.000 |
| MiniLM adaptive fusion | 0.959 | 1.000 | 1.000 | 1.000 | 0.000 |
| **BGE base adaptive fusion** | **0.977** | **1.000** | **1.000** | **1.000** | **0.000** |

The adopted BGE-base policy changed the top result in 10 cases. Three cases improved
MRR or missing-evidence coverage and none regressed on those case metrics. In a paired,
alternating three-repeat run (141 measurements per variant), the deterministic path
averaged 0.541 seconds and adaptive fusion averaged 0.876 seconds. Neural reranking
triggered on 51.4% of queries, adding 0.335 seconds on average and 0.794 seconds at P95.

CPU BGE-base preserved quality but averaged 6.04 seconds/query, so the neural profile
is GPU-only. On CPU, absent model packs, or inference failure, the same query uses the
deterministic fallback. The evidence-sufficiency gate rejected all five negative cases
and admitted all 36 positive cases; an insufficient result exposes the fixed response
`未在当前知识库中找到足够证据，无法回答。` rather than asking the model to improvise.

## 5. Decision

Adopt the following V0.1 default profile:

```text
pypdf fast parse
+ critical-only MinerU recovery
+ SQLite FTS5 exact recall
+ multilingual-E5-small dense recall
+ deterministic CPU fallback / adaptive BGE-base GPU fusion
+ evidence-sufficiency / coverage guard
+ versioned retrieval trace and evidence packet
```

Keep these alternatives:

- lexical-only is the explicit offline/degraded profile;
- Qwen3-Embedding-0.6B is the optional GPU quality profile;
- BGE-M3 remains an experimental comparison profile, not a default download;
- replacement-style and always-on neural reranking remain disabled;
- adaptive BGE-base fusion is the optional GPU balanced profile because it improved
  MRR and coverage with a paired mean cost of 0.335 seconds;
- Docling remains an experiment adapter, not an installed default;
- full-page/full-document OCR is not performed unless diagnostics justify it.

The current result freezes the no-neural-reranker default for V0.1, but keeps the
reranker port replaceable. The vector-storage backend remains outside this bake-off,
as requested; the present Python flat-cosine implementation remains the correctness
baseline.

## 6. Product/code findings closed during the experiment

The bake-off found and fixed production issues rather than only producing scores:

1. SentenceTransformers changed its embedding-dimension API; the adapter now supports
   the current API and the old test-double contract.
2. Dense retrieval could return more candidates than the repository read cap; node
   hydration now occurs in bounded batches.
3. The old parser diagnostics missed ciphered/gibberish PDF text; a conservative
   low-English-token/high-abnormal-case detector now escalates those pages.
4. MinerU's default output limit silently truncated long papers; the adapter now sets
   an explicit 4,000,000-character limit.
5. Embedding profiles now carry query/document prefixes, profile family, pinned model
   revision, device/batch configuration, and `trust_remote_code` policy instead of
   assuming every model follows E5 formatting.
6. Maximum embedding input length is now a versioned profile property; long-context
   models cannot silently change resource use or the indexed content boundary.
7. fp16 model outputs are normalized again after serialization so every backend
   satisfies the same strict L2 vector contract.
8. A top-N CrossEncoder adapter and a Qwen3 causal yes/no scorer now exist behind the
   same product reranker port; failed attempts do not become adopted policy.

## 7. Reproduction artifacts

- Corpus source specification: `verification/corpora/corporate-finance-v1.sources.json`
- Frozen question set: `verification/rag-corporate-finance-questions.v1.json`
- Experiment specification: `verification/experiments/rag-corporate-finance-bakeoff-v1.json`
- Parser challenge set: `verification/corporate-finance-parser-challenge.v1.json`
- Corpus builder: `tools/build_paper_corpus.py`
- Dense-index builder: `tools/build_workspace_dense_index.py`
- Scenario evaluator: `tools/run_real_rag_scenario_evaluation.py`
- Parser evaluator: `tools/run_corporate_finance_parser_bakeoff.py`

Raw reports are immutable directories under:

```text
D:\stata-agent-rag-lab\runs\corp-fin-parser-mineru-v1-attempt4
D:\stata-agent-rag-lab\runs\corp-fin-scenario-mineru-critical-lexical-v1-latency
D:\stata-agent-rag-lab\runs\corp-fin-scenario-mineru-critical-e5-v1-latency
D:\stata-agent-rag-lab\runs\corp-fin-scenario-mineru-critical-bge-m3-v1-latency
D:\stata-agent-rag-lab\runs\corp-fin-mineru-critical-e5-v1
D:\stata-agent-rag-lab\runs\corp-fin-mineru-critical-bge-m3-v1
D:\stata-agent-rag-lab\runs\corp-fin-mineru-critical-qwen3-embedding-0.6b-v1-attempt3
D:\stata-agent-rag-lab\runs\corp-fin-scenario-mineru-critical-qwen3-embedding-0.6b-v1
D:\stata-agent-rag-lab\runs\corp-fin-scenario-e5-bge-reranker-v2-m3-v1
D:\stata-agent-rag-lab\runs\corp-fin-scenario-e5-qwen3-reranker-0.6b-v1-attempt2
```

Failed and interrupted attempts are intentionally retained beside successful runs so
that parser/resource failures and policy changes remain auditable.
