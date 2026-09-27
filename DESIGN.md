---
name: Beacon
description: "Daylight Flight Deck for evidence-first YouTube opportunity research."
colors:
  canvas: "#F3F6FA"
  panel: "#FFFFFF"
  surface-alt: "#F7F9FC"
  surface-strong: "#EEF3F8"
  border: "#D9E1EA"
  text: "#14233B"
  muted: "#66758A"
  signal-green: "#17B978"
  signal-green-dark: "#0C8C59"
  signal-green-soft: "#E6F7F0"
  analysis-cobalt: "#4E67E8"
  analysis-cobalt-soft: "#EEF1FF"
  danger: "#D94F5C"
  danger-soft: "#FDECEF"
  amber: "#C98213"
  amber-soft: "#FFF4DE"
  ink-blue: "#10243E"
  ink-blue-active: "#1B385B"
  rail-text: "#F7FAFD"
  rail-muted: "#AFC0D2"
typography:
  display:
    fontFamily: "Bahnschrift"
    fontSize: "28px"
    fontWeight: 700
  headline:
    fontFamily: "Bahnschrift"
    fontSize: "17px"
    fontWeight: 700
  title:
    fontFamily: "Segoe UI Variable Text"
    fontSize: "15px"
    fontWeight: 700
  body:
    fontFamily: "Segoe UI Variable Text"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.35
  label:
    fontFamily: "Segoe UI Variable Text"
    fontSize: "11px"
    fontWeight: 600
rounded:
  small: "8px"
  inset: "10px"
  control: "11px"
  tile: "12px"
  brand: "13px"
  card: "14px"
  chip: "18px"
  pill: "20px"
  circle: "999px"
spacing:
  micro: "4px"
  compact: "6px"
  control: "8px"
  field: "10px"
  body: "12px"
  grid: "14px"
  section: "16px"
  content: "18px"
  panel: "20px"
  shell: "30px"
components:
  button-primary:
    backgroundColor: "{colors.signal-green}"
    textColor: "{colors.ink-blue}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "12px 18px"
  button-outlined:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "10px 14px"
  input-field:
    backgroundColor: "{colors.surface-alt}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  nav-active:
    backgroundColor: "{colors.ink-blue-active}"
    textColor: "{colors.rail-text}"
    typography: "{typography.label}"
    rounded: "{rounded.tile}"
    padding: "9px 11px"
  status-pill:
    backgroundColor: "rgba(23, 185, 120, 0.11)"
    textColor: "{colors.signal-green}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "3px 8px"
  surface-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: "{spacing.panel}"
  opportunity-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: "{spacing.section}"
  agent-audit:
    backgroundColor: "{colors.analysis-cobalt-soft}"
    textColor: "{colors.text}"
    rounded: "{rounded.inset}"
    padding: "5px 9px"
  autopilot-audit:
    backgroundColor: "{colors.surface-alt}"
    textColor: "{colors.text}"
    rounded: "{rounded.inset}"
    padding: "5px 9px"
---

# Design System: Beacon

## Overview

**Creative North Star: "Daylight Flight Deck"**

Daylight Flight Deck turns Beacon into a bright operational research desk rather than a generic analytics dashboard. A fixed ink-blue rail anchors a cool paper canvas, while white evidence surfaces, compact outlined controls, and restrained state color make a dense investigative app feel calm and legible.

The system is evidence-first: opportunity cards keep identity, eligibility, proof metrics, confidence, and the plain-language reason for qualification in one sweep. Beacon Green marks live, approved, selected, or successful state; Analysis Cobalt identifies analytical context, provenance, and technical detail; Agent and Autopilot actions remain transparent through progressive-disclosure audit patterns.

**Key Characteristics:**

- Bright cool-paper workspace anchored by a fixed ink-blue rail.
- Beacon Green for live/approved state and Cobalt for analysis/provenance.
- Compact desktop density with generous outer gutters and wrapped controls.
- White cards over tonal inset surfaces, using one restrained ambient shadow.
- Evidence-first opportunity cards and reversible, expandable Agent audits.

## Colors

The palette separates operational state from analytical context while keeping the working canvas bright and quiet.

### Primary

- **Beacon Green** (`colors.signal-green`): Primary actions, active/live marks, successful results, selected operational states, progress, and faceless/replicable eligibility.
- **Deep Beacon Green** (`colors.signal-green-dark`): Green text and icons that need stronger contrast on light or soft-green surfaces.
- **Beacon Green Wash** (`colors.signal-green-soft`): Selected presets, safe automation icon fields, and low-intensity positive state surfaces.

### Secondary

- **Analysis Cobalt** (`colors.analysis-cobalt`): Analytical headings, topics, provenance, technical tool detail, informational icons, and discovery-time context.
- **Cobalt Wash** (`colors.analysis-cobalt-soft`): User messages, information strips, active filter chips, examples, and expandable technical disclosures.

### Tertiary

- **Operational Amber** (`colors.amber`): Waiting, stalled, paused, or attention-needed states that are not failures.
- **Danger Red** (`colors.danger`): Errors, destructive controls, and failed tool results only.
- **Soft State Washes** (`colors.amber-soft`, `colors.danger-soft`): Low-intensity backgrounds for their matching semantic states.

