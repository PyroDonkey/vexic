# Vexic Context

Internal tooling doc for agent and maintainer planning; not public product
documentation.

Vexic is a provenance-first memory product for long-running agents. This
glossary pins down the product language used while moving from a local memory
core to standalone service surfaces.

## Language

**Memory Core**:
The host-neutral Vexic package that owns the public memory contract, memory
invariants, and local reference behavior.
_Avoid_: Hosted service, platform runtime

**Hosted Memory API**:
The networked Vexic service boundary for customer applications and product
operations.
_Avoid_: MCP server, dashboard backend

**MCP Adapter**:
The agent-facing integration layer that exposes selected Vexic memory
capabilities through MCP while delegating semantics to the public memory
contract.
_Avoid_: Core service, separate memory API

**Agent Integration Surface**:
The supported way external agent runtimes connect to Vexic memory.
_Avoid_: Control plane, product backend

**Host Transcript Recorder**:
A host-owned integration that captures completed agent turns, produces cleaned
replayable transcript material, and submits it to the memory core.
_Avoid_: Agent memory tool, direct database writer

**Control Plane**:
Account, billing, admin, auth, metering, and operational management around the
hosted memory API.
_Avoid_: Memory core

**Vexic Console**:
The human-facing product surface for hosted Vexic account, organization,
project, API-key, usage, and support workflows. The implementation lives in
the private `PyroDonkey/vexic-website` repo, not this one.
_Avoid_: Memory browser, memory core

**Customer Account**:
The human account/team boundary represented by a Clerk Organization in the
Vexic Console.
_Avoid_: Memory scope, customer memory database

**Customer Account Mapping**:
The hosted control-plane relationship that links a Customer Account to
Vexic-owned customer and tenant identity.
_Avoid_: Clerk organization as memory tenant id, passive tenant provisioning

**Customer Bootstrap**:
The intentional first-write workflow that creates a Customer Account Mapping,
Customer Memory Database handle, and first Project for a Customer Account.
_Avoid_: Page-load provisioning, operator-only account setup

**Project**:
A Vexic-owned control-plane record under a Customer Account that maps human
configuration to hosted API project scope.
_Avoid_: Clerk organization, memory database

**Agent API Key**:
A Vexic-owned machine credential minted by the hosted API for agent access to
project-scoped memory capabilities.
_Avoid_: Clerk API key, human session

**Console Service Credential**:
A Vexic-owned control-plane credential that lets the Vexic Console call hosted
control-plane routes as the Console service while carrying server-derived human
and organization context.
_Avoid_: Agent API Key, Clerk API key, human session

**Support View**:
The Vexic Console surface for account, project, key, usage, audit, job, and
incident metadata needed to operate hosted Vexic without browsing raw memory.
_Avoid_: Memory browser, transcript viewer, fact browser

**No Operator Raw Memory Access**:
The hosted Vexic rule that developers, admins, support operators, incident
responders, and other customers must never view a customer's raw agent memory
content or memory files. Customer memory-content questions must be resolved
through customer-visible access, customer-supplied evidence, sanitized
operational metadata, row counts, checksums, and scoped operation traces.
_Avoid_: Break-glass memory browser, admin transcript viewer

**Customer-Enabled Memory Processor**:
A hosted model or processor path that handles customer memory content only after
the customer explicitly enables that processing path. Unauthorized model
processing of customer memory is a data-exposure incident even when no human
views the memory.
_Avoid_: Ambient hosted model access, implicit memory processing

**Operational Telemetry**:
Sanitized control-plane records used to run, audit, meter, and debug the hosted
memory API without storing customer memory payloads.
_Avoid_: Product analytics, memory telemetry

**Non-Content Operational Aggregate**:
A derived operational metric that contains counts, rates, durations, token or
cost totals, or status totals without customer memory content, prompt content,
query text, transcript text, facts, tool bodies, secrets, or identifiers beyond
the approved operational dimensions.
_Avoid_: Product analytics dataset, retrieval query aggregate

**Product-Improvement Data Collection**:
Customer-data-derived collection used to improve Vexic's product behavior beyond
operating a tenant's own memory service.
_Avoid_: Operational telemetry, audit log

**Memory Scope**:
The customer-visible boundary that limits which tenant, project, user, or
session memory a caller may access. Agent identity can further refine this
scope when a host runs multiple agents inside the same parent memory boundary.
_Avoid_: Account, workspace

**Agent Scope**:
The optional memory refinement that separates one agent's private memory from
shared memory inside the same tenant, project, user, and session parent scope.
_Avoid_: Principal, actor identity

**Shared Agent Scope**:
Memory rows with no `agent_id`, visible only when callers explicitly request
the shared scope for the same parent memory boundary.
_Avoid_: Wildcard, legacy unscoped memory

**Customer Memory Database**:
The isolated storage boundary that contains one customer tenant's Vexic memory
data behind the hosted memory API. Project, user, and session boundaries remain
`MemoryScope` refinements inside it; control-plane catalog, auth, billing, and
routing metadata live outside it.
_Avoid_: Shared tenant rows, control-plane database

**Canonical Row Migration**:
A migration that preserves source memory records and provenance through Vexic
export/import, while rebuilding derived projections after import.
_Avoid_: Semantic replay, re-dream migration

**Summary Frontier**:
The set of a session's most recent, non-superseded `session_summaries` rows
(leaf or condensed) that together cover the oldest portion of that session's
transcript without gaps, oldest-first.
_Avoid_: All summaries, summary history

**Fresh Context**:
The bounded, no-query recap-plus-tail read (summary frontier plus a
token-budgeted raw transcript tail) that primes a new or resumed conversation,
distinct from a targeted search query.
_Avoid_: Search results, expand_history output

**Prime Context Header**:
The literal marker prefixed to host-injected SessionStart priming text so
recorders and ingest can recognize and exclude it from Tier 1 transcript,
keeping injected memory context from re-entering memory as if it were new
conversation.
_Avoid_: Transcript marker, system message
