---
name: empirical-research-main
description: Default end-to-end Stata empirical-research workflow for traceable local research.
metadata: {"version": "2.4.0"}
---

# Empirical Research Main Skill

This is the default research policy for a local Stata Research Agent. It is adapted from the
CC0 `working-with-data`, `stata-data-cleaning`, and `stata-regression` skills in
`JonasWeinert/EconAgentSkills` at commit
`c1b8b1ef7ab59e6858fe3599a39d4732e5a76142`.

## Product contract

- The Agent may work autonomously, but the researcher owns substantive judgment.
- Use only registered tools. Never invent a dataset, variable, command result, coefficient,
  standard error, sample size, table, or figure.
- Formal numeric claims and Word tables must come from qualified Stata Results and Evidence.
- Preserve the original input. Transformations operate on an isolated working copy and every
  adopted Data Version remains recoverable.
- Create a provisional semantic Plan before formal estimation. The Plan records research intent,
  dependencies, and unresolved choices; it is not an exact-command whitelist. Bind each formal
  Stata call to the semantic node it advances. Revise the Plan when evidence changes the intended
  direction instead of silently changing the design.
- Prefer a useful reversible first pass. Do not turn ordinary exploratory choices into constant
  interruptions.

## Decision policy

Proceed and document when a choice is reversible and conventional: data inspection, uniqueness
checks, descriptive summaries, a simple exploratory specification, reproducibility scaffolding,
table formatting, and non-destructive diagnostics.

Pause for the researcher when unresolved alternatives can materially change the research answer:
the outcome or treatment definition, unit of analysis, identification strategy, merge cardinality,
sample exclusion, missing-data treatment, clustering level, or a transformation that changes the
estimand. Present concrete options, likely consequences, and a recommended default.

Never overwrite raw input, silently drop observations, use an `m:m` merge, silently swap an
estimator, or represent exploratory Python output as a formal regression Result.

## Research loop

1. Inspect the available data through Stata before selecting variables.
2. State the provisional research question, unit of analysis, outcome, focal explanatory or
   treatment variable, controls, sample, and identification limits.
3. Check IDs, duplicates, missingness, ranges, labels, panel/time structure, and merge assumptions.
4. Record each cleaning or construction action, its rationale, and before/after observation count.
5. Run a simple baseline before advanced models. Match the estimator and inference to the design.
6. Add diagnostics and robustness checks that answer a stated risk; do not generate a large ritual
   battery with no connection to the Plan.
7. Export tables with `esttab` or another registered Stata exporter. Keep estimation state and
   table provenance together.
8. Write the Word draft only from adopted Results/Evidence. Distinguish what the data show from
   interpretation, assumptions, limitations, and suggestions for the next iteration.
9. Stop only when the current Turn goal is covered, or pause/return partial with explicit gaps.
   Once every requested deliverable is durably committed and its delivery gate passes, terminate
   the Turn. Do not reopen satisfied obligations for incidental diagnostics or speculative polish.

## Registered tool protocol

The sequence below is guidance, not a fixed pipeline. Revisit earlier steps whenever evidence
requires it, and use as many exploratory calls as the research problem needs.

- Use `stata.execute` for Agent-selected Stata code. Set `execution_role=data_step` for inspection,
  cleaning, construction, diagnostics, graphs, and exploratory models. The Stata session preserves
  the resulting data state across calls. Set `reset_data=true` only when deliberately returning to
  a clean managed copy.
- An Execution Scope materializes authorized input files, not undeclared empty sibling directories.
  Before running an inspected do-file, identify any output directories it assumes and create those
  directories explicitly in the same Stata Call (for example with bounded `capture mkdir` steps).
  Declaring an `artifact_outputs` staging target creates that target's staging parent, but does not
  create a separate directory expected by the do-file itself.
- When a Stata estimation is intentionally proposed as a formal Result, run that estimation as a
  focused call with `execution_role=formal_result_candidate`. Do not bundle unrelated preparation
  commands into that same call. Supply `plan_node_key` for the matching node in the current Plan;
  the Runtime freezes the Plan/Node identities before Tool Admission while preserving the exact
  executed command separately.
- Use `research.promote_stata_result` only after checking the returned Stata output and deciding
  which returned values matter to the current research claim. Supply the exact source Operation ID
  and select only source keys advertised in that Operation's generic Stata Result Catalog. Select
  coefficients, uncertainty measures, sample statistics, diagnostics, and other returned values
  that a researcher would need to understand or audit the result. The Runtime verifies identity
  and provenance; it does not decide whether the method or selected diagnostics are substantively
  appropriate. A rejected promotion is evidence to inspect or rerun, never permission to invent a
  substitute value.
- A `stata.execute` Result Catalog represents the final stored `e()`/`r()` state, not every
  intermediate `summarize`, local, scalar, display, or post-estimation command in one code block.
  When the deliverable needs several descriptive values or transformations, make each requested
  number directly auditable: use focused formal Calls or a Stata command/parameterization that
  stores the needed values together, then promote the exact advertised source keys. Do not label
  a contrast, gap, or regression coefficient as a level/cell mean merely because the latter can be
  reconstructed mentally. If a requested number is a linear or nonlinear transformation, obtain
  it as an explicit Stata stored result or preserve a qualified Stata derivation receipt before
  reporting it.
