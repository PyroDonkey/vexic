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
    title: "Local-first SQLite",
    body: "A Python core with a SQLite reference service and conformance tests. Run it on your machine today; hosted integrations are the path, not the requirement."
  },
  {
    title: "Read-only MCP",
    body: "Expose transcript and long-term search to agents over stdio or hosted HTTP MCP. Writes, export, and admin tools stay deliberately out of reach."
  }
] as const;

const INTEGRATIONS = [
  {
    name: "Claude Code",
    body: "One setup command installs a transcript recorder hook and session-start memory priming. Conversations are cleaned, ingested, and recalled automatically."
  },
  {
    name: "Codex",
    body: "Drop a TOML block into your Codex MCP config and point it at the stdio launcher. Scoped, read-only search from any Codex session."
  },
  {
    name: "MCP",
    body: "Standard Model Context Protocol, stdio or streamable HTTP. Any MCP-capable agent gets search_transcript and search_long_term — nothing more."
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
              local-first core on GitHub · hosted waitlist open
            </p>
            <h1 className="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
              Memory your agents can trust
            </h1>
            <p className="mt-5 max-w-xl text-lg leading-relaxed text-muted-foreground">
              Vexic is a provenance-first, replayable memory engine for long-running AI agents.
              Lossless transcripts in, durable facts out — every memory carrying the receipts for
              where it came from.
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
        className="bg-background-raised"
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
            <Reveal>
              <FactArtifact />
            </Reveal>
          </div>
        </div>
      </Section>

      {/* Integrations */}
      <Section title="Meets your agents where they run" className="bg-background-raised">
        <div className="grid gap-6 md:grid-cols-3">
          {INTEGRATIONS.map((integration, index) => (
            <Reveal key={integration.name} delay={index * 0.12}>
              <div className="h-full rounded-xl border border-border bg-card p-6">
                <h3 className="mb-2 font-mono text-sm text-primary">{integration.name}</h3>
                <p className="text-sm leading-relaxed text-muted-foreground">{integration.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </Section>

      {/* Quickstart */}
      <Section
        title="Running locally in one command"
        lede="Point the read-only MCP server at a local SQLite database and your agent has scoped, provenance-backed recall."
      >
        <Reveal>
          <div className="mx-auto max-w-3xl overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              <span className="font-mono text-xs text-muted-foreground">terminal</span>
            </div>
            <pre className="overflow-x-auto p-4 font-mono text-xs leading-relaxed sm:p-5 sm:text-sm">
              <code>
                <span className="text-muted-foreground"># Vexic as a read-only MCP server</span>
                {"\n"}
                <span className="text-primary">claude</span> mcp add --scope local vexic -- \{"\n"}
                {"  "}uv run python \{"\n"}
                {"  "}scripts/vexic-mcp-stdio.py \{"\n"}
                {"  "}--db-path ./memory.db \{"\n"}
                {"  "}--tenant-id local \{"\n"}
                {"  "}--session-id default
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

      {/* Final CTA: the one committed-color moment on the page. The local
          --muted-foreground override keeps secondary text hued on the emerald
          surface instead of washed-out gray. */}
      <Section className="bg-background-raised">
        <Reveal>
          <div className="mx-auto max-w-3xl rounded-2xl border border-primary/35 bg-primary-surface px-6 py-14 text-center [--muted-foreground:var(--primary-surface-muted)]">
            <h2 className="text-2xl font-semibold tracking-tight text-balance sm:text-4xl">
              Give your agents a memory worth trusting
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-primary-surface-muted">
              Join the waitlist for early access to hosted Vexic, or start with the local-first core
              on GitHub today.
            </p>
            <div className="mt-8 flex flex-col items-center gap-4">
              <WaitlistForm source="footer-cta" />
              <a
                href={GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-primary-surface-muted transition-colors hover:text-foreground"
              >
                Explore the source on GitHub →
              </a>
            </div>
          </div>
        </Reveal>
      </Section>
    </>
  );
}
