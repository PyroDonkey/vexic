import { SignIn } from "@clerk/nextjs";

import { isClerkConfigured } from "@/lib/clerk-config";

export default function SignInPage() {
  return (
    <main className="auth-page">
      <section className="auth-copy">
        <div className="eyebrow">Vexic Console</div>
        <h1>Sign in</h1>
        <p>Authenticated access is required for console routes.</p>
      </section>
      {isClerkConfigured() ? <SignIn routing="path" path="/sign-in" /> : <AuthConfigNotice />}
    </main>
  );
}

function AuthConfigNotice() {
  return (
    <section className="notice">
      <h2>Clerk environment missing</h2>
      <p>Add the values from `.env.example` before using hosted auth locally.</p>
    </section>
  );
}
