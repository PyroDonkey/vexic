"use client";

import { motion } from "motion/react";

const LOOP = { duration: 6, repeat: Infinity, ease: "easeInOut" as const };

const TRANSCRIPT_ROWS = [46, 78, 110, 142];
const FACT_ROWS = [70, 118];

/**
 * Looping hero visual: transcript rows feed the extraction phases, which emit
 * durable facts with a provenance link back to their source rows.
 */
export function PipelineVisual() {
  return (
    <div className="glow-ring relative overflow-hidden rounded-xl border border-border bg-card p-4">
      <svg
        viewBox="0 0 640 220"
        role="img"
        aria-label="Animated diagram: transcript rows flow through Light, REM, and Deep extraction phases and become durable facts with provenance"
        className="h-auto w-full"
      >
        {/* Column headers */}
        <text x="88" y="24" textAnchor="middle" className="fill-[var(--muted-foreground)] font-mono" fontSize="11">
          transcript · tier 1
        </text>
        <text x="320" y="24" textAnchor="middle" className="fill-[var(--muted-foreground)] font-mono" fontSize="11">
          extraction
        </text>
        <text x="552" y="24" textAnchor="middle" className="fill-[var(--muted-foreground)] font-mono" fontSize="11">
          durable facts
        </text>

        {/* Transcript rows (lossless, append-only) */}
        {TRANSCRIPT_ROWS.map((y, index) => (
          <g key={y}>
            <rect x="24" y={y} width="128" height="22" rx="5" className="fill-[var(--muted)]" />
            <motion.rect
              x="24"
              y={y}
              width="128"
              height="22"
              rx="5"
              className="fill-[var(--primary)]"
              initial={{ opacity: 0 }}
              animate={{ opacity: [0, 0.35, 0] }}
              transition={{ ...LOOP, delay: index * 0.4 }}
            />
            <rect x="34" y={y + 8} width={70 - index * 8} height="6" rx="3" className="fill-[var(--border)]" />
          </g>
        ))}

        {/* Flow: transcript -> phases */}
        <path d="M 152 105 C 200 105, 210 110, 252 110" className="stroke-[var(--border)]" strokeWidth="1.5" fill="none" />
        <motion.circle
          r="4"
          className="fill-[var(--primary)]"
          initial={false}
          animate={{ cx: [152, 252], cy: [105, 110], opacity: [0, 1, 0] }}
          transition={{ ...LOOP, times: [0, 0.5, 1] }}
        />

        {/* Extraction phases */}
        {[
          { label: "light", y: 52 },
          { label: "rem", y: 99 },
          { label: "deep", y: 146 }
        ].map((phase, index) => (
          <g key={phase.label}>
            <motion.rect
              x="252"
              y={phase.y}
              width="136"
              height="34"
              rx="8"
              className="fill-[var(--background-raised)] stroke-[var(--border)]"
              strokeWidth="1"
              initial={false}
              animate={{ stroke: ["var(--border)", "var(--primary)", "var(--border)"] }}
              transition={{ ...LOOP, delay: 1 + index * 0.7 }}
            />
            <text
              x="320"
              y={phase.y + 22}
              textAnchor="middle"
              className="fill-[var(--foreground)] font-mono"
              fontSize="13"
            >
              {phase.label}
            </text>
          </g>
        ))}
        {/* Phase chaining */}
        <path d="M 320 86 L 320 99 M 320 133 L 320 146" className="stroke-[var(--border)]" strokeWidth="1.5" />

        {/* Flow: phases -> facts */}
        <path d="M 388 163 C 430 163, 440 94, 484 94" className="stroke-[var(--border)]" strokeWidth="1.5" fill="none" />
        <motion.circle
          r="4"
          className="fill-[var(--primary)]"
          initial={false}
          animate={{ cx: [388, 484], cy: [163, 94], opacity: [0, 1, 0] }}
          transition={{ ...LOOP, delay: 3, times: [0, 0.5, 1] }}
        />

        {/* Durable facts with provenance badge */}
        {FACT_ROWS.map((y, index) => (
          <g key={y}>
            <motion.g
              initial={false}
              animate={{ opacity: [0.55, 1, 0.55] }}
              transition={{ ...LOOP, delay: 3.6 + index * 0.5 }}
            >
              <rect x="484" y={y - 24} width="136" height="40" rx="8" className="fill-[var(--background-raised)] stroke-[var(--border)]" strokeWidth="1" />
              <rect x="494" y={y - 14} width="76" height="6" rx="3" className="fill-[var(--foreground)]" opacity="0.7" />
              <rect x="494" y={y - 2} width="52" height="6" rx="3" className="fill-[var(--border)]" />
              <circle cx="604" cy={y - 8} r="7" className="fill-[var(--primary)]" opacity="0.25" />
              <path
                d={`M 600.5 ${y - 8} l 2.5 2.5 l 4.5 -5`}
                className="stroke-[var(--primary)]"
                strokeWidth="1.8"
                fill="none"
                strokeLinecap="round"
              />
            </motion.g>
          </g>
        ))}

        {/* Provenance link back to source rows */}
        <motion.path
          d="M 484 94 C 380 210, 220 200, 152 153"
          className="stroke-[var(--primary)]"
          strokeWidth="1"
          strokeDasharray="4 5"
          fill="none"
          initial={false}
          animate={{ opacity: [0, 0.7, 0] }}
          transition={{ ...LOOP, delay: 4.2 }}
        />
        <motion.text
          x="330"
          y="206"
          textAnchor="middle"
          className="fill-[var(--primary)] font-mono"
          fontSize="10"
          initial={false}
          animate={{ opacity: [0, 0.9, 0] }}
          transition={{ ...LOOP, delay: 4.2 }}
        >
          source_message_ids
        </motion.text>
      </svg>
    </div>
  );
}
