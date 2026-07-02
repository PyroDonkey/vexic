import { HowItWorks } from "@/components/how-it-works";
import { PipelineVisual } from "@/components/pipeline-visual";
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
      {/* Hero */}
      <section className="relative overflow-hidden px-5 pt-20 pb-16 sm:pt-28">
        <div className="hero-grid absolute inset-0 -z-10" aria-hidden />
        <div
          className="absolute -top-40 left-1/2 -z-10 h-80 w-200 -translate-x-1/2 rounded-full bg-glow blur-3xl"
          aria-hidden
        />
        <div className="mx-auto grid w-full max-w-6xl items-center gap-12 lg:grid-cols-2">
          <div>
            <p className="mb-4 inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-xs text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full bg-primary" aria-hidden />
              Early access — internal alpha
            </p>
            <h1 className="text-4xl font-semibold tracking-tight text-balance sm:text-5xl lg:text-6xl">
              Memory your agents <span className="text-gradient inline-block">can trust</span>
            </h1>
            <p className="mt-5 max-w-xl text-lg leading-relaxed text-muted-foreground">
              Vexic is a provenance-first, replayable memory engine for long-running AI agents.
              Lossless transcripts in, durable facts out — every memory carrying the receipts for
              where it came from.
            </p>
            <div id="waitlist" className="mt-8 scroll-mt-24">
              <WaitlistForm source="hero" />
            </div>
          </div>
          <PipelineVisual />
        </div>
      </section>

      {/* Problem */}
      <Section
        eyebrow="The problem"
        title="Agent memory is broken by default"
        lede="Bolting a vector store onto an agent isn't memory. It's a cache with no lifecycle, no scope, and no accountability."
      >
        <div className="grid gap-6 md:grid-cols-3">
          {PROBLEMS.map((problem, index) => (
            <Reveal key={problem.title} delay={index * 0.12}>
              <div className="h-full rounded-xl border border-border bg-card p-6">
                <h3 className="mb-2 text-lg font-semibold">{problem.title}</h3>
                <p className="text-sm leading-relaxed text-muted-foreground">{problem.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </Section>

      {/* How it works */}
      <Section
        id="how-it-works"
        eyebrow="How it works"
        title="From raw conversation to durable, auditable facts"
        lede="Three tiers, one direction of trust: the transcript is canonical, candidates are staged, and only reviewed facts become long-term memory."
        className="bg-background-raised"
      >
        <HowItWorks />
      </Section>

      {/* Features */}
      <Section
        eyebrow="Features"
        title="Built like infrastructure, not a demo"
        lede="Memory behavior you can test, migrate, and debug — because every layer above the transcript is rebuildable."
      >
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((feature, index) => (
            <Reveal key={feature.title} delay={(index % 3) * 0.1}>
              <div className="group h-full rounded-xl border border-border bg-card p-6 transition-colors hover:border-primary/40">
                <h3 className="mb-2 font-semibold">{feature.title}</h3>
                <p className="text-sm leading-relaxed text-muted-foreground">{feature.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </Section>

      {/* Integrations */}
      <Section
        eyebrow="Integrations"
        title="Meets your agents where they run"
        className="bg-background-raised"
      >
        <div className="grid gap-6 md:grid-cols-3">
          {INTEGRATIONS.map((integration, index) => (
            <Reveal key={integration.name} delay={index * 0.12}>
              <div className="h-full rounded-xl border border-border bg-card p-6">
                <p className="mb-2 font-mono text-sm text-primary">{integration.name}</p>
                <p className="text-sm leading-relaxed text-muted-foreground">{integration.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </Section>

      {/* Quickstart */}
      <Section
        eyebrow="Quickstart"
        title="Running locally in one command"
        lede="Point the read-only MCP server at a local SQLite database and your agent has scoped, provenance-backed recall."
      >
        <Reveal>
          <div className="glow-ring mx-auto max-w-3xl overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              <span className="h-3 w-3 rounded-full bg-border" aria-hidden />
              <span className="h-3 w-3 rounded-full bg-border" aria-hidden />
              <span className="h-3 w-3 rounded-full bg-border" aria-hidden />
              <span className="ml-2 font-mono text-xs text-muted-foreground">terminal</span>
            </div>
            <pre className="overflow-x-auto p-5 font-mono text-sm leading-relaxed">
              <code>
                <span className="text-muted-foreground"># Add Vexic to Claude Code as a read-only MCP server</span>
                {"\n"}
                <span className="text-primary">claude</span> mcp add --scope local vexic -- \{"\n"}
                {"  "}uv run python scripts/vexic-mcp-stdio.py \{"\n"}
                {"  "}--db-path ./memory.db --tenant-id local --session-id default
              </code>
            </pre>
          </div>
          <p className="mt-4 text-center text-sm text-muted-foreground">
            Exposes <code className="rounded bg-card px-1.5 py-0.5 font-mono text-xs">search_transcript</code> and{" "}
            <code className="rounded bg-card px-1.5 py-0.5 font-mono text-xs">search_long_term</code> — writes and
            admin tools are intentionally not registered.
          </p>
        </Reveal>
      </Section>

      {/* Final CTA */}
      <Section className="bg-background-raised">
        <Reveal>
          <div className="relative mx-auto max-w-3xl overflow-hidden rounded-2xl border border-border bg-card px-6 py-14 text-center">
            <div
              className="absolute -top-24 left-1/2 h-48 w-96 -translate-x-1/2 rounded-full bg-glow blur-3xl"
              aria-hidden
            />
            <h2 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
              Give your agents a memory worth trusting
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-muted-foreground">
              Join the waitlist for early access to hosted Vexic, or start with the local-first core
              on GitHub today.
            </p>
            <div className="mt-8 flex flex-col items-center gap-4">
              <WaitlistForm source="footer-cta" />
              <a
                href={GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-muted-foreground transition-colors hover:text-foreground"
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
