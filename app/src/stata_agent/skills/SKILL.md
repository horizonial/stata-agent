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
