# Hosted dreaming runs on per-tenant provisioned provider keys

Status: accepted

## Context

Hosted dreaming (Light/REM/Deep, `run_dream_phase`) needs inference. Vexic core
does not read provider secrets: `DreamPhasePorts` carries `secrets` and an
`AgentFactory(model_group, secrets)`, and the host adapter supplies them at job
time (ADR 0007, `src/vexic/ports.py`). The core is therefore already indifferent
to *where* a provider credential comes from. The question this ADR settles is
what the hosted adapter puts in that port.

Today it puts one thing. `adapters/openrouter_live_adapter.py` reads an ambient
`OPENROUTER_API_KEY` from the environment, so every tenant's dreaming bills to a
single platform-wide OpenRouter key. That was correct for an alpha with one
tenant. It does not survive contact with a second one, for three reasons that
are worth separating because they have different severities.

**The spend cap cannot be built on it.** ADR 0006 requires per-tenant token and
dollar spend caps before hosted launch. With one shared key there is nothing to
cap against: the provider sees one caller. The natural implementation is to
enforce the cap from Vexic's own usage counters -- and those counters are not
trustworthy enough to be the only thing standing between a runaway dream loop
and the bill. `summarize_agent_usage` returns an all-zero `UsageSummary` when a
result exposes no callable `.usage`, with no exception and no log; pydantic-ai
is mid-deprecation on exactly that shape, so a routine dependency bump silently
zeroes every token counter in the system. A spend cap reading a zeroed counter
caps nothing, and the telemetry that would have revealed the breakage is the
telemetry that broke.

**Usage cannot be attributed.** Per-tenant cost is not derivable from a shared
key. Pricing (and any usage-based plan) needs a number the platform can stand
behind, independently of the same counters above.

**The blast radius is the whole platform.** A leaked key is not hypothetical
here: COA-317 rotated this exact key after it was dumped into an agent
transcript. One key means one compromise takes down every tenant's dreaming, and
one rotation interrupts every tenant.

OpenRouter's key-management API offers a direct answer. A *Management* key --
distinct from a completion key, and unable to call completion endpoints itself
-- authorizes `/api/v1/keys`. Keys can be created with a name and a credit
limit; the key string is returned once at creation. Key objects expose
`limit_remaining`, `usage`, `usage_daily`, `usage_weekly`, and `usage_monthly`,
and support an automatic limit reset on a daily/weekly/monthly schedule. Keys
can be listed, retrieved, updated, disabled without deletion, and deleted.

## Decision

**Each hosted tenant gets its own provider key, minted by the platform at tenant
provisioning time and carrying a provider-enforced credit limit. The hosted
adapter passes that key to `DreamPhasePorts.secrets`; nothing reads an ambient
per-tenant provider secret from the environment.**

The platform still pays. This is a *managed* strategy -- the tenant never sees,
supplies, or is billed for a provider credential -- and the per-tenant key is a
cost-control and isolation boundary, not a cost-shifting one.

Consequences of that framing, made explicit because they are easy to assume away:

- **The credit limit is the spend cap.** It is enforced by the provider, on the
  provider's side of the call, and it holds whether or not Vexic's usage
  counters work. This is the property that makes it worth building: ADR 0006's
  spend-cap gate stops depending on the correctness of our own telemetry. Vexic's
  counters remain useful for attribution and alerting; they are no longer the
  only thing between a bug and an unbounded bill.
- **Per-tenant usage comes from the key object**, giving an independent
  cross-check against `dream_runs` totals. Two sources that disagree is a signal;
  one source that can silently read zero is not.
- **This does not change the privacy posture.** Tenant memory text still flows to
  the platform's chosen provider under the platform's account. Vexic remains a
  data processor and the ADR 0009 consent gate still applies in full. Only a
  bring-your-own-credential strategy would move that, and this ADR does not adopt
  one.

**BYO is deferred, not rejected.** The plumbing this decision requires -- a
per-tenant key threaded through `DreamPhasePorts.secrets` -- is the same plumbing
BYO needs. Adopting BYO later becomes a question of where the key comes from, not
new machinery. It stays deferred because it requires per-tenant provider-secret
storage (ADR 0008 posture, unbuilt) and adds setup friction that cuts against the
seamless-setup direction of ADR 0026.

### The Management key

Provisioning introduces a new platform secret whose blast radius is strictly
larger than the key it replaces: the Management key can mint, inspect, disable,
and delete every tenant's provider key.

- It lives in `adapters/`, never in `src/vexic`. This is the existing host-port
  boundary and provisioning does not bend it: the core takes a key through a
  port, and has no idea one was minted.
- It is never passed into `DreamPhasePorts.secrets`. The port carries the
  tenant's completion key, and nothing else. A dream phase that could reach the
  Management key could mint keys.
- Its custody and rotation story must be at least as strong as the current
  `OPENROUTER_API_KEY`, which has already been compromised once.

### Key lifecycle

- **Mint** at tenant provisioning, named for the tenant so a key is traceable to
  a tenant from the provider console.
- **Disable** on tenant retirement. Note that retirement does not currently cut
  live access at all (COA-323); provider-key disable is one leg of that fix, not
  a substitute for it.
- **Rotate** on suspected compromise, per tenant, without touching other tenants.
- **Delete** on purge, alongside the scope's other durable state (ADR 0022).

### Exhausting the limit

A tenant whose key hits its credit limit must fail *visibly*. A dream phase that
dies on an exhausted key and leaves no durable record -- while still advancing
the 24h retry clock -- would reproduce COA-374's silent-stall failure through a
new route, and would be worse here because it is an expected condition rather
than a transient fault. Limit exhaustion is a terminal, queryable outcome with
its own error type, not an exception that vanishes into a killed phase.

## Consequences

- `adapters/openrouter_live_adapter.py` stops reading `OPENROUTER_API_KEY` for
  hosted per-tenant dreaming and takes the tenant's key from the control plane
  via `DreamPhasePorts.secrets`. The ambient env read remains valid for
  single-key, non-hosted use (the local live-retrieval baseline harness,
  `docs/usage.md`), which is not multi-tenant and has no tenant to attribute to.
- The control plane gains per-tenant provider-key state: the key id, its limit,
  and its lifecycle status. The key *secret* is returned once at creation and is
  stored under the same posture as other tenant secrets (ADR 0008/0023) -- or
  not stored at all if it can be re-minted on rotation instead.
- ADR 0006's spend-cap gate is satisfiable without first fixing Vexic's usage
  telemetry. The telemetry bug (COA-375) remains a real defect and is fixed on
  its own merits; it is simply no longer load-bearing for billing safety.
- Onboarding friction is unchanged: zero. The tenant never handles a provider
  credential. This preserves the ADR 0026 seamless-setup direction.
- The platform's total exposure is now bounded by the sum of per-tenant limits
  rather than unbounded. It is not reduced to zero, and this ADR does not claim
  it is; the platform still pays for dreaming.
- Vexic core is unchanged. No contract, schema, or `MemoryService` operation
  moves. This is adapter and control-plane work by construction.
