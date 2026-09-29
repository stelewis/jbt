# Architecture

<!-- TODO: Update this document to reflect the current architecture -->

Use this document to describe the durable technical shape of the project.

Focus on stable boundaries, responsibilities, and interactions. Do not turn this page into an implementation diary.

## Summary

<!--
Write 2-5 short paragraphs that explain:
- the overall architecture style
- the main runtime pieces or subsystems
- how requests, data, or work flow through the system
- what should stay true even as implementation details change
-->

## System Boundaries

<!--
Describe the major boundaries in the system.

Useful prompts:
- What is inside the project boundary?
- What external systems, APIs, queues, databases, or files does it depend on?
- Which responsibilities belong here and which explicitly do not?
-->

## Main Components

<!--
List the durable components or layers and what each owns.

Prefer responsibilities over module names. Example categories might include:
- interface layer
- application or orchestration layer
- domain logic
- persistence or integration layer
- background processing
-->

## Data And Control Flow

<!--
Explain the important flows through the system.

Cover the flows that matter to future maintainers, such as:
- request or command handling
- validation and error handling paths
- persistence or event publication
- external integration boundaries
-->

## Key Invariants

<!--
Document the architectural rules that should remain true.

Examples:
- which layer may call which other layer
- where validation must happen
- where side effects are allowed
- transaction or consistency boundaries
- tenancy, security, or audit guarantees
-->

## Operational Considerations

<!--
Capture durable runtime concerns such as:
- scaling constraints
- performance-sensitive paths
- resilience expectations
- observability requirements
- deployment assumptions
-->

## Security And Trust Boundaries

<!--
Document the trust model for the system.

Cover:
- authentication and authorization boundaries
- sensitive data handling
- untrusted inputs and validation expectations
- secrets, keys, or credential usage patterns
-->

## Related Records

<!--
Link to ADRs and other durable docs that define architectural decisions or public contracts.

Examples:
- docs/adr/0007-some-decision.md
- docs/developer/context.md
- docs/developer/versioning.md
-->
