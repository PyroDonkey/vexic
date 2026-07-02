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

/* Timeline: 0 reset · 1-4 transcript rows · 5-7 extraction phases ·
   8 candidate staged · 9 fact promoted + provenance drawn. */
const STEP_STAGED = 8;
const STEP_PROMOTED = 9;
const FINAL_STEP = STEP_PROMOTED;

/* Loop a few times, then park on the completed state so the compositor can
   go idle instead of animating the hero fold forever. */
const MAX_RUNS = 3;

/** ms to hold on each step before advancing (index = step). */
const STEP_HOLDS = [1000, 620, 620, 620, 860, 430, 430, 640, 1100, 6500];

function statusFor(step: number): string {
  if (step === 0) return "session open · listening";
  if (step <= 4) return "ingesting transcript";
  if (step <= 7) return "extraction pass running";
  if (step === STEP_STAGED) return "candidate staged";
  return "fact promoted · provenance linked";
}

/**
 * Hero visual: one truthful run of the pipeline. Transcript rows stream in,
 * the extraction phases fire, a candidate is staged, and a durable fact is
 * promoted with provenance lines drawn back to its source rows — including
 * the correction that superseded the original claim. Rendered as real HTML
 * text (wraps on any viewport); provenance connectors are an SVG overlay
 * measured from the DOM. Under prefers-reduced-motion the completed state
 * renders statically. The panel is aria-hidden with an sr-only description.
 */
