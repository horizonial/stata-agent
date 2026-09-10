# Econometrics Result Verification V1

> Status: implementation design for the next development round.
> Scope: trusted extraction and evidence-readiness verification for coefficient results.

## 1. Problem found in the current implementation

The real Stata transport is verified, but the evidence boundary is not yet a
verification boundary:

- `verify_result` only returns stored `machine` and provenance fields.
- `run_stata` asks the model to print generic `MACHINE_B/MACHINE_SE` markers and then
  automatically signs every finite machine value.
- Generic markers can be printed by model-controlled code before the trusted suffix;
  the parser currently accepts the first matching line.
- The machine layer does not bind a coefficient to its Stata term, estimator,
  dependent variable, VCE, cluster variables, or absorbed fixed effects.
- A succeeded run containing only `N` or `R2` can still produce cards and a generic
  supported claim.
- Evidence signing checks that the do-file exists, but does not recompute its hash.
- Automatic signing exceptions are swallowed, so callers cannot distinguish an
  exploratory run from a result that failed the evidence gate.

This permits a technically executable result that differs from the intended
specification to enter the evidence chain.

## 2. Decision

Introduce a versioned `ResultContract` and deterministic `VerificationReport`. A Stata
run may execute without a contract for exploration, but numeric cards may be signed
only when a contract is present and every required check passes.

Responsibilities remain separate:

1. **Method selection:** Skill + model + user approval decide what should be estimated.
2. **Specification conformance:** trusted extraction + deterministic verifier prove
   what Stata actually estimated and which coefficient was extracted.
3. **Research evidence:** evidence signer issues immutable cards only after verification.

The verifier does not claim that a causal design is substantively valid. It prevents
reporting a different estimator, outcome, term, VCE, cluster, or FE configuration than
the declared contract.

## 3. V1 contract

```json
{
  "schema_version": 1,
  "target_term": "1.treated#1.post",
  "estimator": "reghdfe",
  "dependent_variable": "outcome",
  "vce": "cluster",
  "cluster_variables": ["firm_id"],
  "fixed_effects": ["firm_id", "year"],
  "required_stats": ["coef", "se", "N"]
}
```

V1 supports coefficient evidence for `regress` and `reghdfe`. Other estimators may
still run, but are not evidence-ready until a later profile defines their stored-result
contract. Empty cluster/fixed-effect lists mean none are expected.

`target_term` uses a conservative Stata coefficient-name allowlist before trusted code
interpolates it into `_b[]` and `_se[]`. Contract fields never accept arbitrary source.

## 4. Trusted extraction

Extraction belongs in `StataExecutor`, not model-authored code or the prompt-facing
handler. For a contracted run, the executor appends trusted post-estimation commands
using an unpredictable per-run marker namespace. It extracts:

- `_b[target_term]` and `_se[target_term]`;
- `e(N)` and available `e(r2)`;
- `e(cmd)`, `e(depvar)`, `e(vce)`, `e(clustvar)`, and `e(absvars)`;
- Stata version/flavor already required by provenance.

The parser accepts only the namespace created for that run. Plain `MACHINE_B=...` text
from untrusted code is ignored. A missing target term or failed trusted extraction uses
the existing Stata rc failure path.

The semantic input hash covers model-authored code plus canonical contract JSON. The
command hash covers the exact persisted/executed do-file including the trusted suffix.
They are intentionally distinct: semantic input drives reuse; command hash proves
artifact integrity.

This follows Stata's documented `_b[]`/`_se[]`, `e(cmd)`, and `e(depvar)` interfaces;
`reghdfe` additionally documents `e(absvars)`, `e(clustvar)`, and `e(vce)`.

References:

- https://www.stata.com/manuals/pereturn.pdf
- https://www.stata.com/support/faqs/statistics/variance-covariance-matrix/
- https://scorreia.com/help/reghdfe.html

## 5. Verification report

The pure verifier returns stable fields: `schema_version`, `run_id`, `ok`,
`evidence_ready`, `contract_hash`, `machine_hash`, ordered `checks`, and bounded
`observed` metadata.

Check order and IDs are stable:

1. `run_exists`
2. `run_succeeded`
3. `contract_supported`
4. `provenance_trusted`
5. `artifact_hash_matches`
6. `required_stats_present`
7. `target_term_matches`
8. `estimator_matches`
9. `dependent_variable_matches`
10. `vce_matches`
11. `cluster_variables_match`
12. `fixed_effects_match`

The verifier fails closed on missing, malformed, non-finite, unsupported, or
contradictory fields. It may trim whitespace and compare cluster/FE lists
order-independently; it must not rewrite factor-variable semantics. An old run without
a contract returns `contract_missing`, remains immutable, and is not evidence-ready.

## 6. Evidence gate

`sign_run_numeric_cards` calls the deterministic verifier for real and explicit test
runs. It refuses to sign unless `evidence_ready=true` and hashes still match the run,
machine payload, contract, and do-file.

Numeric card locators add `target_term` for coefficient/SE cards, `contract_hash`, and
`verification_schema_version`. Existing actor/write-authority rules, card IDs,
append-only behavior, and idempotency remain unchanged.

A successful Stata run that fails verification remains a successful execution, but
returns zero new cards plus a structured verification failure. Execution failure and
evidence rejection are not conflated.

## 7. Skill behavior

The packaged causal-inference Skill must require the agent to declare outcome, target
term, estimator, VCE, clusters, and fixed effects before treating a coefficient as
reportable; use a contracted call for draft-bound numbers; inspect `evidence_ready`;
and never claim that software conformance proves causal validity.

No new intent router or second orchestration path is introduced.

## 8. Compatibility and boundaries

- Existing uncontracted exploratory commands continue to execute.
- Existing ledgers replay because the new RunRecord contract field is optional.
- No SQLite migration or new event type is required; the contract uses the existing run
  payload and projection.
- FakeExecutor remains explicit test/demo infrastructure and may emit only compatible
  test-only contracted results.
- `run_do_file` uses the same verification path when given a contract.
- UI, ChatService, provider, memory, RAG, writer, and outbox architecture do not change.

## 9. Deferred

- Proving causal identification or choosing an estimator for a research idea.
- IV, event-study, nonlinear, survival, survey, MI, bootstrap, or multi-equation profiles.
- Weak-IV, pretrend, balance, cluster-threshold, placebo, and multiplicity diagnostics.
- Prepared-data signatures and independent cross-machine reruns.
- New approval, event, database, UI, or distributed-runtime designs.

## 10. Acceptance boundary

V1 is complete when spoofed markers cannot create evidence; mismatched estimator,
outcome, term, VCE, cluster, or FE yield no cards; matching `regress` and `reghdfe`
contracts pass offline tests; one real Stata contracted regression passes the opt-in
live test; legacy histories replay; and all release gates remain green.
