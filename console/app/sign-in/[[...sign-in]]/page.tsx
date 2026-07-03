import { SignIn } from "@clerk/nextjs";
import { dark } from "@clerk/themes";

import { AmbientCanvas } from "@/components/ambient-canvas";
import { isClerkConfigured } from "@/lib/clerk-config";

// Marketing site root; the sign-in page borrows its waitlist for
// "get notified" requests instead of exposing self-serve sign-up.
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vexic.dev";

// Mirrors the marketing site palette (website/DESIGN.md) regardless of the
// console's resolved light/dark theme, since visitors arrive from vexic.dev.
const signInAppearance = {
  baseTheme: dark,
  variables: {
    colorPrimary: "#10b981",
    colorPrimaryForeground: "#131313",
    colorBackground: "#1c1b1b",
    colorForeground: "#e5e2e1",
    colorMutedForeground: "#9aa89e",
    colorInput: "#131313",
    colorInputForeground: "#e5e2e1",
    borderRadius: "0.5rem",
    fontFamily: "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif"
  },
  elements: {
    cardBox: { border: "1px solid #2a2a2a", boxShadow: "none" },
    formButtonPrimary: {
      boxShadow: "none",
      fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
      fontWeight: 600
    },
    // Access is waitlist-gated for now; the sign-up prompt row is hidden and
    // replaced by the notify link rendered below the panel.
    footerAction: { display: "none" }
  }
};

export default function SignInPage() {
  return (
    <main className="auth-page">
      {/* Same lattice as the vexic.dev hero; content layers above via .auth-content. */}
      <AmbientCanvas
        color="#10b981"
        maxOpacity={0.5}
        speed={1}
        density={1}
        fadeDirection="to-bottom"
        className="mix-blend-screen"
      />
      <div className="auth-content">
        <p className="auth-wordmark">
          Vexic <span className="auth-wordmark-product">Console</span>
        </p>
        {isClerkConfigured() ? (
          <SignIn routing="path" path="/sign-in" appearance={signInAppearance} />
        ) : (
          <AuthConfigNotice />
        )}
        <p className="auth-notify">
          Don&apos;t have an account yet?{" "}
          <a className="auth-notify-link" href={`${SITE_URL}/#waitlist`}>
            Get notified when access opens →
          </a>
        </p>
      </div>
    </main>
  );
}

function AuthConfigNotice() {
  return (
    <section className="auth-notice">
      <h2>Clerk environment missing</h2>
      <p>
        Add the values from <code>.env.example</code> before using hosted auth locally.
      </p>
    </section>
  );
}
