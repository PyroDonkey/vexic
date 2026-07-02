"use client";

import { motion } from "motion/react";

const STEPS = [
  {
    tag: "01 · record",
    title: "Lossless transcript",
    body: "Cleaned conversation rows land in an append-only Tier 1 store. Rows are never updated or deleted — this is the source of truth every other layer replays from.",
    detail: "messages · append-only"
  },
  {
    tag: "02 · stage",
    title: "Extraction pipeline",
    body: "Light extraction stages candidate memories, REM reinforces what repeats, and Deep review promotes or supersedes. Nothing becomes durable without passing review.",
    detail: "light → rem → deep"
  },
  {
    tag: "03 · promote",
    title: "Durable facts, with receipts",
    body: "Every promoted fact carries provenance, confidence, and category — pointing back to the exact transcript rows it came from. Recall you can audit, test, and roll back.",
    detail: "provenance · confidence · category"
  }
] as const;

const reveal = {
  initial: { opacity: 0, y: 24 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true, margin: "-80px" }
} as const;

export function HowItWorks() {
  return (
    <div>
      {/* Animated flow spine */}
      <div className="relative mx-auto mb-12 hidden h-20 max-w-4xl md:block" aria-hidden>
        <svg viewBox="0 0 800 60" className="h-full w-full" fill="none">
          <motion.path
            d="M 20 30 H 780"
            className="stroke-[var(--border)]"
            strokeWidth="2"
            initial={{ pathLength: 0 }}
            whileInView={{ pathLength: 1 }}
            viewport={{ once: true }}
            transition={{ duration: 1.4, ease: "easeInOut" }}
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
              transition={{ delay: 0.3 + index * 0.55, duration: 0.35 }}
            />
          ))}
          <motion.circle
            r="4"
            cy="30"
            className="fill-[var(--primary)]"
            initial={false}
            animate={{ cx: [20, 780], opacity: [0, 1, 1, 0] }}
            transition={{ duration: 3.2, repeat: Infinity, ease: "easeInOut", delay: 1.6 }}
          />
        </svg>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        {STEPS.map((step, index) => (
          <motion.article
            key={step.tag}
            {...reveal}
            transition={{ duration: 0.5, delay: index * 0.15 }}
            className="rounded-xl border border-border bg-card p-6"
          >
            <p className="mb-3 font-mono text-xs text-primary">{step.tag}</p>
            <h3 className="mb-2 text-lg font-semibold">{step.title}</h3>
            <p className="text-sm leading-relaxed text-muted-foreground">{step.body}</p>
            <p className="mt-4 rounded-md bg-background-raised px-3 py-2 font-mono text-xs text-muted-foreground">
              {step.detail}
            </p>
          </motion.article>
        ))}
      </div>
    </div>
  );
}
