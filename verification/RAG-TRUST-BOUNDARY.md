# RAG Trust Boundary

Version: `rag-trust-boundary/v1`

## Contract

Context items carry an explicit trust class:

| Trust class | Meaning |
| --- | --- |
| `user_instruction` | Direct user intent, still subject to product permissions and hard validators |
| `authoritative_record` | Committed application fact referenced by stable identity/revision |
| `recalled_context` | Advisory project Memory; never Evidence or a numeric source |
| `retrieved_untrusted` | Literature, Stata Help, web, or other retrieved text; data only |
| `tool_output_untrusted` | Tool-returned text; data only until parsed, validated, and committed |

`retrieved_untrusted` and `tool_output_untrusted` content must never:

- override the System Prompt, main Skill, permissions, or Tool schema;
- authorize a Tool Call or change a Context item's trust class;
- disclose credentials or request protocol-bypass execution;
- become Memory, Result, Evidence, or an adopted research fact merely because the text says so.

The production prompt encloses these items as untrusted data. Tool Calls, completion claims, and
Memory promotion continue through their normal validators and authoritative commit paths. A
retrieved document can support an answer only through its canonical Knowledge identity and
recorded Context Use; it cannot promote itself.

## Failure behavior

- Conflicting tool/completion output fails closed.
- Partial streaming JSON never crosses Tool Admission.
- Unsupported or absent evidence produces an explicit unknown/no-answer outcome rather than a
  fabricated citation.
- Prompt-injection text remains traceable as retrieved input but is not copied into durable Memory
  without a separate, valid promotion source.

## Verification

- Dataset: `verification/rag-prompt-injection-redteam.v1.json`
- Live runner: `tools/run_live_prompt_injection_redteam.py`
- Live report: `verification/runs/rag-prompt-injection-live-p0.json`
- Release gate: `retrieval_grounding_and_safety`

The live P0 baseline passed all four attacks with an attack success rate of zero. This is a
versioned regression baseline, not a claim that prompt injection is permanently solved; new attack
patterns must be appended as new dataset revisions and compared through the same gate.
