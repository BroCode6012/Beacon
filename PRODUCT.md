# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Beacon is a private, single-operator desktop application for a non-technical
YouTube researcher. The operator uses it to discover and evaluate faceless,
replicable channel formats without needing to understand the scraper, database,
or machine-learning systems underneath.

## Product Purpose

Beacon is a standalone YouTube opportunity control room. It continuously
collects channels, helps the operator find unusually strong and repeatable
formats, organizes promising channels into niche workspaces, monitors the
discovery pipeline, and provides an AI agent for investigation and maintenance.

Success means the operator can quickly answer: what is newly emerging, what is
worth investigating, why it looks promising, and what the system is doing now.

## Positioning

Beacon is intentionally narrower than a general YouTube analytics product. It
is built to surface faceless formats that a new creator could realistically
repeat. Music, celebrities, children's content, major brands and networks,
one-off viral accidents, malformed records, and unstable tiny-data ratios are
suppressed rather than celebrated.

Its differentiator is the combination of a continuously growing private index,
replicability-focused opportunity ranking, discovery-time monitoring, deep
niche investigation, and an operator-controlled AI agent in one desktop
control room.

## Operating Context

- The operator uses Beacon as a Windows desktop app across five primary
  screens: Overview, Channels, Real Time, Find, and Agent.
- Channels and Real Time are high-density browsing surfaces with combinable
  filters, topic selection, presets, progressive paging, and direct YouTube
  links.
- Overview explains index health, research coverage, cloud collection,
  organization backlog, and radar state in plain language.
- Find runs targeted niche investigations.
- Agent supports natural-language investigation and reversible maintenance.
- Beacon owns the one canonical YouTube index while shared scraper operations
  and state live in the Scripter project.

## Capabilities and Constraints

- Beacon is a Flet desktop application and normal UI features use
  `FinderClient`.
- Beacon has one private operator for now; team accounts, permissions, and
  collaboration are out of scope.
- The product distinguishes topic, niche workspace, and YouTube category.
- Discovery time and YouTube channel age are different clocks.
- Opportunity views require faceless and replicable eligibility.
- Exclusions are reversible; channel and video rows are not deleted.
- Full-token and read-token authorization must remain distinct.
- The canonical live database must not be copied, reset, replaced, or casually
  backed up.
- The Beacon Bedrock agent remains separate from Production Lab chat and agent
  modules.
- Product behavior must be understandable to a non-technical operator.

## Brand Commitments

- The product name is Beacon.
- Beacon should feel like a focused investigative control room, not a generic
  admin dashboard.
- The interface may take inspiration from Nexlev's clarity and creator-focused
  analytics patterns, but must preserve Beacon's distinctive emphasis on
  evidence, replicability, discovery operations, and operator control.
- Voice is concise, calm, direct, and explanatory. Labels should describe
  outcomes before implementation details.

## Evidence on Hand

- The live product and its real data provide the primary evidence.
- Existing workflows, status states, filters, cards, and operator terminology
  are implemented in `beacon_app.py`.
- Project behavior and architectural constraints are documented in
  `PROJECT_GUIDE.md` and `CLAUDE.md`.
- The repository does not contain approved testimonials, customer claims, or
  external brand assets; future design work must not fabricate them.

## Product Principles

1. Surface replicable opportunities, not merely impressive numbers.
2. Explain system state in plain language and make trust visible.
3. Prioritize rapid scanning while keeping evidence close at hand.
4. Keep powerful automation bounded, reversible, and transparent.
5. Preserve one canonical source of truth across the product.

## Accessibility & Inclusion

The interface should remain legible at desktop scale, usable without relying on
color alone, keyboard-friendly where Flet permits, and clear to a non-technical
operator. Dense information should use hierarchy and progressive disclosure
rather than tiny text.
