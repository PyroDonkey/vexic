"use client";

import { useState } from "react";

type FormState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "success" }
  | { status: "error"; message: string };

export function WaitlistForm({ source = "hero" }: { source?: string }) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<FormState>({ status: "idle" });

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

  if (state.status === "success") {
    return (
      <div className="glow-ring flex items-center gap-3 rounded-lg bg-card px-4 py-3 text-sm">
        <span aria-hidden className="text-primary">
          ✓
        </span>
        <p>
          You&apos;re on the list. We&apos;ll email <span className="font-semibold">{email}</span>{" "}
          when early access opens.
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="w-full max-w-md">
      <div className="flex flex-col gap-2 sm:flex-row">
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
          className="h-11 flex-1 rounded-md border border-input bg-background-raised px-3.5 text-sm placeholder:text-muted-foreground focus:ring-2 focus:ring-ring focus:outline-none"
        />
        <button
          type="submit"
          disabled={state.status === "submitting"}
          className="h-11 rounded-md bg-primary px-5 text-sm font-semibold text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-60"
        >
          {state.status === "submitting" ? "Joining…" : "Join the waitlist"}
        </button>
      </div>
      {state.status === "error" && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {state.message}
        </p>
      )}
      <p className="mt-2 text-xs text-muted-foreground">
        Early access is rolling out gradually. No spam — one email when it&apos;s your turn.
      </p>
    </form>
  );
}
