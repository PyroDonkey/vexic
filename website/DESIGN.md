# Design System: Vexic — Agent Memory Engine

Source of truth for the marketing site (`website/`). Tokens live in
`app/globals.css`; this doc records the decisions behind them.

## 1. Visual Theme & Atmosphere

A cockpit-dense, technical spec-sheet interface for a developer infrastructure
product. Atmosphere is clinical calm-power: sharp, low-latency, authoritative —
the visual equivalent of a well-instrumented terminal, not a consumer SaaS
marketing page. Density 6, Variance 7 (asymmetric split hero, offset two-column
sections), Motion 5 (purposeful micro-interactions, no cinematic choreography).

Voice: sell the hosted memory service. Copy leads with the managed endpoint
(`https://api.vexic.dev/mcp`); the open core is a credibility signal
(small "Read the source on GitHub" links), never the pitch. No stdio/local-first
framing in marketing copy.

## 2. Color Palette & Roles

Exact hexes — tokens must match these values, not approximations:

- **Off-Black Canvas `#131313`** — the one unified page background. Every
  section sits on it; sections are separated by spacing and structural lines,
  never by alternating background bands. Never pure `#000000`.
- **Raised Panel `#1c1b1b`** — fill for data artifacts only: cards, code
  blocks, terminal panels, the JSON fact artifact, the hero machine.
- **Structural Line `#2a2a2a`** — 1px borders and row dividers. Exception:
  form-field boundaries use a lighter neutral (`oklch(0.93 0.005 162.48 / 45%)`)
  because `#2a2a2a` fails the WCAG 1.4.11 ≥3:1 non-text minimum.
- **Bone White `#e5e2e1`** — primary text, headlines. Not near-white.
- **Muted Sage `#9aa89e`** — secondary text, descriptions, metadata
  (desaturated green cast, not flat gray). 7.5:1 on canvas, 6.9:1 on panels.
- **Signal Emerald `#10b981`** — single accent. Button fills (with `#131313`
  text), active states, focus rings, status dots, mono text accents,
  provenance lines. No secondary accent anywhere; no lighter mint tint —
  one emerald, used at full value or as alpha tints (`primary/10`,
  `primary/25`, `primary/35`) for borders and washes.
- Destructive red (`oklch(0.704 0.191 22.216)`) exists for form errors only;
  it is a functional state color, not an accent.

The final-CTA panel is the page's one section-level color moment: a dark
panel with a subtle emerald gradient wash (`from-card to-primary/10`,
`border-primary/25`) — never a flat colored surface.

(Max 1 accent. No purple/neon. No warm/cool gray drift — one neutral
temperature throughout; sage carries the only tint besides emerald.)

## 3. Typography

- **Display/Headline: Geist** — `tracking-tight`, weight 600, `text-balance`.
  Section headings `text-3xl sm:text-4xl`; hero `text-4xl sm:text-5xl`.
- **Body: Geist** — `leading-relaxed` (1.625), max ~65ch (`max-w-prose` /
  `max-w-xl`), `text-pretty` on ledes.
- **Mono: Geist Mono** — eyebrow/status labels, nav links, button labels,
  input text and placeholders, code blocks, JSON artifacts, integration
  names, and all numeric/data values (confidence scores, message ids).
- **Wordmark**: plain text "Vexic" (`text-lg font-semibold tracking-tight`)
  in nav and footer. No SVG/image logo.
- Banned: Inter, any serif. Sans + mono only.

## 4. Component Stylings

- **Buttons (primary)**: flat `#10b981` fill, `#131313` text, mono
  `text-sm font-semibold` label, `rounded-md`. Hover `brightness-110`;
  active `translate-y-px` + `brightness-95` (tactile push). No outer glow.
  Secondary/ghost: `border-border` outline, sage text, hover to foreground.
- **Nav**: sticky, `bg-background/80 backdrop-blur-md`, border-b. Links in
  mono `text-xs uppercase tracking-wide` with `gap-9` (36px) spacing; "Sign
  in" and the CTA stay sentence-case. Mobile menu rows ≥44px tall.
- **Cards**: data artifacts only (terminal, JSON fact, hero machine,
  integration-free). Everywhere else use `border-t border-border` divider
  rows. Exception: the how-it-works steps render as a 3-card row because
  they are a real numbered pipeline sequence (01 record → 02 stage →
  03 promote), not a feature grid.
- **Inputs**: full box always — `rounded-md`, `bg-background` fill (reads as
  an inset well on panels), lighter neutral `border-input`, mono `text-sm`,
  sage placeholder. Focus: 2px emerald ring, no outline. Error text below
  the field via a persistent `aria-live` region, never inline-blocking.
  Single-field forms use an sr-only label with placeholder as visual label.
- **Loaders**: text-state buttons ("Joining…"), skeletal shimmer if ever
  needed. No circular spinners.
- **Empty/Status states**: composed states with real copy (pricing
  waitlist-gated page is the reference), never a bare "No data".

## 5. Layout Principles

- One `#131313` canvas; rhythm comes from spacing (`py-20 sm:py-28`) and
  structural lines, not background bands.
- Hero: asymmetric split (copy left, hero machine right,
  `10fr / 11fr` grid) — never centered.
- Vary section grammar: problems = centered heading + divider rows;
  features = list-with-sidebar (sticky JSON artifact); integrations =
  offset two-column (heading hangs left, divider rows right); quickstart =
  centered heading + single terminal artifact.
- Content contained at `max-w-6xl` (1152px), centered; inner content
  narrows per section (`max-w-3xl` / `max-w-5xl`).
- No overlapping elements — every block owns its spatial zone.

## 6. Motion & Interaction

- Easing: `--ease-out-quart` (`cubic-bezier(0.25, 1, 0.5, 1)`) tweens
  throughout. No linear easing, no bounce/elastic.
- Animate transform and opacity only (color transitions on hover are the
  paint-level exception). Never layout properties. The hero machine's
  promotion glow animates the opacity of a static token-derived shadow, not
  `box-shadow` itself — the one sanctioned glow on the site.
- **No infinite loops.** The hero machine runs its pipeline three times and
  parks on the completed state; the flow-spine comet sweeps three times and
  rests; the status dot pulses only while the machine runs. Compositor goes
  idle once the mechanism has been demonstrated.
- Scroll reveals enhance an already-visible default (hidden state applied
  from JS only): rise for list rows, fade-in-place for artifacts; ~100ms
  stagger for stacked rows.
- Every animation collapses under `prefers-reduced-motion` (global 0.01ms
  override + `useReducedMotion` in motion components).

## 7. Anti-Patterns (Banned)

- No emojis anywhere.
- No Inter, no serif fonts.
- No pure black `#000000`; canvas is `#131313`.
- No section background banding — one canvas color.
- No mint/lightened accent variants — one emerald.
- No neon/outer-glow shadows (single exception: hero machine promotion
  moment, opacity-animated, 10% alpha).
- No gradient-text headlines.
- No image/SVG logos — text wordmark only.
- No custom mouse cursors.
- No overlapping text/image elements.
- No 3-column equal-card feature rows (numbered process sequences exempt).
- No centered hero section.
- No filler UI copy: "Scroll to explore," bouncing chevrons.
- No AI copywriting clichés: "Elevate," "Seamless," "Unleash," "Next-Gen,"
  "Supercharge."
- No fake endpoints, round numbers, or placeholder names — commands and
  URLs on the site must exist (`api.vexic.dev/mcp` is real).
- No stdio/local-first framing in marketing copy — hosted service leads;
  the open core appears only as a credibility link.
