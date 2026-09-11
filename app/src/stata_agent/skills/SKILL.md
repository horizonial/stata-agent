---
name: causal-inference-mixtape
description: Route causal-inference questions to an appropriate design and report assumptions conservatively.
triggers: [did, difference-in-differences, causal, regression, event study, iv, rdd]
---

# Causal inference routing

Route from the treatment-assignment mechanism to a design before writing code.
State the identifying assumption, run the design-specific diagnostics, and
report the estimate with its sample, clustering, and limitations.  Do not use
causal language when the design does not support it.

## Structured result verification

Method selection is not software verification. Before reporting a coefficient,
declare a schema-version-1 `result_contract` with the target term, supported
estimator (`regress` or `reghdfe`), dependent variable, VCE, cluster variables,
fixed effects, and required statistics. Let the executor extract the result;
never print or trust `MACHINE_*` markers from model-authored code. Call
`verify_result` and only use numbers when `evidence_ready` is true. A Stata run
can succeed while contract verification fails; that is a specification failure,
not proof that the causal design is valid.
