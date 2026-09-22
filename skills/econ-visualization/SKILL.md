---
name: econ-visualization
description: Design reproducible economics figures such as event-study plots, coefficient plots, binscatters, distributions, time series, and treatment-control comparisons when a research claim is easier to inspect visually.
metadata: {"version": "1.0.0"}
---

# Economics Visualization

Load this Skill only when the current research question actually needs a figure. It is adapted from
the CC0 `econ-visualization` Skill in `JonasWeinert/EconAgentSkills` at commit
`c1b8b1ef7ab59e6858fe3599a39d4732e5a76142`.

## Decide what the figure must show

- Start from one research question or diagnostic risk, not from a preferred chart type.
- Identify the plotted sample, units, transformations, grouping variables, reference period, and
  uncertainty interval. Reuse the adopted analysis sample when appropriate; disclose deviations.
- Use coefficient plots for estimates across periods, groups, or specifications; distribution plots
  for overlap and tails; scatter/binscatter for continuous relationships; and time-series plots for
  dynamics. Avoid dual axes, 3-D charts, decorative color, and figures with no interpretable claim.
- Ask the researcher only when chart alternatives encode materially different comparisons or when
  the displayed sample/estimand is unresolved. Ordinary formatting remains an Agent choice.

## Produce and retain the figure

- Generate figures from code. Never use a screenshot of a Results window as the research artifact.
- Stata is appropriate for `twoway`, `coefplot`, `marginsplot`, `binscatter`, and figures tied closely
  to estimation state. Python is appropriate when its plotting/data-shaping ecosystem is materially
  more useful; pass exact Artifact IDs and write outputs under `SRA_OUTPUT_DIR`.
- Prefer a vector format for a paper when the registered delivery path supports it. Use PNG for
  raster content or when the current preview/delivery path requires it.
- Use a colorblind-safe palette, readable labels with units, a visible zero/reference line where
  relevant, consistent scales across related panels, and uncertainty intervals for estimates.
- Keep the exact code, input Artifact identities, output Artifact, sample/filter description, and
  interpretation together. A visually persuasive figure does not override the underlying numbers.

## Product-specific evidence boundary

- A Stata-created figure remains tied to its Stata Operation and data-state chain.
- A Python/Shell figure must be classified as an exact `VISUAL` Analysis Output. Classification is
  not adoption. It can enter the formal research record only after the researcher confirms that
  exact fingerprint and preview Artifact in a later Turn.
- Do not represent a plotted Python regression as a qualified statistical Result. If coefficients
  matter to the paper, reproduce and qualify the estimation in Stata, then use those sourced values
  to construct or verify the figure.

## Interpretation check

Before treating the figure as useful, verify that it answers its stated question, labels the sample
and units, does not hide missing categories or truncated ranges, and distinguishes descriptive
patterns from causal estimates. Report visual anomalies as questions for the next research loop.