export function HeroMachine() {
  const reduceMotion = useReducedMotion();
  const [step, setStep] = useState(FINAL_STEP);
  const [inView, setInView] = useState(true);
  const [done, setDone] = useState(false);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const factRef = useRef<HTMLDivElement | null>(null);
  const sourcesRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const [paths, setPaths] = useState<string[]>([]);

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

  const measure = useCallback(() => {
    const content = contentRef.current;
    const fact = factRef.current;
    const sources = sourcesRef.current;
    if (!content || !fact || !sources) return;
    const base = content.getBoundingClientRect();
    const sx = fact.getBoundingClientRect().left - base.left;
    const sourcesRect = sources.getBoundingClientRect();
    const sy = sourcesRect.top - base.top + sourcesRect.height / 2;
    const gx = 10;
    const next: string[] = [];
    for (const id of SOURCE_IDS) {
      const row = rowRefs.current.get(id);
      if (!row) continue;
      const rect = row.getBoundingClientRect();
      const ex = rect.left - base.left - 4;
      const ey = rect.top - base.top + rect.height / 2;
      next.push(
        `M ${sx} ${sy} L ${gx + 8} ${sy} Q ${gx} ${sy} ${gx} ${sy - 8} L ${gx} ${ey + 8} Q ${gx} ${ey} ${gx + 8} ${ey} L ${ex} ${ey}`
      );
    }
    setPaths(next);
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
        Diagram: transcript messages are ingested losslessly, pass through light, REM, and deep
        extraction phases, and become durable facts. Each promoted fact links back to its exact
        source messages, including corrections that superseded earlier statements.
      </p>
      <div className="relative" aria-hidden>
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
          <div className="flex items-center justify-between gap-4 border-b border-border px-4 py-2.5 font-mono text-[11px] sm:text-xs">
            <span className="flex items-center gap-2 text-muted-foreground">
              <motion.span
                className="h-1.5 w-1.5 rounded-full bg-primary"
                initial={false}
                animate={reduceMotion || !inView || done ? { opacity: 1 } : { opacity: [1, 0.3, 1] }}
                transition={
                  reduceMotion || !inView || done
                    ? { duration: 0 }
                    : { duration: 2.4, repeat: Infinity }
                }
              />
              vexic ingest · sess_a41
            </span>
            <motion.span
              key={statusFor(step)}
              className="text-muted-foreground"
              initial={false}
              animate={{ opacity: 1 }}
              transition={{ duration: reduceMotion ? 0 : 0.3 }}
            >
              {statusFor(step)}
            </motion.span>
          </div>

          <div ref={contentRef} className="relative flex flex-col gap-5 p-4 pl-8 sm:p-5 sm:pl-9">
            {/* Provenance connectors, measured from the DOM */}
            <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
              {paths.map((d, index) => (
                <motion.path
                  key={d}
                  d={d}
                  fill="none"
                  strokeWidth="1.2"
                  className="stroke-[var(--primary)]"
                  initial={false}
                  animate={{ pathLength: promoted ? 1 : 0, opacity: promoted ? 0.75 : 0 }}
                  transition={{
                    pathLength: { duration: reduceMotion ? 0 : 0.9, ease: EASE_OUT, delay: reduceMotion ? 0 : 0.25 + index * 0.18 },
                    opacity: { duration: reduceMotion ? 0 : 0.3, delay: reduceMotion ? 0 : promoted ? 0.25 : 0 }
                  }}
                />
              ))}
            </svg>

            {/* Tier 1: canonical transcript */}
            <div>
              <p className="mb-2 font-mono text-[10px] tracking-wide text-muted-foreground">
                tier 1 · transcript (canonical)
              </p>
              <div className="flex flex-col">
                {TRANSCRIPT.map((row, index) => {
                  const isSource = SOURCE_IDS.includes(row.id);
                  const active = promoted && isSource;
                  return (
                    <motion.div
                      key={row.id}
                      ref={(node) => {
                        if (node) rowRefs.current.set(row.id, node);
                      }}
                      className={`flex items-baseline gap-3 rounded px-2 py-1 font-mono text-[11px] leading-5 transition-colors duration-500 sm:text-xs ${
                        active ? "bg-primary/10" : "bg-transparent"
                      }`}
                      initial={false}
                      animate={fade(shown(index + 1))}
                      transition={spring()}
                    >
                      <span
                        className={`shrink-0 transition-colors duration-500 ${
                          active ? "text-primary" : "text-muted-foreground"
                        }`}
                      >
                        {row.id}
                      </span>
                      <span className="w-11 shrink-0 text-muted-foreground">{row.role}</span>
                      <span className="min-w-0 text-foreground/90">{row.text}</span>
                    </motion.div>
                  );
                })}
              </div>
            </div>

            {/* Tier 2: extraction pass */}
            <div>
              <p className="mb-2 font-mono text-[10px] tracking-wide text-muted-foreground">
                tier 2 · extraction
              </p>
              <div className="flex flex-wrap items-center gap-2 px-2">
                {PHASES.map((phase, index) => {
                  const active = shown(5 + index);
                  return (
                    <span key={phase} className="flex items-center gap-2">
                      {index > 0 && <span className="font-mono text-[10px] text-muted-foreground">→</span>}
                      <span
                        className={`rounded-md border px-2.5 py-1 font-mono text-[11px] transition-colors duration-300 sm:text-xs ${
                          active ? "border-primary/60 text-primary" : "border-border text-muted-foreground"
                        }`}
                      >
                        {phase}
                      </span>
                    </span>
                  );
                })}
                <motion.span
                  className="ml-1 font-mono text-[11px] text-muted-foreground sm:text-xs"
                  initial={false}
                  animate={fade(shown(STEP_STAGED), 4)}
                  transition={spring()}
                >
                  candidate staged · confidence 0.96
                </motion.span>
              </div>
            </div>

            {/* Tier 3: durable fact with provenance */}
            <div>
              <p className="mb-2 font-mono text-[10px] tracking-wide text-muted-foreground">
                tier 3 · durable facts
              </p>
              <div className="relative">
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
                  <div ref={sourcesRef} className="mt-1.5 flex flex-wrap items-baseline gap-x-2 text-muted-foreground">
                    <span>sources</span>
                    {SOURCE_IDS.map((id) => (
                      <span key={id} className="text-primary">
                        {id}
                      </span>
                    ))}
                    <span>· category infra · confidence 0.96</span>
                  </div>
                </motion.div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
