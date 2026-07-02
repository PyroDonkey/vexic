const KEY_CLASS = "text-muted-foreground";
const STRING_CLASS = "text-primary";
const NUMBER_CLASS = "text-foreground";

/**
 * A durable Tier 3 fact, field-for-field the shape `search_long_term` returns
 * (vexic.storage.longterm.LongTermFact). Rendered statically — this is
 * evidence, not decoration.
 */
export function FactArtifact() {
  return (
    <figure className="overflow-hidden rounded-xl border border-border bg-card">
      <figcaption className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="font-mono text-xs text-muted-foreground">search_long_term → fact</span>
        <span className="rounded-full border border-primary/40 px-2 py-0.5 font-mono text-[11px] text-primary">
          provenance
        </span>
      </figcaption>
      <pre className="overflow-x-auto p-4 font-mono text-xs leading-relaxed sm:p-5 sm:text-[13px]" aria-label="Example durable fact JSON with provenance fields">
        <code>
          {"{"}
          {"\n  "}
          <span className={KEY_CLASS}>&quot;fact_id&quot;</span>: <span className={NUMBER_CLASS}>184</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;fact_text&quot;</span>:{" "}
          <span className={STRING_CLASS}>&quot;Prefers uv over pip.&quot;</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;subject&quot;</span>: <span className={STRING_CLASS}>&quot;tooling&quot;</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;category&quot;</span>: <span className={STRING_CLASS}>&quot;preference&quot;</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;importance&quot;</span>: <span className={NUMBER_CLASS}>7</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;confidence&quot;</span>: <span className={NUMBER_CLASS}>0.92</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;source_message_ids&quot;</span>: [<span className={NUMBER_CLASS}>412</span>,{" "}
          <span className={NUMBER_CLASS}>418</span>, <span className={NUMBER_CLASS}>573</span>],
          {"\n  "}
          <span className={KEY_CLASS}>&quot;retrieved_count&quot;</span>: <span className={NUMBER_CLASS}>12</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;used_count&quot;</span>: <span className={NUMBER_CLASS}>4</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;editable&quot;</span>: <span className={NUMBER_CLASS}>true</span>,
          {"\n  "}
          <span className={KEY_CLASS}>&quot;created_at&quot;</span>:{" "}
          <span className={STRING_CLASS}>&quot;2026-06-14T21:08:11Z&quot;</span>
          {"\n"}
          {"}"}
        </code>
      </pre>
      <p className="border-t border-border px-4 py-3 text-xs leading-relaxed text-muted-foreground">
        <code className="font-mono">source_message_ids</code> points at the exact Tier 1 transcript
        rows this fact came from. Every recall is traceable to its receipts.
      </p>
    </figure>
  );
}
