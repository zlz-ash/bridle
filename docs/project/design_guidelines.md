<!-- SCOPE: Canonical visual system, component inventory, accessibility targets, and UI maintenance rules for the current Bridle frontend -->
<!-- DOC_KIND: explanation -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Read when changing React UI, design-system components, tokens, typography, colors, or accessibility behavior -->
<!-- SKIP_WHEN: Skip when you only need backend, API, persistence, or infrastructure contracts -->
<!-- PRIMARY_SOURCES: frontend/package.json, frontend/src/components/ds, frontend/src/components/ds/styles.css, frontend/src/styles/tokens -->
<!-- NO_CODE_EXAMPLES: This document defines visual and accessibility contracts without implementation snippets. -->

# Frontend Design Guidelines

## Quick Navigation

| Need | Read |
|---|---|
| Frontend runtime context | [Technology Stack](tech_stack.md) |
| UI ownership and system boundaries | [Architecture](architecture.md) |
| Shared components and theming | [Design System](#design-system) |
| Fonts and hierarchy | [Typography](#typography) |
| Palette and contrast expectations | [Colors](#colors) |
| WCAG target and verification | [Accessibility](#accessibility) |
| Test entry point | [Tests README](../../tests/README.md) |

## Agent Entry

Bridle is a local-first workspace and project-map tool. Its frontend uses React 18.3.1, TypeScript 5.7.2, Vite 5.4.11, TanStack React Query 5.62.7, and Vitest 2.1.8 with jsdom. New UI should extend the custom `brd-*` system and shared tokens rather than establish a parallel visual language inside feature components. Exact runtime values remain owned by the source paths listed in `PRIMARY_SOURCES`.

| Signal | Value |
|---|---|
| Purpose | Keep components, typography, color, layout, and accessibility behavior coherent. |
| Canonical | Yes, for documented frontend visual contracts. |
| Component source | `frontend/src/components/ds` |
| Shared styles | `frontend/src/components/ds/styles.css` |
| Token source | `frontend/src/styles/tokens` |
| Verification stack | Vitest with jsdom plus rendered keyboard, contrast, responsive, and assistive-technology checks where behavior requires them. |

## Design System

### System Identity

| Property | Current Contract |
|---|---|
| System | Custom `brd-*` component system |
| Framework | React 18.3.1 with TypeScript 5.7.2 |
| Build | Vite 5.4.11 |
| Server state | TanStack React Query 5.62.7 |
| Component implementation | `frontend/src/components/ds` |
| Token ownership | `frontend/src/styles/tokens` |
| Shared component styling | `frontend/src/components/ds/styles.css` |
| Customization approach | Change shared tokens or an existing component variant first; keep feature-specific styling local only when it expresses real domain semantics. |

### Verified Component Inventory

| Component | Responsibility |
|---|---|
| Button | Primary, secondary, and contextual actions through shared variants. |
| Card | Bounded content, state, or feedback surfaces. |
| Badge | Compact state, category, or count indicators. |
| AgentNode | Project-map agent and node state presentation. |
| ConversationBubble | Conversation content presentation. |
| IconButton | Compact icon-only actions with an accessible name. |
| Input | Shared text-entry behavior and visual states. |
| Switch | Binary settings with explicit labels and state. |
| Tabs | View selection with keyboard and selection semantics. |
| Toast | Transient status feedback that does not become the only error record. |
| Tooltip | Supplemental explanation for unfamiliar controls. |

### Verified Surface Inventory

| Surface | Responsibility |
|---|---|
| Header | Global workspace and application context. |
| InputBar | Primary message or command entry surface. |
| RightInspector | Selected-node details and state inspection. |
| ConversationCard | Conversation overlay and task context. |
| IslandPill | Compact floating status or action surface. |

### Inventory Boundary

The normalized project context does not list a shared Modal or Dialog component. Do not document or depend on a shared modal contract until one exists in the verified `brd-*` inventory. When a dialog is introduced, it must define labelling, initial focus, focus containment, Escape behavior, close semantics, and focus restoration before this inventory is updated.

## Typography

### Font Families

| Role | Current Family | Source |
|---|---|---|
| Interface and body | Geist Variable | `@fontsource-variable/geist` through the frontend dependency set |
| Identifiers, metrics, and technical text | Geist Mono Variable | `@fontsource-variable/geist-mono` through the frontend dependency set |

### Documented Type Scale

| Token | Size | Intended Use |
|---|---|---|
| `--text-display` | `34px` | Large metrics and intentionally dominant values. |
| `--text-h1` | `24px` | Primary view or panel heading. |
| `--text-body` | `14px` | Default dense workspace UI and body text. |
| `--text-caption` | `12px` | Captions and secondary labels. |
| `--text-micro` | `11px` | Compact metadata and eyebrow labels; not for long-form reading. |

Geist is a variable family, so weight is part of the hierarchy contract rather than a separate fixed-family inventory. Use the shared token and component definitions for actual weight values. Headings, body text, labels, and interactive states must remain distinguishable without relying on size alone, and feature code must not hardcode a replacement type scale.

## Colors

### Current Primary Palette

| Value | Semantic Role | Usage Boundary |
|---|---|---|
| `#0F1F1A` | Deep ink green | Primary dark canvas and map context. |
| `#1F1814` | Deep brown-black | Panels, chrome, and dark secondary surfaces. |
| `#F5E8D0` | Cream | High-contrast light text or light surface foundation. |
| `#FFC857` | Amber | Emphasis, focus, active, or running state. |
| `#E08855` | Copper | Warning or failure emphasis. |
| `#7FB89B` | Sage | Completed or success emphasis. |

These six values are the palette verified by the current normalized project context. Token names and composed alpha values remain owned by `frontend/src/styles/tokens` and `frontend/src/components/ds/styles.css`; this document must not invent additional current colors.

### Semantic Color Rules

| Purpose | Contract |
|---|---|
| Success | Sage may support success, but text or iconography must also communicate the state. |
| Warning and failure | Copper may support caution or failure, but the final meaning cannot depend on hue alone. |
| Active and focus | Amber may provide emphasis only when the resulting contrast and focus outline are measurable. |
| Canvas and panels | Ink green and brown-black establish dark surfaces; foreground colors require contrast verification for each actual pairing. |
| Light surfaces and text | Cream provides the light foundation; alpha composition must be tested against the rendered background. |

### Contrast Status

WCAG contrast compliance has not been established by this documentation rewrite. The palette is documented accurately, but each rendered foreground/background and state combination still requires measurement. Do not infer compliance from the color name, visual impression, or an opaque-value calculation when the implementation uses transparency.

## Layout and Interaction

| Concern | Contract |
|---|---|
| Workspace density | Preserve scannability and repeatable operation without turning the UI into a marketing layout. |
| Project map | Drag, zoom, selection, and node status must remain understandable and operable without covering essential context. |
| Inspector | Keep details and state in the inspector; do not turn it into an unrelated navigation root. |
| Conversation surfaces | Overlay behavior must not hide the current map state or trap keyboard users. |
| Toolbar | Reuse shared icon controls; unfamiliar icons require a visible or programmatic explanation. |
| Responsive behavior | Adapt layouts by available space and content pressure; verify actual supported widths rather than documenting unverified breakpoint values. |
| Text fit | Buttons, nodes, badges, tabs, and pills must not clip, overlap, or conceal adjacent content. |

## Accessibility

### Target and Current Status

The project target is WCAG 2.1 Level AA for user-facing frontend behavior. This is an acceptance target, not a claim that the current application has completed a full accessibility audit or satisfies every success criterion. Conformance language may be strengthened only after automated checks and relevant manual verification have produced retained evidence across supported flows and viewports.

### WCAG 2.1 AA Working Thresholds

| Area | Minimum Target |
|---|---|
| Normal text contrast | `4.5:1` against the rendered background. |
| Large text contrast | `3:1` when the text meets the applicable large-text definition. |
| Non-text UI and focus indicators | `3:1` against adjacent colors where the criterion applies. |
| Keyboard | Every interactive function is reachable and operable without a pointer. |
| Focus | Focus is visible, ordered logically, and restored after transient surfaces close. |
| Names and roles | Controls expose an accessible name, role, state, and value as applicable. |
| Status | Success, warning, failure, and progress are not communicated by color alone. |
| Motion | Nonessential motion respects the user's reduced-motion preference. |
| Reflow and text fit | Content remains usable without clipping, overlap, or loss of controls at supported sizes. |

### Component Accessibility Contracts

| Component Type | Required Behavior |
|---|---|
| IconButton | Provide a programmatic accessible name independent of the tooltip. |
| Input | Associate a visible label or equivalent accessible name and expose error/help relationships. |
| Switch | Communicate label, checked state, and keyboard operation. |
| Tabs | Preserve tab, tablist, and selected-panel relationships with expected keyboard movement. |
| Toast | Announce important state without stealing focus and preserve actionable failure details elsewhere. |
| Tooltip | Remain supplemental; essential instructions cannot exist only on hover. |
| Map node | Expose selection and status without requiring color perception or pointer-only interaction. |

### Verification Expectations

| Check | Evidence Needed |
|---|---|
| Unit and interaction behavior | Relevant Vitest and Testing Library assertions. |
| Keyboard path | Manual traversal of primary controls, map selection, inspector, and conversation surfaces. |
| Focus behavior | Visible focus, logical order, transient-surface entry, exit, and restoration. |
| Contrast | Measured rendered color pairs, including alpha-composited surfaces and states. |
| Screen reader semantics | Manual inspection of names, roles, states, relationships, and announcements. |
| Responsive and zoom behavior | Rendered checks across supported widths and text zoom, with no overlap or lost control. |
| Motion | Reduced-motion behavior verified for nonessential transitions. |

## Maintenance

**Update Triggers:**

- React, Vite, the component-system identity, or the verified component and surface inventories change.
- `frontend/src/styles/tokens` or `frontend/src/components/ds/styles.css` changes typography, palette, spacing, focus, or state semantics.
- Geist or Geist Mono dependencies, type roles, or the documented scale change.
- Accessibility audit findings alter the WCAG target, keyboard behavior, semantic contracts, contrast evidence, or verification scope.
- A shared dialog, responsive breakpoint system, or additional verified palette value is introduced.

**Verification:**

- [ ] React, TypeScript, Vite, React Query, and Vitest versions match the current normalized project context.
- [ ] The component and surface inventories match the custom `brd-*` system.
- [ ] Font families, type scale, token sources, and all six documented palette values remain current.
- [ ] Internal links and every `PRIMARY_SOURCES` path resolve.
- [ ] Accessibility wording distinguishes the WCAG 2.1 AA target from verified conformance.
- [ ] Contrast, keyboard, focus, semantics, responsive behavior, text fit, and reduced motion have evidence appropriate to the changed UI.
- [ ] The opening contract, required top sections, no-code rule, UTF-8 encoding, and final newline pass validation.

**Last Updated:** 2026-07-11
