"use client";

import { useEffect, useRef, useState } from "react";

import { GITHUB_URL } from "@/lib/links";

type FormState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "success" }
  | { status: "error"; message: string };

export function WaitlistForm({
  source = "hero",
  showNote = true
}: {
  source?: string;
  showNote?: boolean;
}) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<FormState>({ status: "idle" });
  const successRef = useRef<HTMLDivElement | null>(null);

  // The submit button unmounts on success; move focus to the confirmation so
  // keyboard and screen-reader users are not dropped onto <body>.
  useEffect(() => {
    if (state.status === "success") {
      successRef.current?.focus();
    }
  }, [state.status]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState({ status: "submitting" });

    try {
      const response = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, source })
      });
      const data = (await response.json()) as { ok: boolean; error?: string };

      if (!response.ok || !data.ok) {
        setState({ status: "error", message: data.error ?? "Something went wrong. Try again." });
        return;
      }
      setState({ status: "success" });
    } catch {
      setState({ status: "error", message: "Network error. Try again." });
    }
  }

  return (
    <div className="w-full max-w-md">
      {state.status === "success" ? (
        <div ref={successRef} tabIndex={-1} className="focus:outline-none">
          <div className="flex items-center gap-3 rounded-lg border border-primary/40 bg-card px-4 py-3 text-sm">
            <span aria-hidden className="text-primary">
              ✓
            </span>
            <p>
              You&apos;re on the list. We&apos;ll email{" "}
              <span className="font-semibold break-all">{email}</span> when early access opens.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              setEmail("");
              setState({ status: "idle" });
            }}
            className="mt-2 text-xs text-muted-foreground underline underline-offset-2 transition-colors hover:text-foreground"
          >
            Wrong email? Sign up with a different one
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit}>
          {/* flex-wrap instead of a breakpoint: the form drops to two rows
             whenever its container is too narrow for input + button, which
             also covers the md-range hero split column. */}
          <div className="flex flex-wrap gap-2">
            <label htmlFor={`waitlist-email-${source}`} className="sr-only">
              Email address
            </label>
            <input
              id={`waitlist-email-${source}`}
              type="email"
              required
              autoComplete="email"
              placeholder="you@company.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="h-11 min-w-[14rem] flex-1 rounded-md border border-input bg-background px-3.5 font-mono text-sm placeholder:text-muted-foreground focus:ring-2 focus:ring-ring focus:outline-none"
            />
            <button
              type="submit"
              disabled={state.status === "submitting"}
              className="h-11 w-full rounded-md bg-primary px-5 font-mono text-sm font-semibold whitespace-nowrap text-primary-foreground sm:w-auto transition-[filter,translate] hover:brightness-110 active:translate-y-px active:brightness-95 disabled:opacity-60"
            >
              {state.status === "submitting" ? "Requesting…" : "Request early access"}
            </button>
          </div>
          {showNote && (
            <p className="mt-2 text-xs text-muted-foreground">
              Early access rolls out gradually. We&apos;ll send one email when it&apos;s your turn, and nothing else.
            </p>
          )}
        </form>
      )}
      {/* Persistent live region: mounted from first render so screen readers
          announce content injected into it (regions mounted already populated
          are not announced). */}
      <div aria-live="polite">
        {state.status === "error" && (
          <p role="alert" className="mt-2 text-sm text-destructive">
            {state.message}{" "}
            <a
              href={`${GITHUB_URL}/issues`}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2 transition-colors hover:text-foreground"
            >
              Still stuck? Open a GitHub issue.
            </a>
          </p>
        )}
        {state.status === "success" && (
          <p className="sr-only">
            You&apos;re on the list. We&apos;ll email {email} when early access opens.
          </p>
        )}
      </div>
    </div>
  );
}
