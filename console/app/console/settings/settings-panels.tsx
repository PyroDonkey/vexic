"use client";

import { OrganizationProfile, UserProfile } from "@clerk/nextjs";

export default function SettingsPanels() {
  return (
    <>
      <header className="page-title">
        <div>
          <div className="eyebrow">Settings</div>
          <h1>Account and organization</h1>
          <p className="muted">Clerk owns human identity and organization membership.</p>
        </div>
      </header>
      <section className="grid">
        <div className="panel">
          <h2>User</h2>
          <UserProfile routing="hash" />
        </div>
        <div className="panel">
          <h2>Organization</h2>
          <OrganizationProfile routing="hash" />
        </div>
      </section>
    </>
  );
}
