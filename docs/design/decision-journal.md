# Decision Journal Custody Contract

The decision journal is an ordered history of immutable entries, selected by an exact committed head. This page owns its custody metadata and structural history rules. [ADR 0013](../adr/0013-decision-journal-and-review-basis.md) owns the durability, stage-local applicability, and pinned-review decisions.

An entry seals a separately retained [decision body](./decision-bodies.md) with its byte digest. Header validity does not certify that body's schema, references, financial meaning, or guards. Those are decision-variant and model-stage responsibilities. A captured financial input must retain and validate the selected headers, every referenced body, and the immutable review bases sealed by those bodies.

## Entry and head encoding

Both documents use exact UTF-8 JSON with sorted object keys, compact separators, native integer literals, and one final LF. Unknown fields, missing nullable fields, alternate encodings, duplicate keys, floats, invalid Unicode, and unsupported versions are rejected. Digests are exactly 64 lowercase hexadecimal characters.

The closed entry fields are:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1`, never boolean |
| `sequence` | Integer from 1 through `2^63 - 1`, in physical append order |
| `previous_entry_digest` | Null for sequence 1; otherwise the preceding entry's byte digest |
| `entity_id` | Stable entity identifier |
| `declaration_key` | Stable decision key within the entity |
| `kind` | `assign`, `split`, `identity`, `statement_revision`, `transfer`, or `link` |
| `operation` | `record`, `supersede`, or `retract` |
| `prior_entry_digest` | Null for `record`; the immediately active entry's byte digest for `supersede` or `retract` |
| `reviewed_attempt_digest` | Retained attempt against which the entry was reviewed; lineage, not an applicability condition |
| `reason` | Nonempty descriptive text, retaining exact Unicode and whitespace |
| `payload_digest` | Separately retained decision-body byte digest for `record` or `supersede`; null for `retract` |

Identifiers remain nonempty NFC keys without Unicode Cc controls. They are rejected, never repaired. Descriptive reasons may contain decomposed text, padding, newlines, and controls as valid UTF-8.

The entry's byte digest is SHA-256 over its complete canonical bytes. It is not embedded in those bytes. Stable declaration identity uses the existing entity-qualified declaration namespace. Immutable declaration revision identity uses the existing revision framing with unbounded effective endpoints and the complete header as payload; the body seal binds its content without including a self-reference.

A head has exactly `schema_version`, `sequence`, and `entry_digest`. Sequence zero with a null digest selects the empty journal. A positive sequence through `2^63 - 1` requires that selected entry's exact byte digest.

The journal entry/head, decision-body, and review-report schemas validate retained operational custody. Published journal headers follow `declaration_schema`. Strict artifact decoders return typed journal values; they do not apply decisions.

## Physical history and selection

Supplied entries form one contiguous chain beginning at sequence 1. Each preceding-entry digest must match the independently computed canonical bytes. Gaps, duplicate sequences, reordered entries, broken links, and a selected head that does not match its entry are errors.

The selected head must extend every supplied retained head. A retained head cannot be later than the selected head, and its digest must agree at its sequence. This rejects a valid-looking truncated chain together with a rolled-back head.

Selection returns the committed prefix and an explicit unselected suffix. It never folds the suffix. Input capture treats entries beyond the live committed head as uncommitted appends; pinned historical replay may instead encounter entries committed later. The caller supplies the retained heads applicable to its selection rather than silently consulting live state.

## Active decisions and logical order

The history key is `(entity_id, declaration_key)`. `record` establishes that key once, including after its eventual retraction. Reordering requires a new key.

`supersede` and `retract` must name the immediately active entry of the same entity, key, and kind. Unknown, stale, inactive, differently keyed, cross-entity, and different-kind references fail. Retraction disables a decision; it does not retract its economic event or erase history.

The first record's sequence is the decision's logical position. Supersession replaces the active revision at that position, while retaining its later physical append sequence:

| Physical sequence | Entry | Active logical order after the entry |
| --- | --- | --- |
| 1 | Record split A | A at 1 |
| 2 | Record assignment B to A's child | A at 1, B at 2 |
| 3 | Supersede A with an identical split body | A revision 3 at 1, B at 2 |
| 4 | Supersede A with changed split amounts | A revision 4 at 1, B at 2 |
| 5 | Retract B | A revision 4 at 1 |
| 6 | Record assignment C under a new key | A revision 4 at 1, C at 6 |

This structural fold neither evaluates B's financial guard nor treats changed amounts as equivalent. The enrich-stage fold must see A before B: an identical split preserves B's reviewed facts; changed child amounts produce `changed`, not an order-induced `missing`.
