# Stata Agent Design System

This file is the visual contract for the local empirical-research workspace. Functional and interaction detail lives in `design/ui-design-codex.md`.

## Source of truth

- Primary shell and first viewport: `design/ui-concept-primary.png`.
- Evidence Sheet and expanded Trace anatomy: `design/ui-concept-evidence-trace.png`.
- The second image is **not** authority for its alternative left navigation, product name, or top navigation. Those areas must follow the primary image.
- Requirements: `design/ui-requirements-codex.md`.

The concepts were produced before implementation and reviewed at original resolution. Implementation must preserve their information hierarchy, density, copy, container model, palette, type scale, borders, radii, and interaction placement.

## Visual direction

**Codex desktop × Linear discipline × Claude calmness, adapted to empirical research.**

The interface should feel like an instrument used for hours: quiet, legible, traceable, and precise. Its recognizable visual idea is a research phase rail feeding a single conversation surface, with provenance always one click away in an inspector or Trace drawer.

- Density: medium-high; compact chrome, generous reading measure.
- Background character: true neutral white and cool neutral gray. Never cream or warm beige.
- Personality: academic utility, not startup marketing and not BI dashboard theater.
- Primary focal point: the latest agent turn or unresolved gate.
- Decoration budget: zero representational imagery; icons only when they clarify an action.

## Layout lock

- Top toolbar: 52px.
- Left phase rail: 176px desktop.
- Center: flexible, minimum 560px; conversation measure capped near 760px.
- The message scroller always spans the full center pane so its scrollbar sits
  on the inspector divider. Turn surfaces and the composer may expand to
  1440px on wide monitors, while prose itself remains capped near 760px.
- Right inspector: 320px.
- Collapsed Trace bar: 40px; expanded drawer: 45–60vh.
- Composer stays at the bottom of the center surface, not at page top.
- Main separation uses full-height 1px dividers. Do not wrap all three columns in a giant rounded container.
- Phase-axis segments connect marker edge to marker edge. No line may protrude
  above the first marker or below the last marker at any viewport or zoom.
- Under 1024px: hide phase rail and expose phase in the header; inspector becomes a right Sheet.
- Under 720px: single-column conversation; Trace becomes full-width overlay; approval actions stack.

## Color lock

```css
--canvas: #f7f7f5;
--surface: #ffffff;
--surface-hover: #f2f2ef;
--text: #1f1f1d;
--text-muted: #6b6b66;
--text-faint: #8d8d86;
--border: #e3e3de;
--border-strong: #c9c9c2;
--accent: #2f63d8;
--accent-soft: #eef3ff;
--success: #2f7d4a;
--warning: #9a6700;
--danger: #b42318;
--focus: #6b8afd;
```

Color is semantic and sparse. Accent may appear on the current phase, active tab, evidence link, focus, and primary action. Status colors belong on a 2px rail, icon, or text—not on large tinted cards.

## Typography

No remote font requests. Use locally available UI fonts:

```css
--font-ui: "Segoe UI Variable", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
--font-mono: "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
```

- Workspace title: 17px / 24px / 600.
- Agent turn heading: 24px / 32px / 600.
- Body: 15–16px / 1.65 / 400.
- Primary UI controls: 14px / 20px / 500.
- Labels and inspector rows: 13–14px / 20px.
- Trace metadata: 12px / 18px; event names and IDs use mono.
- No all-caps labels, no single accented word in headings, no ornamental eyebrow text.
- Reading lines remain under about 78 characters.

## Geometry and spacing

- Spacing scale: 4, 8, 12, 16, 24, 32.
- Radius: 4px for small status/details, 6px controls, 8px dialogs/panels; never above 8px.
- Borders: 1px neutral; stronger only for focus or selected state.
- Shadows: none for shell/rows/cards; one low-elevation shadow only for Sheet, Dialog, or lifted composer on narrow screens.
- Buttons: 36–40px tall desktop, clear hover/focus/disabled states.
- Interactive rows: minimum 36px; important touch actions 44px on narrow screens.

## Container model

Allowed: rails, open sections, rows, tables, full-height panels, drawers, Sheets, dialogs, one bounded approval panel.

Avoid wrapping every message, metric, or inspector group in a Card. Group inspector fields through alignment and dividers. Agent messages are open content with a slim left status rail; user messages use a restrained gray band rather than a rounded chat bubble.

