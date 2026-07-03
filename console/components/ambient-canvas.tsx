"use client";

// Copied verbatim from website/components/ambient-canvas.tsx so the sign-in
// page matches the vexic.dev hero backdrop. Keep the two in sync.

import { useEffect, useRef } from "react";

type FadeDirection = "to-bottom" | "to-top" | "none";

/* Edge the pattern fades out toward, as a CSS mask (webkit prefix applied in JSX). */
const MASKS: Record<Exclude<FadeDirection, "none">, string> = {
  "to-bottom": "linear-gradient(to bottom, black 25%, transparent)",
  "to-top": "linear-gradient(to top, black 25%, transparent)"
};

const BASE_SPACING = 26; // px between lattice dots at density 1
const DOT_SIZE = 2.5; // dot edge in CSS px; square dots read as data, not decor
const DRIFT_X = 7; // lattice drift in px/s at speed 1
const DRIFT_Y = 3.5;
const START_T = 40; // nonzero time origin so the first (or only) frame isn't uniform

/** Canvas fillStyle can't parse var() or color-mix(); resolve through computed style. */
function resolveColor(el: HTMLElement, color: string): string {
  const previous = el.style.color;
  el.style.color = color;
  const resolved = getComputedStyle(el).color;
  el.style.color = previous;
  return resolved;
}

/**
 * Ambient animated backdrop: a sparse dot lattice that drifts diagonally while
 * slow sine waves modulate per-dot brightness. Renders as an absolutely
 * positioned overlay filling its nearest positioned ancestor, which must be
 * `relative overflow-hidden`; in-flow content on top needs `relative` so it
 * paints above the canvas.
 *
 * The loop runs only while the section is on screen (IntersectionObserver),
 * the tab is visible, and the user allows motion; `prefers-reduced-motion`
 * gets a single static frame. High-DPI displays render at devicePixelRatio
 * (capped at 2), and a ResizeObserver keeps the bitmap in sync.
 */
export function AmbientCanvas({
  color = "var(--primary)",
  maxOpacity = 0.3,
  speed = 1,
  density = 1,
  fadeDirection = "none",
  className = ""
}: {
  /** Any CSS color the browser can compute, including var() and color-mix(). */
  color?: string;
  /** Peak dot alpha; the lattice floor sits at 30% of this. */
  maxOpacity?: number;
  /** Multiplier over the base drift and shimmer rate. */
  speed?: number;
  /** Multiplier over dot count; 1 ≈ one dot per 26px of section. */
  density?: number;
  fadeDirection?: FadeDirection;
  className?: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    const spacing = BASE_SPACING / Math.max(density, 0.1);
    const fill = resolveColor(canvas, color);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

    let width = 0;
    let height = 0;
    let raf = 0;
    let running = false;
    let inView = false;
    let t = START_T; // seconds of accumulated animation time
    let last = 0;

    const draw = () => {
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = fill;
      // The lattice drifts by translating the grid origin; each dot keeps a
      // stable world index (col/row) so its brightness phase travels with it
      // instead of jumping when the origin wraps.
      const startX = ((t * DRIFT_X) % spacing) - spacing;
      const startY = ((t * DRIFT_Y) % spacing) - spacing;
      for (let y = startY; y < height + spacing; y += spacing) {
        const row = Math.round((y - t * DRIFT_Y) / spacing);
        for (let x = startX; x < width + spacing; x += spacing) {
          const col = Math.round((x - t * DRIFT_X) / spacing);
          // Three offset sine fields sum to a slow traveling shimmer. Scaling
          // by /2 (not /3) and clamping lets the sum saturate, so peaks reach
          // full maxOpacity and read as signal passing through the lattice
          // rather than uniform flicker; squaring keeps the floor quiet.
          const wave =
            Math.sin(col * 0.37 + t * 0.42) +
            Math.sin(row * 0.29 - t * 0.31) +
            Math.sin((col + row) * 0.17 + t * 0.2);
          const brightness = Math.min(Math.max(wave / 2 + 0.5, 0), 1);
          ctx.globalAlpha = maxOpacity * (0.3 + 0.7 * brightness * brightness);
          ctx.fillRect(x, y, DOT_SIZE, DOT_SIZE);
        }
      }
      ctx.globalAlpha = 1;
    };

    const frame = (now: number) => {
      // Clamp dt so a resumed loop (tab switch, scroll return) continues
      // calmly instead of lurching to catch up.
      const dt = Math.min(now - last, 100) / 1000;
      last = now;
      t += dt * speed;
      draw();
      raf = requestAnimationFrame(frame);
    };

    const update = () => {
      const shouldRun = inView && !document.hidden && !reducedMotion.matches;
      if (shouldRun && !running) {
        running = true;
        last = performance.now();
        raf = requestAnimationFrame(frame);
      } else if (!shouldRun && running) {
        running = false;
        cancelAnimationFrame(raf);
      }
    };

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.max(1, Math.round(rect.width * dpr));
      canvas.height = Math.max(1, Math.round(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw(); // keep a current frame even while paused or reduced
    };

    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(canvas);
    const intersectionObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) inView = entry.isIntersecting;
      update();
    });
    intersectionObserver.observe(canvas);
    const onVisibility = () => update();
    document.addEventListener("visibilitychange", onVisibility);
    const onReducedChange = () => {
      update();
      if (reducedMotion.matches) draw();
    };
    reducedMotion.addEventListener("change", onReducedChange);
    resize();

    return () => {
      cancelAnimationFrame(raf);
      resizeObserver.disconnect();
      intersectionObserver.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      reducedMotion.removeEventListener("change", onReducedChange);
    };
  }, [color, maxOpacity, speed, density]);

  const mask = fadeDirection === "none" ? undefined : MASKS[fadeDirection];

  return (
    <canvas
      ref={ref}
      aria-hidden
      className={`pointer-events-none absolute inset-0 h-full w-full ${className}`}
      style={mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined}
    />
  );
}