### Neutral

- **Ink Blue** (`colors.ink-blue`): The fixed navigation rail and high-authority structural anchor.
- **Active Ink Blue** (`colors.ink-blue-active`): The selected navigation tile.
- **Cool Paper** (`colors.canvas`): The application canvas.
- **White Evidence Surface** (`colors.panel`): Cards, dialogs, the status bar, and other primary reading surfaces.
- **Quiet Surface** (`colors.surface-alt`): Metric bands, evidence bands, transcript bubbles, target rows, and other inset groups.
- **Strong Quiet Surface** (`colors.surface-strong`): Progress tracks and slightly stronger tonal separation.
- **Hairline Blue-Gray** (`colors.border`): Dividers, field outlines, and inactive control strokes.
- **Deep Text** (`colors.text`): Primary copy and metrics.
- **Muted Steel** (`colors.muted`): Secondary copy, timestamps, helper text, and inactive controls.
- **Rail White / Rail Muted** (`colors.rail-text`, `colors.rail-muted`): Primary and secondary copy on the ink-blue rail.

**The State Has a Job Rule.** Beacon Green means live, allowed, selected, successful, or eligible; Amber means waiting or attention; Danger Red means failure or destructive intent.

**The Analysis Is Cobalt Rule.** Use Analysis Cobalt for interpretation, provenance, technical detail, and information—not for operational success.

**The Paper Before Chrome Rule.** Keep most of every screen in Cool Paper, White Evidence Surface, and quiet tonal insets; accent color should clarify state rather than decorate empty space.

## Typography

**Display Font:** Bahnschrift

**Body Font:** Segoe UI Variable Text

**Character:** Bahnschrift gives page titles, the Beacon wordmark, opportunity scores, and key metrics a condensed flight-instrument authority. Segoe UI Variable Text keeps controls, explanations, audit records, and dense evidence readable in a familiar Windows voice.

### Hierarchy

- **Display** (700, 28px): One page title per screen.
- **Brand** (Bahnschrift 700, 21px): Beacon in the navigation rail.
- **Headline** (700, 16–17px): Card leads, section headings, and primary task labels.
- **Title** (700, 15px): Channel identity and compact audit-item titles.
- **Metric** (Bahnschrift 700, 17–20px): Opportunity score, evidence metrics, and index statistics.
- **Body** (400, 12px; 1.35 line-height in Markdown): Explanations, system state, and normal working copy.
- **Label** (600–700, 10–11px): Pills, metadata, timestamps, field groups, and supporting evidence.

**The Display Earns Its Weight Rule.** Reserve Bahnschrift for identity, hierarchy, and numbers that deserve instrument-like emphasis; do not set paragraphs or dense controls in it.

## Layout

Beacon is a desktop control room with a fixed 230px navigation rail, a fluid content workspace, and a slim bottom status bar. The default window is 1320×860 and the enforced minimum is 1040×700; this system is not designed as a phone layout.

The content workspace uses 30px horizontal gutters, 26px top padding, 34px bottom padding, and an 18px vertical content rhythm. Primary cards use 20px internal padding with 12px between children. Major responsive groups use a 12-column grid with 16px gaps; opportunity grids use 14px gaps; compact stat grids use 10px gaps.

At the minimum window size, toolbar rows, chips, segmented controls, and action groups wrap instead of clipping. Opportunity cards are full width at small widths and two-up from the medium desktop range. Overview and Find use a 7/5 split only when space permits, then collapse both cards to full width. The navigation rail remains fixed while the evidence area absorbs width changes.

**The Minimum Is a Contract Rule.** Every primary workflow must remain complete, readable, and unclipped at 1040×700.

**The Evidence Stays in Reach Rule.** Wrap controls and collapse columns before separating identity, eligibility, metrics, rationale, or audit outcome from the surface where the operator makes the decision.

## Elevation & Depth

Depth is a hybrid of tonal layering and one ambient card lift. Primary white cards use a single cool ink shadow (`0 7px 22px -8px rgba(16, 36, 62, 0.16)`). Inset metric bands, information strips, transcript bubbles, and audit disclosures stay flat and use quiet surface color or a one-pixel border. The navigation rail is structural color, not a floating layer.

### Shadow Vocabulary

- **Ambient Card Lift** (`0 7px 22px -8px rgba(16, 36, 62, 0.16)`): Outer cards and clickable result shells only.
- **Flat Inset** (no shadow): Evidence bands, stat cells, status strips, active filters, and audit-detail surfaces.

**The One Ambient Lift Rule.** Apply the ambient shadow to the outer decision surface only; nested evidence groups must remain flat.

## Shapes

The form language is softly technical: compact controls and nested tonal groups use 10–12px corners, primary cards use 14px corners, and state chips use 18–20px capsules. The 13px Beacon brand mark and circular avatars add recognition without turning the interface playful.

Hairline borders are used for control affordance, inactive states, dividers, and explicit warnings. Rounded capsules are reserved for short status, preset, filter, and eligibility labels—not for paragraphs or whole cards.