- Use `research.export_word` only for an adopted Result slot. It invokes the registered Stata table
  exporter and the Word delivery gate; never manually transcribe coefficients into the document.
  The selected Result must contain the exact advertised `term.<name>.coefficient`,
  `term.<name>.se`, `scalar.N`, and requested fit-statistic keys consumed by this table contract.
  Supply the coefficient terms and returned fit-statistic name used by the requested table. These
  are presentation choices, not a declaration that the Runtime recognizes the research method.
  A scalar-only descriptive or power Result cannot satisfy the current coefficient-table contract.
  Do not invent a treatment variable or repeatedly probe for one merely to make a table. When the
  research task supports a transparent noncausal association, a clearly labelled descriptive
  e-class model may serve as the table Result; state its limited purpose and do not reinterpret it
  causally. If no honest coefficient-bearing analysis is supported, pause with that precise output
  limitation instead of retrying unrelated estimators.
  When the user asks for a draft manuscript, provide the optional manuscript sections as connected
  prose covering the question, data and methods, interpretation, limitations, and conclusion. The
  current prose scope must contain no literal numeric claims; all formal numbers remain in the
  Evidence-bound Stata table. Describe direction and uncertainty honestly and let the delivery
  validator reject any unbound number rather than paraphrasing it around the gate.
- Do not ask whether an estimator has a registered method Profile. Any Stata command with a
  complete generic Result Catalog can become a formal Result when its exact returned values pass
  the provenance gates. Method choice, required diagnostics, interpretation, and what to retain are
  research judgments governed by this Skill, specialized Skills, Agent reasoning, and the user.
- If a useful statistic was not promoted, rerun the exact recorded recipe and select it from the
  new Operation. The rerun creates a new Run/Result and must not rewrite a prior Result or Word
  revision.

### Optional Python and Shell exploration

Python and Shell are optional research instruments, not required stages. Use them when they offer a
clear advantage for data preparation, visualization, tests, or other exploration, and prefer Stata
when it is already the simpler or more reproducible choice.

- Use `python.run` or `shell.run` only with explicit input Artifact IDs. When continuing from a
  Stata data step, pass the exact `data_artifact_id` returned by `stata.execute`; do not rediscover
  an untracked copy by path.
- Write intended outputs under `SRA_OUTPUT_DIR`. The runtime captures the exact submitted code and
  produced files as managed Artifacts. Do not claim an output that is absent from the returned
  captured Artifact list.
- If an exploratory output may matter to the research record, classify its exact Artifact IDs with
  `research.classify_analysis_output`. Classification records what was produced; it does not adopt
  the output or make it eligible for a document.
- Treat `REGRESSION` and `UNKNOWN` Python/Shell outputs as ineligible for formal adoption. Re-run a
  regression intended for the paper in Stata, then promote the exact values returned by that Stata
  Operation.
- A non-regression `VISUAL`, `SCALAR`, `TABLE`, `TEST`, or `CUSTOM` output may become Evidence only
  after the researcher confirms that exact candidate in a later Turn. The confirmation should bind
  the candidate fingerprint and preview Artifact, not merely a vague category such as "the Python
  chart". Then use `research.adopt_analysis_output`; never infer approval from silence or from the
  fact that the code ran successfully.
- Classify only outputs worth retaining. Disposable exploration can remain in Trace without being
  promoted into the research record.

### Specialized Skills

The registered specialized Skill catalog contains optional, task-specific guidance. Its names and
descriptions are visible without loading the bodies. Call `research.load_skill` with the listed
exact name and revision only when the current task matches a description or the researcher invokes
that Skill. Do not load every Skill preemptively, and do not treat a loaded Skill as executable code
or as permission to bypass the product's provenance and confirmation boundaries.

## Data discipline

- State and assert the unit key with `isid` when one is known.
- Declare merge cardinality and inspect `_merge`; reject unexplained many-to-many joins.
- Treat missingness as a research question: describe patterns, propose plausible mechanisms, and
  compare deletion, imputation, alternative variables, or sample definitions when consequential.
- Log transformations and filters. A future reader must be able to reconstruct the analysis sample.
- Prefer long format for panels and validate `xtset`/`tsset` before lags, leads, or differences.

## Estimation discipline

- OLS is an exploratory default, not automatic causal identification.
- Fixed effects, clustering, IV, DiD, event studies, RDD, matching, weighting, and other estimators
  require their identifying structure to be visible in the Plan.
- For staggered treatment, do not silently rely on a single naive TWFE estimate.
- For IV, retain first-stage and weak-instrument diagnostics; for DiD, retain timing and pre-trend
  evidence; for RDD, retain bandwidth and continuity/manipulation diagnostics.
- Report failures, empty samples, convergence problems, unsupported commands, and specification
  changes exactly as observed.

## Adaptive Plan discipline

- Re-plan only when there is a concrete trigger: new user direction, a material data finding,
  failed or contradictory execution, a completed node that changes the next question, or a
  research-quality finding. Put `change_kind` and `trigger_references` in the structured Plan so
  the revision explains why it exists.
- Keep still-relevant future nodes when revising. Do not replace the whole research direction with
  only the commands in the current Step.
- Reusing the same semantic Plan is not a new revision. A new revision must change intent,
  dependency, or an explicit node specification.
- If the change depends on a consequential user decision, ask first. Do not submit and adopt a new
  Plan in the same output as a Waiting request.
- Node kinds are open research vocabulary. Use a concise stable name that fits the method at hand;
  do not force a new or literature-derived method into a fixed built-in taxonomy.

## Deliverables

For each completed direction retain: Plan revision, input Data Version, executed Stata commands,
Run/Result identities, exported tables or figures, Evidence uses, Word revision, decisions and
unresolved questions. The readable summary and raw Trace must point to the same authoritative facts.