## Component families

- `TopBar`: title, local privacy indicator, precise run state, pending-gate anchor, draft action.
- `PhaseRail`: sequential progress only; read-only navigation to history.
- `ConversationTurn`: actor/time/phase metadata + composable content blocks.
- `ApprovalPanel`: research gate (blue) and permission gate (amber) variants.
- `ResultTable`: dense academic table; evidence-linked values are text links with a small chain icon.
- `Inspector`: Summary / Evidence / Runs tabs with row/list anatomy.
- `EvidenceSheet`: provenance chain, do-file location, data/environment fingerprints, two actions.
- `TraceDrawer`: filter toolbar, compact rows, expandable payload, cursor pagination.
- `Composer`: multiline input, Stop, Send; sending while busy clearly signals interruption.

## Icon inventory

Use one code-native 1.5px outline family, 16–18px, round caps/joins, `currentColor`. No emoji and no mixed filled/outline families.

| Meaning | Icon metaphor | Placement |
|---|---|---|
| local privacy | shield/check | top bar |
| export draft | download/document | top bar |
| current state | clock/activity | top bar |
| approve | shield/check | primary approval action |
| reject | circle/slash | reject action |
| modify | pencil | modify action |
| attach | paperclip | composer |
| stop | square | composer |
| send | arrow up-right/paper plane | composer |
| expand | chevron | trace/payload/disclosure |
| provenance | chain link | result number |
| copy | overlapping squares | evidence sheet |

## Visible-copy allowlist for first viewport

Do not invent subtitles, badges, fake metrics, or explanatory marketing copy above the fold. The first screen may use only content supplied by state plus this UI vocabulary:

`实证研究 Agent` · idea title · `本机运行` · run-state text · `生成 Word 初稿` · the eight phase names · `你` · `Agent` · `摘要` · `证据` · `运行` · `待你批准` · gate subject · `批准并继续` · `拒绝并说明` · `提出修改` · `给 agent 发消息…` · `停止` · `发送` · `Trace` · event count.

Empty state may add exactly one useful instruction: `描述研究问题、数据范围，或粘贴现有模型设定。`

## Interaction and motion

- Motion follows user action: Sheet open/close, Trace drawer resize, approval confirmation, tab change.
- Duration: 150–180ms; easing `cubic-bezier(.2,.8,.2,1)`.
- Do not animate every row/card on load. No decorative pulsing; only a small working-state indicator may pulse.
- Respect `prefers-reduced-motion`.

## Forbidden defaults

- Purple gradients, random gradients, glow, glassmorphism.
- Warm off-white/beige canvas.
- Inter fetched from the web or any third-party font/script/analytics.
- Radius above 8px, large soft shadows, floating bento tiles.
- Three-column SaaS feature cards or dashboard KPI cards.
- Emoji icons, status-pill soup, fake labels, `BETA`, marketing nav, oversized hero headings.
- A separate left app navigation containing pages such as Data/Models/Documents. This is a single-idea workspace; phase rail is the only left rail.
- Rounded chat bubbles, generic avatars, ornamental illustrations.
- Raw JSON as the primary state view or raw stack traces as errors.

## Fidelity and review gate

Implementation is not complete at “tests pass.” Before handoff:

1. Run the actual FastAPI UI in a browser.
2. Capture a 1600×1000 primary-screen screenshot and a screenshot with evidence Sheet + Trace drawer open.
3. Inspect both screenshots at original resolution beside the two concept images.
4. Record and fix mismatches across at least: shell proportions, first-viewport copy, typography, palette, border/radius/shadow, component density, icon consistency, and narrow-screen collapse.
5. Verify the core flow: send → working feedback → reply/gate; open evidence; filter/open Trace; unavailable backend actions must not pretend to succeed.
6. Verify keyboard focus, Escape on overlays, Enter/Shift+Enter behavior, 200% text zoom, and mobile overflow.
7. Remove QA-only artifacts after review, keeping only the two accepted concept images.

No implementation may reinterpret the accepted shell or add a new component family without updating this file first.

## Design references

The workflow and constraints were adapted from OpenAI `frontend-app-builder`, Anthropic `frontend-design`, `frontend-ui`, `ckw-design`, and `design-md`: design before code, a subject-specific visual direction, explicit tokens/container rules, and screenshot-based critique rather than stopping when the app merely runs.
