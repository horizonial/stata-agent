# UI Fidelity Review

Date: 2026-09-07  
Viewport checks: 1600×1000 desktop; 700×900 narrow screen  
Design references: `ui-concept-primary.png`, `ui-concept-evidence-trace.png`

## Review ledger

| Area | Concept evidence | Browser render finding | Resolution |
|---|---|---|---|
| Shell proportions | Phase rail, conversation, inspector are full-height rails with 1px dividers | Three-rail skeleton matched; conversation was initially centered with a large dead gutter | Anchored conversation/composer to the left working rail and widened their container to 900px while retaining a 720px body measure |
| Phase rail | Step marker and label have visible separation | Current marker overlapped the Chinese label by several pixels | Increased marker-to-label clearance; verified at 1600×1000 |
| Typography | Quiet chrome, one strong current-turn heading | Dynamic provider summaries became oversized and repeated as body copy | Reduced dynamic turn heading to 20/28 and removed duplicated summary paragraphs |
| Palette | True white/cool neutral, charcoal, sparse cobalt | Render matched; no purple, gradients, glow, warm beige, remote fonts, or decorative art | Kept tokens locked in root `DESIGN.md` |
| Container model | Rails, rows, dividers; approval is the only bounded focal panel | Render uses open message turns and inspector rows; no card grid or bento | No change required |
| Radius / shadows | Radius ≤8px; shadow only for overlays | Render matched; shell has no elevation and overlays use one low shadow | No change required |
| Trace drawer | Expanded event table must remain inside the viewport | First implementation toggled content but kept a 40px flex basis, so rows overflowed below the viewport | Added an open-state flex basis and made content height derive from the drawer; rechecked filters and rows visually |
| Latest-turn focus | First viewport should land on the current decision/reply | Long history initially opened at the oldest turns; background previews also suspended rAF scrolling | Added message-key tracking and synchronous follow-latest scrolling; verified newest agent turn is visible after reload and send |
| Responsive collapse | At narrow widths phase rail hides and inspector becomes a Sheet | 700×900 render kept a single readable conversation, compact header, inspector trigger, composer and Trace bar without horizontal overflow | No material mismatch found |
| Interaction truthfulness | Unsupported actions must not fake success | Stop is visibly disabled and `/api/control/stop` returns 501 with readable copy | No change required |

## Functional QA

- Root HTML and local CSS/ES module returned 200.
- Desktop and narrow-screen layouts rendered without horizontal overflow.
- Trace opened, filtered surface remained legible, and rows stayed within the viewport.
- Browser send path was tested with the mock provider.
- Browser testing exposed a real SQLite writer-lease race between parallel read polling and chat writes. UI store sessions are now serialized process-locally; a stress smoke test completed one chat POST plus 72 concurrent projection reads with zero failures.
- User and ledger text are inserted via `textContent`; there are no third-party page resources.
- Full test suite and JavaScript syntax check pass.

## Intentional limitations

- Stop remains unavailable until the runner exposes a cancellation primitive; the UI does not claim otherwise.
- The visual concept includes rich approval/evidence data. Those states render when corresponding ledger records exist; the current demo database does not contain a signed evidence card.
- Generated concepts are design references only. All shipped interface text and controls are code-native.

## Wide-screen compatibility review · 2026-09-07

Reviewed against the user's 2139×1356 capture and re-rendered at 2139×1356,
1969×1248, and 1600×1000.

- Replaced the rail-wide phase-axis pseudo-element with one connector per pair
  of adjacent markers. The first and last markers now terminate the axis by
  construction rather than by hard-coded top/bottom offsets.
- Moved the blue current-phase indicator away from the numbered circle and
  constrained it to a centered 40px rail.
- Made the message scroller occupy the full center pane. Its scrollbar is now
  adjacent to the inspector divider instead of appearing inside the reading
  column with a large dead gutter to its right.
- Removed native WebKit scrollbar arrow buttons and normalized a quiet 10px
  track/4px visual thumb across messages, inspector, phase rail, Trace, and
  sheets.
- Removed the empty 42px desktop conversation header; the header is restored
  only when the mobile inspector control is needed.
- Expanded turn/composer surfaces to a 1440px wide-screen maximum while
  preserving the 720px prose measure. At 2139px the center pane is 1643px,
  the scrollbar sits at its right edge, and the composer ends 155px before the
  inspector rather than leaving the original roughly 430px dead zone.
- Browser geometry check found no horizontal document overflow.
