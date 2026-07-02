import type { ReactNode } from "react";

export function Section({
  id,
  eyebrow,
  title,
  lede,
  children,
  className = ""
}: {
  id?: string;
  eyebrow?: string;
  title?: string;
  lede?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section id={id} className={`px-5 py-20 sm:py-28 ${className}`}>
      <div className="mx-auto w-full max-w-6xl">
        {(eyebrow || title || lede) && (
          <div className="mx-auto mb-14 max-w-2xl text-center">
            {eyebrow && (
              <p className="mb-3 text-xs font-bold tracking-widest text-primary uppercase">
                {eyebrow}
              </p>
            )}
            {title && (
              <h2 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
                {title}
              </h2>
            )}
            {lede && <p className="mt-4 text-base text-muted-foreground sm:text-lg">{lede}</p>}
          </div>
        )}
        {children}
      </div>
    </section>
  );
}
