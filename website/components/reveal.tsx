"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * Scroll reveal that enhances an already-visible default. The server renders
 * content fully visible; the hidden state is applied only from JS, only for
 * elements still below the viewport, and only when the user allows motion.
 * No JS, hidden tabs, and reduced-motion all get plain visible content.
 */
export function Reveal({
  children,
  delay = 0,
  className = ""
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    // The observer's initial record (fired on observe, regardless of the
    // threshold list) decides whether to hide: anything already visible —
    // even a sliver straddling the fold — is left alone, so painted content
    // never flickers out. Using the observer instead of getBoundingClientRect
    // also avoids a forced synchronous reflow per Reveal during hydration.
    let sawInitialRecord = false;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!sawInitialRecord) {
            sawInitialRecord = true;
            if (entry.isIntersecting) {
              observer.disconnect();
              return;
            }
            el.style.transitionDelay = `${delay}s`;
            el.classList.add("reveal-hidden");
            return;
          }
          if (entry.isIntersecting) {
            el.classList.add("reveal-shown");
            observer.disconnect();
          }
        }
      },
      { threshold: 0.15 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [delay]);

  return (
    <div ref={ref} className={className}>
      {children}
    </div>
  );
}
