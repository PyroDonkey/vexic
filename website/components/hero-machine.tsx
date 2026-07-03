"use client";

import { motion, useReducedMotion } from "motion/react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const EASE_OUT = [0.25, 1, 0.5, 1] as const;

const TRANSCRIPT = [
  { id: "msg_0184", role: "user", text: "we're moving the api to us-east-2 next sprint" },
  { id: "msg_0185", role: "agent", text: "noted, updating the deploy scripts" },
  { id: "msg_0192", role: "user", text: "hold that migration, we're staying in us-west-1" },
  { id: "msg_0193", role: "agent", text: "understood, cancelling the region move" }
] as const;

const PHASES = ["light", "rem", "deep"] as const;

/* The fact cites both the original claim and its correction — recall returns
   the corrected fact, and provenance shows exactly why. */
const SOURCE_IDS: readonly string[] = ["msg_0184", "msg_0192"];

/* Timeline: 0 reset · 1-4 intake rows · 5 source rows light up ·
   6 their curves braid toward the extraction stage · 7 tier 2 animates in ·
   8-10 extraction phases · 11 candidate staged · 12 arrow draws toward
   durable memory · 13 tier 3 animates in · 14 fact promoted (glow). */
const STEP_HIGHLIGHT = 5;
const STEP_CURVES = 6;
const STEP_TIER2 = 7;
const STEP_STAGED = 11;
const STEP_ARROW = 12;
const STEP_TIER3 = 13;
const STEP_PROMOTED = 14;
const FINAL_STEP = STEP_PROMOTED;

/* One full run, then park on the completed state: replays would wipe the
   promoted fact off the shelf mid-read (the opposite of durable memory), and
   the compositor goes idle once the mechanism has been demonstrated. */
const MAX_RUNS = 1;

/** ms to hold on each step before advancing (index = step). */
const STEP_HOLDS = [900, 520, 520, 520, 620, 700, 800, 550, 430, 430, 550, 800, 650, 600, 1300];

type FlowPaths = { curves: string[]; arrow: string | null; arrowHead: string | null };

/**
 * Hero visual: one truthful run of the pipeline, laid out as a left-to-right
 * flow. Intake rows stream in on the left, their connector curves braid into
 * the extraction phases, a candidate is staged, and a durable fact lands on
 * the right with its source rows lit. A separate recall panel then expands
 * below: the agent asks, memory answers with the corrected fact and cites
 * both sources. Rendered as real HTML text; connectors are an SVG overlay
 * measured from the DOM, desktop-only (the stacked mobile layout reads
 * top-to-bottom without them). Under prefers-reduced-motion the completed
 * state renders statically. The panels are aria-hidden with an sr-only
 * description.
 */
