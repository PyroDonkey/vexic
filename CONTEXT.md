# Vexic Context

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
