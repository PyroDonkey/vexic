"use client";

import { motion, useInView, useReducedMotion } from "motion/react";
import { useRef } from "react";

import { Reveal } from "@/components/reveal";

const STEPS = [
  {
    tag: "01 · record",
    title: "Lossless transcript",
    body: "Cleaned conversation rows land in an append-only Tier 1 store. Rows are never updated or deleted. It's the source of truth every other layer replays from.",
    detail: "tier 1 · messages · append-only"
  },
  {
    tag: "02 · stage",
    title: "Extraction pipeline",
    body: "Light extraction stages candidate memories in Tier 2, a REM pass clusters and reinforces what repeats, and deep review promotes or supersedes them. A memory only becomes durable after it passes review.",
    detail: "tier 2 · light → rem → deep"
  },
  {
    tag: "03 · promote",
    title: "Durable facts, with receipts",
    body: "Every promoted Tier 3 fact carries provenance, confidence, and category, and points back to the exact transcript rows it came from. You can audit any recall, test it, and roll it back.",
    detail: "tier 3 · provenance · confidence · category"
  }
] as const;

export function HowItWorks() {
  // useReducedMotion() is null during SSR, so the element tree must never
  // branch on it — that would hydrate a different tree for reduced-motion
  // users. One motion tree; only transition/animate values vary.
  const reduceMotion = useReducedMotion();
  const spineRef = useRef<HTMLDivElement>(null);
  // Gates the infinite comet loop on real visibility: a display:none wrapper
  // (mobile) or an offscreen spine never intersects, so the loop stays idle
  // instead of ticking rAF forever for an invisible SVG.
  const spineInView = useInView(spineRef);
  const cometActive = spineInView && !reduceMotion;

  return (
    <div>
      {/* Flow spine: entrance collapses to duration 0 under reduced motion; the comet stays parked. */}
      <div ref={spineRef} className="relative mx-auto mb-12 hidden h-20 max-w-4xl md:block" aria-hidden>
        <svg viewBox="0 0 800 60" className="h-full w-full" fill="none">
          <motion.path
            d="M 20 30 H 780"
            className="stroke-[var(--border)]"
            strokeWidth="2"
            initial={{ pathLength: 0 }}
            whileInView={{ pathLength: 1 }}
            viewport={{ once: true }}
            transition={reduceMotion ? { duration: 0 } : { duration: 1.4, ease: "easeInOut" }}
          />
          {[20, 400, 780].map((x, index) => (
            <motion.circle
              key={x}
              cx={x}
              cy="30"
              r="6"
              className="fill-[var(--primary)]"
              initial={{ scale: 0, opacity: 0 }}
              whileInView={{ scale: 1, opacity: 1 }}
              viewport={{ once: true }}
              transition={reduceMotion ? { duration: 0 } : { delay: 0.3 + index * 0.55, duration: 0.35 }}
            />
          ))}
          {/* Three sweeps then rest (ends on the opacity-0 keyframe) so the
              compositor goes idle once the flow has been demonstrated. */}
          <motion.circle
            r="4"
            cy="30"
            className="fill-[var(--primary)]"
            initial={false}
            animate={cometActive ? { cx: [20, 780], opacity: [0, 1, 1, 0] } : { cx: 20, opacity: 0 }}
            transition={
              cometActive
                ? { duration: 3.2, repeat: 2, ease: "easeInOut", delay: 1.6 }
                : { duration: 0 }
            }
          />
        </svg>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        {STEPS.map((step, index) => (
          <Reveal key={step.tag} delay={index * 0.15}>
            <article className="h-full rounded-xl border border-border bg-card p-6">
              <p className="mb-3 font-mono text-xs text-primary">{step.tag}</p>
              <h3 className="mb-2 text-lg font-semibold">{step.title}</h3>
              <p className="text-sm leading-relaxed text-muted-foreground">{step.body}</p>
              <p className="mt-4 rounded-md bg-background px-3 py-2 font-mono text-xs text-muted-foreground">
                {step.detail}
              </p>
            </article>
          </Reveal>
        ))}
      </div>
    </div>
  );
}
