import { createClient, type Client } from "@libsql/client";

// Durable waitlist store backed by Turso/libSQL. The route treats an
// unconfigured store differently per environment: dev logs and accepts,
// production refuses rather than pretending to save.
let client: Client | null = null;
let schemaReady: Promise<unknown> | null = null;

function getClient(): Client | null {
  const url = process.env.TURSO_DATABASE_URL;
  if (!url) return null;
  client ??= createClient({ url, authToken: process.env.TURSO_AUTH_TOKEN });
  return client;
}

function ensureSchema(db: Client): Promise<unknown> {
  schemaReady ??= db
    .execute(
      `CREATE TABLE IF NOT EXISTS waitlist_signups (
        email TEXT PRIMARY KEY,
        source TEXT NOT NULL DEFAULT 'unknown',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
      )`
    )
    .catch((error: unknown) => {
      // Allow a later request to retry schema creation after a transient failure.
      schemaReady = null;
      throw error;
    });
  return schemaReady;
}

export type SaveResult =
  | { ok: true }
  | { ok: false; reason: "unconfigured" | "storage_error" };

export async function saveWaitlistSignup(email: string, source: string): Promise<SaveResult> {
  let db: Client | null;
  try {
    db = getClient();
  } catch (error) {
    // A malformed TURSO_DATABASE_URL makes createClient throw synchronously;
    // that is a broken configuration, not an unconfigured store, and must not
    // escape the SaveResult contract as an unhandled 500.
    console.error("[waitlist] store misconfigured:", error);
    return { ok: false, reason: "storage_error" };
  }
  if (!db) return { ok: false, reason: "unconfigured" };

  try {
    await ensureSchema(db);
    // Duplicate signups are idempotent successes, not errors.
    await db.execute({
      sql: "INSERT INTO waitlist_signups (email, source) VALUES (?, ?) ON CONFLICT(email) DO NOTHING",
      args: [email, source]
    });
    return { ok: true };
  } catch (error) {
    console.error("[waitlist] store error:", error);
    return { ok: false, reason: "storage_error" };
  }
}
