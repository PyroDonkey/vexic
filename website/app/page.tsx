import { FactArtifact } from "@/components/fact-artifact";
import { HeroMachine } from "@/components/hero-machine";
import { HowItWorks } from "@/components/how-it-works";
import { Reveal } from "@/components/reveal";
import { Section } from "@/components/section";
import { WaitlistForm } from "@/components/waitlist-form";
import { GITHUB_URL } from "@/lib/links";

const PROBLEMS = [
  {
    title: "Agents forget",
    body: "Context windows end, sessions restart, and everything the agent learned evaporates. Long-running work needs memory that outlives the conversation."
  },
  {
    title: "Stale context poisons runs",
    body: "Naive memory keeps everything forever. Outdated facts get recalled next to fresh ones, and the agent can't tell which to trust."
  },
  {
    title: "Recall is unauditable",
    body: "When an agent asserts something from memory, most systems can't answer the only question that matters: where did that come from?"
  }
] as const;

const FEATURES = [
  {
    title: "Provenance on every fact",
    body: "Each durable memory carries source message ids, confidence, and category. Trace any recall back to the exact transcript rows it came from."
  },
  {
    title: "Replayable indexes",
    body: "FTS and vector indexes are rebuildable projections over canonical rows. Blow them away and rebuild — the transcript is the source of truth."
  },
  {
    title: "Scoped memory",
    body: "Explicit MemoryScope binds every request to tenant, project, session, and agent. Null agent scope means shared — never a wildcard."
  },
  {
    title: "Redaction guard",
    body: "A persistence and egress secret guard applies forbidden-value redaction before anything is stored or leaves the boundary."
  },
  {
    title: "Managed pipeline",
    body: "Ingestion, extraction passes, and index rebuilds run as a service. Your agents call one endpoint; durability, migrations, and reindexing are Vexic's job, not yours."
  },
  {
    title: "Read-only MCP",
    body: "Expose transcript and long-term search to agents over hosted HTTP MCP. Writes, export, and admin tools stay deliberately out of reach."
  }
] as const;

const INTEGRATIONS = [
  {
    name: "Claude Code",
    body: "One setup command installs a transcript recorder hook and session-start memory priming. Conversations are cleaned, ingested, and recalled automatically."
  },
  {
    name: "Codex",
    body: "Drop a TOML block into your Codex MCP config and point it at your Vexic endpoint. Scoped, read-only search from any Codex session."
  },
  {
    name: "MCP",
    body: "Standard Model Context Protocol over streamable HTTP. Any MCP-capable agent gets search_transcript and search_long_term — nothing more."
  }
] as const;

