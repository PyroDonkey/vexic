import { SignUp } from "@clerk/nextjs";

import { isClerkConfigured } from "@/lib/clerk-config";

export default function SignUpPage() {
  return (
    <main className="auth-page">
      <section className="auth-copy">
        <div className="eyebrow">Vexic Console</div>
        <h1>Create account</h1>
        <p>Organizations are the customer account boundary for projects and keys.</p>
      </section>
      {isClerkConfigured() ? <SignUp routing="path" path="/sign-up" /> : <AuthConfigNotice />}
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