**The Radius Follows Nesting Rule.** Outer cards use 14px corners; controls and icon tiles use 11–12px; inset evidence groups use 10px; only compact chips and identity circles become fully rounded.

## Components

### Buttons

- **Primary:** Beacon Green fill, Ink Blue text, 11px corners, and 12×18px padding. Use for the single forward action in a task group, such as Ask Beacon, Start sweep, Apply, or Save.
- **Outlined:** White or transparent fill with a Hairline Blue-Gray stroke, 11px corners, and compact padding. Accent the stroke only when the action is contextually active or important.
- **Destructive:** Keep the outlined form and switch both label and stroke to Danger Red.
- **Hover / Focus:** Preserve the fill hierarchy, use the platform Material ink response, and provide a visible focus outline without adding decorative lift.

### Chips

- **Status pills:** 20px capsules with a tinted background, light border, icon or dot, and an explicit text label.
- **Preset chips:** 18px corners; selected presets use Beacon Green Wash with Deep Beacon Green text and a Beacon Green border.
- **Active filters:** Cobalt Wash with Cobalt text and a visible remove icon.
- **State:** Never communicate live, waiting, failed, or selected state by color alone.

### Cards / Containers

- **Primary cards:** White Evidence Surface, 14px corners, 20px padding, and Ambient Card Lift.
- **Inset groups:** Quiet Surface, 10–12px corners, 8–13px padding, and no shadow.
- **Responsive groups:** Use the 12-column grid and wrapping rows; do not force fixed-width controls past the minimum window.

### Inputs / Fields

- **Style:** Dense outlined fields with Hairline Blue-Gray strokes; use Quiet Surface fill for the primary Agent prompt and white/default fill for compact filter and search fields.
- **Focus:** Shift emphasis to Analysis Cobalt or Beacon Green while retaining a visible outline.
- **Dialogs:** Filter dialogs remain compact (360px content width), grouped by plain-language sections, with Reset, Cancel, and one green Apply action.

### Navigation

- **Rail:** Fixed 230px Ink Blue column with 20px padding.
- **Brand:** A 42px Beacon Green mark paired with the Bahnschrift wordmark and a small “Opportunity control” descriptor.
- **Items:** 12px tiles with 9×11px padding. The active tile uses Active Ink Blue, Rail White text, and a green icon field; inactive items use Rail Muted.
- **Workspace state:** A quiet translucent inset at the bottom states “Private workspace” and “One live index.”

### Opportunity Brief

The signature result card reads as one compact decision brief:

1. Identity: avatar, channel name, topic, and a Bahnschrift opportunity score.
2. Eligibility: a visible “Faceless + replicable” pill and external-link affordance.
3. Proof: one flat divided metric band for average views, subscribers, uploads, and age.
4. Confidence: a “Why it qualifies” inset with evidence strength, named factors, and a plain-language rationale.
5. Freshness: Real Time adds explicit discovery-time pills without confusing discovery time with channel age.

**The Evidence Before Score Rule.** A score may lead the eye, but the card is incomplete without eligibility, sample size, leverage, recency, and a readable reason for qualification.

### Agent and Autopilot Audit

The Agent screen puts the investigation input first and keeps every response legible:

- User prompts appear in Cobalt Wash.
- Assistant responses use Markdown on Quiet Surface.
- Tool calls are compact Cobalt Wash expansion rows labeled “Tool used,” with technical parameters hidden until expanded.
- Tool results are separate plain-language outcome rows with a green check or red error and concrete counts when available.
- A visible “Writes stay reversible” pill keeps the safety model close to the action.
- Autopilot history uses timestamped, compact expansion rows with the tools used, accepted/rejected query changes, and the full maintenance summary inside.

**The Audit Before Assurance Rule.** Never rely on an AI summary alone to imply what changed; show the tool, outcome, status, count, reversibility, and expandable detail as separate evidence.

## Do's and Don'ts

### Do:

- Do keep the Cool Paper canvas and fixed Ink Blue rail as the stable frame across all five screens.
- Do use Beacon Green for live/approved state and Analysis Cobalt for interpretation, provenance, and technical detail.
- Do keep identity, eligibility, metrics, and “Why it qualifies” evidence together on every opportunity card.
- Do wrap controls and collapse responsive columns so every workflow remains usable at 1040×700.
- Do pair semantic color with text, icons, counts, or status words.
- Do present Agent and Autopilot actions as reversible, plain-language, expandable audit evidence.
- Do design loading, empty, success, stalled, and error states as first-class operational surfaces.

### Don't:

- Don't turn Beacon into a generic dark analytics dashboard or copy another creator tool's branding.
- Don't use Beacon Green and Analysis Cobalt interchangeably; each has a distinct semantic job.
- Don't surface an opportunity score without the sample, leverage, recency, and qualification rationale that support it.
- Don't stack shadows, nested metric tiles, or decorative gradients inside evidence cards.
- Don't hide tool outcomes, failures, or state changes inside prose-only Agent responses.
- Don't rely on color alone or shrink essential labels below the established 10–11px support range.
- Don't introduce mobile-only composition assumptions; preserve the enforced desktop minimum and wrapping behavior.