export default function HomePage() {
  return (
    <>
      {/* Hero: one truthful run of the pipeline is the visual; the headline serves it. */}
      <section className="px-5 pt-14 pb-20 sm:pt-20 lg:pt-24">
        <div className="mx-auto grid w-full max-w-6xl items-center gap-12 lg:grid-cols-[minmax(0,10fr)_minmax(0,11fr)] lg:gap-14">
          <div className="flex flex-col items-start">
            <p className="mb-5 flex items-center gap-2 font-mono text-xs text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full bg-primary" aria-hidden />
              hosted memory service · early access waitlist open
            </p>
            <h1 className="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
              Memory your agents can trust
            </h1>
            <p className="mt-5 max-w-xl text-lg leading-relaxed text-muted-foreground">
              Vexic is a hosted, provenance-first memory engine for long-running AI agents. Point
              your agents at one endpoint — lossless transcripts in, durable facts out, every
              memory carrying the receipts for where it came from.
            </p>
            <div id="waitlist" className="mt-8 flex w-full scroll-mt-24 flex-col items-start gap-3">
              <WaitlistForm source="hero" />
              <a
                href={GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                className="rounded-md border border-border px-4 py-2 text-sm text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
              >
                Read the source on GitHub →
              </a>
            </div>
          </div>
          <HeroMachine />
        </div>
      </section>

      {/* Problem: three statements, no cards. */}
      <Section
        title="Agent memory is broken by default"
        lede="Bolting a vector store onto an agent isn't memory. It's a cache with no lifecycle, no scope, and no accountability."
      >
        <div className="mx-auto max-w-3xl">
          {PROBLEMS.map((problem, index) => (
            <Reveal key={problem.title} delay={index * 0.1}>
              <div className="grid gap-2 border-t border-border py-7 sm:grid-cols-[14rem_1fr] sm:gap-8">
                <h3 className="font-semibold">{problem.title}</h3>
                <p className="text-sm leading-relaxed text-muted-foreground">{problem.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </Section>

      {/* How it works */}
      <Section
        id="how-it-works"
        title="From raw conversation to durable, auditable facts"
        lede="Three tiers, one direction of trust: the Tier 1 transcript is canonical, Tier 2 candidates are staged, and only reviewed facts reach the Tier 3 long-term store."
      >
        <HowItWorks />
      </Section>

      {/* Features: spec sheet beside the artifact that proves it. */}
      <Section
        title="Built like infrastructure, not a demo"
        lede="Memory behavior you can test, migrate, and debug — because every layer above the transcript is rebuildable."
      >
        <div className="mx-auto grid max-w-5xl gap-12 lg:grid-cols-[1fr_26rem] lg:gap-16">
          <div className="min-w-0">
            {FEATURES.map((feature) => (
              <div key={feature.title} className="border-t border-border py-6 first:border-t-0 first:pt-0">
                <h3 className="mb-1.5 font-semibold">{feature.title}</h3>
                <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">{feature.body}</p>
              </div>
            ))}
          </div>
          <div className="min-w-0 lg:sticky lg:top-24 lg:self-start">
            <Reveal variant="fade">
              <FactArtifact />
            </Reveal>
          </div>
        </div>
      </Section>

      {/* Integrations: offset two-column — heading hangs left, list rows hang
          off structural lines on the right. No panel here on purpose: the
          terminal below stays the only artifact box in this stretch. */}
      <Section>
        <div className="mx-auto grid max-w-5xl gap-10 lg:grid-cols-[minmax(0,4fr)_minmax(0,7fr)] lg:gap-16">
          <div>
            <h2 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
              Meets your agents where they run
            </h2>
            <p className="mt-4 text-base text-pretty text-muted-foreground sm:text-lg">
              One hosted endpoint, spoken over the protocols your agents already use.
            </p>
          </div>
          <div>
            {INTEGRATIONS.map((integration, index) => (
              <Reveal key={integration.name} delay={index * 0.1}>
                <div className="grid gap-1.5 border-t border-border py-6 sm:grid-cols-[9rem_1fr] sm:gap-6">
                  <h3 className="font-mono text-sm text-primary">{integration.name}</h3>
                  <p className="text-sm leading-relaxed text-muted-foreground">{integration.body}</p>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </Section>

      {/* Quickstart: hosted endpoint connect — the whole setup story. */}
      <Section
        title="Connected in one command"
        lede="Add the hosted MCP endpoint to your agent and it has scoped, provenance-backed recall — no database to run, no indexes to babysit."
      >
        <Reveal variant="fade">
          <div className="mx-auto max-w-3xl overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              <span className="font-mono text-xs text-muted-foreground">terminal</span>
            </div>
            <pre className="overflow-x-auto p-4 font-mono text-xs leading-relaxed sm:p-5 sm:text-sm">
              <code>
                <span className="text-muted-foreground"># Vexic as a hosted MCP server</span>
                {"\n"}
                <span className="text-primary">claude</span> mcp add --transport http vexic \{"\n"}
                {"  "}https://api.vexic.dev/mcp \{"\n"}
                {"  "}--header <span className="text-primary">&quot;Authorization: Bearer $VEXIC_API_KEY&quot;</span>
              </code>
            </pre>
          </div>
          <p className="mx-auto mt-4 max-w-2xl text-center text-sm text-muted-foreground">
            Exposes <code className="rounded bg-card px-1.5 py-0.5 font-mono text-xs">search_transcript</code> and{" "}
            <code className="rounded bg-card px-1.5 py-0.5 font-mono text-xs">search_long_term</code> — writes and
            admin tools are intentionally not registered.
          </p>
        </Reveal>
      </Section>

      {/* Final CTA: dark panel with a subtle emerald wash — the color moment
          comes from the tinted gradient and the mint capture button, not a
          flat colored surface. */}
      <Section>
        <Reveal>
          <div className="mx-auto max-w-3xl rounded-2xl border border-primary/25 bg-gradient-to-br from-card to-primary/10 px-6 py-14 text-center">
            <h2 className="text-2xl font-semibold tracking-tight text-balance sm:text-4xl">
              Give your agents a memory worth trusting
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-muted-foreground">
              Hosted Vexic is rolling out through the waitlist — managed ingestion, extraction,
              and recall behind one endpoint.
            </p>
            <div className="mt-8 flex flex-col items-center gap-4">
              <WaitlistForm source="footer-cta" />
              <a
                href={GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                Read the source on GitHub →
              </a>
            </div>
          </div>
        </Reveal>
      </Section>
    </>
  );
}
