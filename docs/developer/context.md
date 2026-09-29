# Project Context

`jbt` is a local, offline build system for reproducible financial records. It preserves original evidence and versioned authored inputs, resolves source observations and reviewed decisions into canonical economic records, then publishes ledgers and analytical snapshots. The goal is an evidence-preserving reproducible data pipeline. The shape is familiar from raw/curated data architectures, lineage-oriented systems, and event replay.

Statements can overlap, disagree, be corrected, or be reissued. Treating each transaction-shaped record as the primary immutable event could make source revisions look like changes to reality, when they’re changes to evidence or interpretation.

## Evidence And Economic Events

A source occurrence is evidence *about* an economic event, not the event itself. A canonical event is the model's determination from available evidence and decisions, not a claim of perfect access to reality. The underlying idea is the distinction between a thing and the evidence or claims we have about it. Here, that means keeping separate:

- **Evidence:** Which retained source occurrences assert which facts?
- **Identity:** Which occurrences describe the same economic event? Its retained anchor does not change when another source supports it or supplies better facts.
- **Authority:** Which source supplies a fact when claims differ? Selecting a source for facts does not select the event's identity.
- **Recognition:** Does the event participate in the recorded economic history? Source status and review state do not determine participation by themselves.

A retracted source assertion remains inspectable and may change an event's recognized participation without deleting its identity or history. A real reversal is a separate economic event, not a retraction of the earlier evidence. The core choice is what is authoritative. Here, it’s the retained evidence and versioned decisions; canonical events and booked records are reproducible determinations from them.

## Reproducible Determinations

Original documents, acquisition records, authored inputs, and versioned decisions are retained. Extraction and resolution produce observations, canonical records, checks, ledgers, and analytical tables; corrections do not overwrite source claims. A content digest verifies byte integrity, not authenticity, truth, or economic identity. Derived views and published snapshots make determinations usable, but do not become the underlying evidence.

A clean build of the same retained input snapshot under the same schemas and toolchain reproduces its outputs. A changed parser, statement, or policy can change a determination without rewriting retained evidence or silently changing an established event identity. Reproducing a historical snapshot requires its inputs and execution context. Comparing "as filed" with "as recomputed" is snapshot comparison, not an automatic query of everything known at a past time.
