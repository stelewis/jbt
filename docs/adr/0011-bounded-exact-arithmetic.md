---
id: 0011
title: "Bounded Exact Arithmetic"
status: accepted
date: 2026-09-28
tags: [numbers, analysis, data-contracts]
supersedes: null
superseded_by: null
---

## Context

An 18-decimal token quantity and a large monetary total can exceed a 38-digit SQL decimal even when each is ordinary source evidence. A total cost of 10 for three units also cannot be replaced by a finite unit cost. Exactness requires preserving totals and controlling division, not thousands of digits of incidental working precision.

## Decision

Use the coefficient/scale representation and checked arithmetic in the [analytical contract](../design/analysis-boundary.md#exact-decimals-and-query-arithmetic). The version-1 limits are 96 coefficient digits and 38 fractional/source-scale digits for stored values, and 384 digits with scale at most 152 for intermediates. These are resource ceilings, not claims about source precision or permission to round evidence.

The storage envelope contains every unsigned 256-bit integer (at most 78 decimal digits), including raw on-chain quantities, without depending on a native SQL integer. Scale 38 includes 18-decimal token quantities and finer supported rates. Ninety-six digits leave integer/fractional headroom while remaining a small bounded string; this is not a claim to support every possible source encoding. A 97-digit coefficient or scale 39 is rejected explicitly, retaining the original evidence for a reviewed future schema extension.

Four stored coefficients can be multiplied within a 384-digit/152-scale envelope. This admits useful quantity, price, multiplier, and conversion expressions without promising that every composition or unbounded aggregation fits. Implement operations using standard-library integers; do not add an arbitrary-precision dependency or create a general symbolic algebra system.

Admission checks precede integer parsing, powers of ten, products, division alignment, and accumulation. Check decimal digit lengths and scale bounds first; for a sum of `n` aligned operands allow the maximum aligned digit count plus `ceil(log10(n))` digits, computed with integer bounds rather than floats. Validate actual results too. Rational denominators are positive and nonzero; cancel common factors before further bounded operations where possible. Rounding uses integer quotient/remainder at a declared scale with `half_even`, `half_up` (ties away from zero), or `toward_zero`. Final residuals belong to an explicitly selected slice, never an unrecorded plug.

Use checked DuckDB decimal expressions where their entire operation fits; use the independent consumer's small integer-UDF adapter for division and wider values. Keep the adapter outside pipeline internals. No SQL expression may silently promote financial arithmetic to floating point. Step 0 must prove UDF result types and actual Parquet/DuckDB round trips before their API is frozen.

## Consequences

The profile covers the concrete [numeric exercises](../design/schema-exercises.md#numeric-limits-and-rounding) with bounded work per primitive, rather than an unexplained very-large-number requirement. Intermediate limits do not bound total input size or replace ordinary file/row resource controls. An expression can fail even when a mathematically simplified result would fit; that failure is explicit, and a reviewed equivalent bounded algorithm may replace it without relaxing exactness.

Nonterminating division remains rational until the specified rounding point. Totals, source scales, and fee residuals stay authoritative. Supporting finer source precision or a larger envelope requires schema and conformance changes, not a runtime knob.

## Alternatives considered

### Universal `DECIMAL(38, s)`

Rejected: choosing one scale trades away range and cannot preserve all supported source values. It also does not make SQL division exact.

### Unbounded integers or a 1,024/4,096-digit profile

Rejected: no current example requires that capacity; it increases the admitted arithmetic/resource surface without a demonstrated financial need.

### Binary floats or rounded authoritative unit costs

Rejected: approximate storage loses evidence and breaks exact conservation, regardless of display precision.

## Related

- [Authoritative inventory allocations](0010-authoritative-inventory-allocations.md)
- [Contract conformance](../developer/tools/local-workflows.md#contract-conformance)
