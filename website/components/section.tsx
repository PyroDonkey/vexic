import type { ReactNode } from "react";

export function Section({
  id,
  title,
  lede,
  children,
  headingLevel = "h2",
  className = ""
}: {
  id?: string;
  title?: string;
  lede?: string;
  children: ReactNode;
  /** "h1" when the Section title is the page's top-level heading
      (pricing, coming-soon pages); "h2" within the landing page flow. */
  headingLevel?: "h1" | "h2";
  className?: string;
}) {
  const Heading = headingLevel;
  return (
    <section id={id} className={`px-5 py-20 sm:py-28 ${className}`}>
      <div className="mx-auto w-full max-w-6xl">
        {(title || lede) && (
          <div className="mx-auto mb-14 max-w-2xl text-center">
            {title && (
              <Heading className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
                {title}
              </Heading>
            )}
            {lede && (
              <p className="mt-4 text-base text-pretty text-muted-foreground sm:text-lg">{lede}</p>
            )}
          </div>
        )}
        {children}
      </div>
    </section>
  );
}
