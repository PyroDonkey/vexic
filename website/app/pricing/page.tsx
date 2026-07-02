import type { Metadata } from "next";

import { Section } from "@/components/section";
import { WaitlistForm } from "@/components/waitlist-form";

export const metadata: Metadata = {
  title: "Pricing",
  description: "Vexic pricing is being finalized. Join the waitlist to hear when it goes live."
};

export default function PricingPage() {
  return (
    <Section
      eyebrow="Pricing"
      title="Pricing lands with the hosted launch"
      lede="Hosted Vexic is in internal alpha and pricing is being finalized alongside it. Leave your email and we'll notify you the moment plans go live."
      className="min-h-[60vh]"
    >
      <div className="mx-auto flex max-w-md flex-col items-center gap-6">
        <WaitlistForm source="pricing" />
        <div className="rounded-xl border border-border bg-card p-5 text-sm text-muted-foreground">
          <p className="mb-2 font-semibold text-foreground">What to expect</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>The local-first Python core stays open on GitHub.</li>
            <li>Hosted plans will price on stored memory and retrieval volume.</li>
            <li>Waitlist members get first access and launch pricing.</li>
          </ul>
        </div>
      </div>
    </Section>
  );
}
