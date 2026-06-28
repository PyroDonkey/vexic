import Link from "next/link";

export default function HomePage() {
  return (
    <main className="landing">
      <section className="landing-hero">
        <div className="landing-copy">
          <img className="landing-logo" src="/vexic-logo-reversed.svg" alt="Vexic" />
          <div className="eyebrow">Provenance-first memory control plane</div>
          <h1>Vexic Console</h1>
          <p>
            A small operator surface for projects, agent credentials, aggregate usage, and metadata-only support workflows.
          </p>
          <div className="actions">
            <Link className="button" href="/console">
              Open Console
            </Link>
          </div>
        </div>
      </section>
      <section className="landing-band" aria-label="Console scope">
        <div>
          <h2>Projects</h2>
          <p>Vexic-owned control-plane records under a Clerk Organization.</p>
        </div>
        <div>
          <h2>Agent keys</h2>
          <p>Project-scoped machine credentials with one-time reveal behavior.</p>
        </div>
        <div>
          <h2>Support</h2>
          <p>Internal metadata only, without raw memory browsing surfaces.</p>
        </div>
      </section>
    </main>
  );
}