export function HeroMachine() {
  const reduceMotion = useReducedMotion();
  const [step, setStep] = useState(FINAL_STEP);
  const [inView, setInView] = useState(true);
  const [done, setDone] = useState(false);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const chipsRef = useRef<HTMLDivElement | null>(null);
  const deepChipRef = useRef<HTMLSpanElement | null>(null);
  const factRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const [paths, setPaths] = useState<FlowPaths>({ curves: [], arrow: null, arrowHead: null });

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), {
      threshold: 0.15
    });
    observer.observe(panel);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (reduceMotion || !inView || done) return;
    let cancelled = false;
    let timer = 0;
    let current = 0;
    let runs = 0;
    const tick = () => {
      if (cancelled) return;
      setStep(current);
      const hold = STEP_HOLDS[current] ?? 800;
      if (current >= FINAL_STEP) {
        runs += 1;
        if (runs >= MAX_RUNS) {
          setDone(true);
          return;
        }
        current = 0;
      } else {
        current += 1;
      }
      timer = window.setTimeout(tick, hold);
    };
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      setStep(FINAL_STEP);
    };
  }, [reduceMotion, inView, done]);

  /* Connector geometry, measured from the DOM. Curves start at each intake
     row's right edge and braid to a single point on the extraction stage's
     left edge; the arrow runs from the deep chip to the fact card. Skipped
     when the layout is stacked (mobile) — detected by the chips sitting
     below the intake rows instead of beside them. */
  const measure = useCallback(() => {
    const content = contentRef.current;
    const chips = chipsRef.current;
    const deep = deepChipRef.current;
    const fact = factRef.current;
    if (!content || !chips || !deep || !fact) return;
    const base = content.getBoundingClientRect();
    const chipsRect = chips.getBoundingClientRect();
    const firstRow = rowRefs.current.get(TRANSCRIPT[0].id);
    if (!firstRow) return;
    /* Side-by-side when the chips column starts right of the intake column;
       in the stacked mobile layout they share a left edge. */
    const stacked = chipsRect.left <= firstRow.getBoundingClientRect().right;
    if (stacked) {
      setPaths({ curves: [], arrow: null, arrowHead: null });
      return;
    }
    /* Curves reach to the visible chip rail (chips are centered inside the
       column, so the column's left edge would stop the braid short). */
    const deepRect = deep.getBoundingClientRect();
    const tx = deepRect.left - base.left - 10;
    const ty = chipsRect.top - base.top + chipsRect.height / 2;
    const curves: string[] = [];
    for (const row of TRANSCRIPT) {
      const node = rowRefs.current.get(row.id);
      if (!node) continue;
      const rect = node.getBoundingClientRect();
      const sx = rect.right - base.left + 4;
      const sy = rect.top - base.top + rect.height / 2;
      curves.push(`M ${sx} ${sy} C ${sx + 36} ${sy}, ${tx - 36} ${ty}, ${tx} ${ty}`);
    }
    const factRect = fact.getBoundingClientRect();
    const ax = deepRect.right - base.left + 8;
    const ay = deepRect.top - base.top + deepRect.height / 2;
    const bx = factRect.left - base.left - 8;
    const by = factRect.top - base.top + factRect.height / 2;
    /* Head is a separate path so the line can draw with pathLength and the
       head can land after it — one combined multi-subpath path dash-draws
       across both and reads as a fade instead of a draw. */
    const arrow = `M ${ax} ${ay} C ${ax + 28} ${ay}, ${bx - 28} ${by}, ${bx} ${by}`;
    const arrowHead = `M ${bx - 6.5} ${by - 4.5} L ${bx} ${by} L ${bx - 6.5} ${by + 4.5}`;
    setPaths({ curves, arrow, arrowHead });
  }, []);

  useLayoutEffect(() => {
    measure();
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, [measure]);

  const shown = (from: number) => step >= from;
  const highlighted = shown(STEP_HIGHLIGHT);
  const curvesShown = shown(STEP_CURVES);
  const promoted = shown(STEP_PROMOTED);
  const fade = (visible: boolean, y = 6) => ({
    opacity: visible ? 1 : 0,
    y: visible ? 0 : y
  });
  const spring = (delay = 0) => ({
    duration: reduceMotion ? 0 : 0.45,
    ease: EASE_OUT,
    delay: reduceMotion ? 0 : delay
  });

  return (
    <div>
      <p className="sr-only">
        Diagram: transcript messages are ingested losslessly, flow into light, REM, and deep
        extraction phases, and become durable facts. Each promoted fact links back to its exact
        source messages, including corrections that superseded earlier statements.
      </p>
      <div className="relative" aria-hidden>
        {/* Shrink-to-fit on desktop so the card hugs the flow content instead
           of leaving dead space either side of the centered grid. */}
        <div className="relative mx-auto w-full max-w-xl lg:w-fit lg:max-w-none">
        {/* Promotion glow: a static token-derived shadow whose opacity is
           animated (composited) instead of animating box-shadow (paint). */}
        <motion.div
          className="pointer-events-none absolute -inset-px rounded-xl"
          style={{
            boxShadow:
              "0 0 0 1px color-mix(in oklab, var(--primary) 28%, transparent), 0 0 44px color-mix(in oklab, var(--primary) 10%, transparent)"
          }}
          initial={false}
          animate={{ opacity: promoted ? 1 : 0 }}
          transition={{ duration: reduceMotion ? 0 : 0.8, ease: EASE_OUT }}
        />
        <div ref={panelRef} className="overflow-hidden rounded-xl border border-border bg-card">
          <div
            ref={contentRef}
            className="relative grid gap-8 p-4 sm:p-6 lg:grid-cols-[auto_auto_auto] lg:items-stretch lg:justify-center lg:gap-6"
          >
            {/* Flow connectors: intake curves braid into the extraction stage,
               the arrow hands the staged candidate to durable memory. */}
            <svg className="pointer-events-none absolute inset-0 hidden h-full w-full overflow-visible lg:block">
              {paths.curves.map((d, index) => {
                const isSource = SOURCE_IDS.includes(TRANSCRIPT[index]?.id ?? "");
                return (
                  <motion.path
                    key={d}
                    d={d}
                    fill="none"
                    strokeWidth="1.2"
                    className="stroke-[var(--primary)]"
                    initial={false}
                    animate={{
                      pathLength: curvesShown ? 1 : 0,
                      opacity: curvesShown ? (isSource ? 0.85 : 0.35) : 0
                    }}
                    transition={{
                      pathLength: {
                        duration: reduceMotion ? 0 : 0.7,
                        ease: EASE_OUT,
                        delay: reduceMotion ? 0 : index * 0.08
                      },
                      opacity: { duration: reduceMotion ? 0 : 0.5 }
                    }}
                  />
                );
              })}
              {paths.arrow && (
                <motion.path
                  key={paths.arrow}
                  d={paths.arrow}
                  fill="none"
                  strokeWidth="1.2"
                  className="stroke-[var(--primary)]"
                  initial={false}
                  animate={{ pathLength: shown(STEP_ARROW) ? 1 : 0, opacity: shown(STEP_ARROW) ? 0.8 : 0 }}
                  transition={{
                    pathLength: { duration: reduceMotion ? 0 : 0.45, ease: EASE_OUT },
                    opacity: { duration: reduceMotion ? 0 : 0.15 }
                  }}
                />
              )}
              {paths.arrowHead && (
                <motion.path
                  d={paths.arrowHead}
                  fill="none"
                  strokeWidth="1.2"
                  className="stroke-[var(--primary)]"
                  initial={false}
                  animate={{ opacity: shown(STEP_ARROW) ? 0.8 : 0 }}
                  transition={{
                    duration: reduceMotion ? 0 : 0.2,
                    delay: reduceMotion || !shown(STEP_ARROW) ? 0 : 0.38
                  }}
                />
              )}
            </svg>

            {/* Intake: canonical transcript rows */}
            <div className="min-w-0">
              <p className="mb-2.5 font-mono text-xs tracking-wide text-muted-foreground lg:text-center">
                Transcript
              </p>
              <div className="flex flex-col gap-2">
                {TRANSCRIPT.map((row, index) => {
                  const active = highlighted && SOURCE_IDS.includes(row.id);
                  return (
                    <motion.div
                      key={row.id}
                      ref={(node) => {
                        if (node) rowRefs.current.set(row.id, node);
                      }}
                      className={`rounded-lg border px-3 py-2 font-mono text-[11px] leading-5 transition-colors duration-500 sm:text-xs ${
                        active ? "border-primary/40 bg-primary/10" : "border-border bg-background/60"
                      }`}
                      initial={false}
                      animate={fade(shown(index + 1))}
                      transition={spring()}
                    >
                      <p className="flex gap-3">
                        <span
                          className={`transition-colors duration-500 ${
                            active ? "text-primary" : "text-muted-foreground"
                          }`}
                        >
                          {row.id}
                        </span>
                        <span className="text-muted-foreground">{row.role}</span>
                      </p>
                      <p className="mt-0.5 text-foreground/90">{row.text}</p>
                    </motion.div>
                  );
                })}
              </div>
            </div>

            {/* Extraction: staged in the middle of the flow. The zone fades
               in only after the intake curves have reached it — the machine
               builds strictly left to right. */}
            <motion.div
              className="lg:flex lg:flex-col lg:px-1"
              initial={false}
              animate={fade(shown(STEP_TIER2), 4)}
              transition={spring()}
            >
              <p className="mb-2.5 font-mono text-xs tracking-wide text-muted-foreground lg:text-center">
                Fact Extraction
              </p>
              {/* Horizontal row on mobile, vertical rail on desktop — the
                 stacked chips keep the middle column narrow so the intake
                 and fact columns get the width. */}
              <div className="lg:my-auto">
              <div
                ref={chipsRef}
                className="flex flex-wrap items-center gap-2 lg:flex-col lg:gap-1.5"
              >
                {PHASES.map((phase, index) => {
                  const active = shown(8 + index);
                  return (
                    <span key={phase} className="flex items-center gap-2 lg:flex-col lg:gap-1.5">
                      {index > 0 && (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          <span className="lg:hidden">→</span>
                          <span className="hidden lg:inline">↓</span>
                        </span>
                      )}
                      <span
                        ref={phase === "deep" ? deepChipRef : undefined}
                        className={`rounded-md border px-2.5 py-1 font-mono text-[11px] transition-colors duration-300 sm:text-xs ${
                          active ? "border-primary/60 text-primary" : "border-border text-muted-foreground"
                        }`}
                      >
                        {phase}
                      </span>
                    </span>
                  );
                })}
              </div>
              <motion.p
                className="mt-2.5 font-mono text-[10px] text-muted-foreground sm:text-[11px] lg:mx-auto lg:max-w-[9.5rem] lg:text-center"
                initial={false}
                animate={fade(shown(STEP_STAGED), 4)}
                transition={spring()}
              >
                candidate staged confidence 0.96
              </motion.p>
              </div>
            </motion.div>

            {/* Durable memory: the zone appears once the arrow has drawn
               (awaiting promotion), then the promoted fact lands in it. */}
            <motion.div
              className="min-w-0 lg:flex lg:flex-col"
              initial={false}
              animate={fade(shown(STEP_TIER3), 4)}
              transition={spring()}
            >
              <p className="mb-2.5 font-mono text-xs tracking-wide text-muted-foreground lg:text-center">
                Durable Facts
              </p>
              <div className="relative lg:my-auto lg:max-w-xs">
                <motion.div
                  className="absolute inset-0 flex items-center justify-center rounded-lg border border-dashed border-border font-mono text-[10px] text-muted-foreground"
                  initial={false}
                  animate={{ opacity: promoted ? 0 : 1 }}
                  transition={{ duration: reduceMotion ? 0 : 0.3 }}
                >
                  awaiting promotion
                </motion.div>
                <motion.div
                  ref={factRef}
                  className="rounded-lg border border-primary/35 bg-background px-3.5 py-3 font-mono text-[11px] leading-5 sm:text-xs"
                  initial={false}
                  animate={{ ...fade(promoted, 8), scale: promoted ? 1 : 0.99 }}
                  transition={spring()}
                >
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="text-muted-foreground">fact_7c21</span>
                    <span className="shrink-0 text-primary">promoted ✓</span>
                  </div>
                  <p className="mt-1 text-foreground">
                    deploy region is us-west-1, the us-east-2 migration was cancelled
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2 text-muted-foreground">
                    <span>sources</span>
                    {SOURCE_IDS.map((id) => (
                      <span key={id} className="text-primary">
                        {id}
                      </span>
                    ))}
                    <span>category infra · confidence 0.96</span>
                  </div>
                </motion.div>
              </div>
            </motion.div>
          </div>
        </div>
        </div>
      </div>
    </div>
  );
}
