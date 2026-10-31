# Reviewed Decision Bodies

The quantity processor supports guarded identity correspondence, scoped statement revisions, assignments, conserving boundary-leg splits, internal transfers, and document/lifecycle links. This page owns their bodies, native review projections, and applicability evidence. The [journal custody contract](./decision-journal.md) owns immutable headers, committed-head selection, and original logical positions.

## Bodies and targets

Every body names its entity, operation, and review-basis reference: the retained attempt digest and basis digest. The journal header owns the reason, record/supersede/retract operation, prior entry, and body seal. Retraction has no body.

| Kind | Operation |
| --- | --- |
| `assign` | Replace permitted fields on one event or leg |
| `split` | Replace a boundary leg with stable keyed children conserving its amount |
| `identity` | Admit reviewed source occurrences to an explicit key-family or occurrence anchor |
| `statement_revision` | Replace reviewed statement evidence within one account, fact kind, basis and half-open period |
| `transfer` | Replace source legs' economic boundaries with declared transit holdings |
| `link` | Attach archived documents or assert a failed-attempt/fee relationship without changing economics |

An event reference contains its entity and event ID; a leg reference contains its entity and leg ID. Identity operands are source-record IDs; revision operands are statement IDs. All operands belong to the body's entity. Collections are ordered and unique at their declared grain. Interpreter qualification remains deferred.

The `decision_body_schema` describes these closed operations. Wire bodies and bases use required nullable keys, native JSON integers, exact UTF-8, and compact sorted encoding with one final LF. Alternate encodings, duplicate keys, and unsupported shapes fail rather than being repaired. Schema validation and typed constructor constraints establish shape, not authorization.

Event assignments cover nullable payee and narration, sorted unique exact-text tags, review state, cancellation, and the five civil-date roles. Cancellation may assign only `cancelled`: it cannot recognize a pending record or substitute for a source revision. Retracting the assignment restores source-derived participation. Every event retains its effective/binding role independently of participation: posted for recognized source events, initiation or authorization for pending source events. Cancellation and retraction preserve that role. Editing its date updates the effective date; editing another role does not. A required effective date cannot become unknown or exceed the horizon without blocking resolution. Authored cancelled or retracted transactions must supply exactly one posted or authorization date to establish their role; supplying both is ambiguous, even when the dates agree. Leg assignments cover nullable category and declared date modes: `inherit`, `value`, or `unknown`. Source quantities, identity, account/position bindings, and derived weights are not scalar assignment permissions.

A split supplies at least two distinct stable child keys and exact amounts. Conservation compares numeric values, not lexical precision. Children inherit the parent's category and dates; subsequent assignments can change them. Child identity derives from the stable parent leg key, decision key, and child key, never revision digest or amount.

An identity body supplies `mode`, `canonical`, and sorted unique `members`, including the canonical source record. `key_family` anchors at the canonical record's source key: every present and future occurrence of another admitted key joins it, while keyless members are admitted individually. `occurrence` anchors at the canonical record's acquired occurrence and moves only the named members. This separates a reused key without redirecting its remaining occurrences. Identity correspondence does not authorize conflicting financial facts. Removed identities remain retired and corrections never follow them.

A statement-revision body supplies sorted unique `replaced` and `replacement` statement IDs, `account`, `fact_kind`, `basis`, `start`, and `end`. The statement sets are nonempty and disjoint. Movement replacement requires complete coverage of the requested period, which several replacement statements may supply collectively. One declared replacement set is not a competing revision branch; incompatible replacement alternatives or causal cycles block. Replacement changes only the requested half-open financial scope; it neither replaces an entire envelope nor changes parallel bases. Events with no remaining live support become retracted under their existing IDs. A later reversal remains a separate recognized event.

### Transfers and transit

A transfer body supplies `commodity`, sorted unique `outgoing` and `incoming` source-position leg IDs, with at least one side. Outgoing quantities must be negative; incoming quantities must be positive. When both sides are present, their aggregate quantities must conserve exactly across different accounts. Several legs may supply either side; amounts are never changed to complete a match. Card payments use this same relation.

Declare one applicable quantity position per commodity with `purpose=in_transit`, in an internal account distinct from its source accounts. Each member's corresponding boundary becomes an equal-and-opposite position leg in that holding, retaining its semantic key and posting ID. The source quantity and its date stay unchanged. A one-sided decision leaves a visible transit balance and publishes no transfer link. Supersede it with both sides to complete the relation; separate departure and arrival dates preserve transit across month ends.

Ownership is per source leg, not per event. Disjoint source legs of an event may belong to different transfers; a source leg cannot be claimed twice. An active category correction or split on the corresponding boundary conflicts rather than disappearing. Rule category contributions become shadowed diagnostics. Independent projected checks verify complete transfer membership and exact conservation.

Transfer proposals compare recognized transaction source-position legs, individually and in same-account, same-day aggregates, in one commodity across different accounts within seven days. Opening balances are not transfer movements. Proposals are deterministic suggestions, never automatic links or evidence of completeness.

### Document and lifecycle links

A document link supplies an event or leg `target` and nonempty sorted `documents` blob digests. Select archive-only accession IDs in the project's `documents` array, separately from importer-backed `sources`. Capture pins their receipts and bytes for review and replay. The guard covers the target and every selected document. Rendering exposes digest metadata, never recovery paths or Beancount `document` directives.

A lifecycle link names an `attempt` event and a distinct recognized `fee` event with outgoing source position legs. The mandatory journal reason is the human assertion of their relationship; machine-readable failure evidence is not required. Published membership identifies the attempted transaction and the fee's position postings. The link changes neither participation nor amounts: cancelling an attempt is a separate assignment, and a paid fee remains in balances.

## Pinned review basis

A basis has exactly `schema_version`, `attempt_digest`, `report_digest`, `before_key`, and `target`. The immutable report must be the one registered for that invocation, not merely another sealed document claiming the same attempt. Capture retains the attempt-to-report binding; historical replay uses those captured references without consulting later live metadata.

For a new decision, `before_key=null` selects its consuming stage's tail, reconstructed from the report's single inventory by reversing later-stage receipts. An enrichment decision therefore reviews pre-transfer structure even when the displayed final posting is structural. For supersession, the stable decision key selects the complete reconstructed pre-entry state at its original logical position. The replacement may deliberately select different eligible operands at that boundary, without changing the decision's closed body variant, key, or position; document and lifecycle links are distinct variants. Enrichment and link targets must be uniquely resolved and active; merge decisions may review uniquely interpretable source evidence whose economic correspondence is blocked. A later decision's output or the replaced decision's own split children are not eligible. Commands never rerun resolution against facts the user has not reviewed.

Report and basis seals establish custody, not live applicability. A decision remains applicable in another attempt when its required pre-entry projection is unchanged.

## Guard profiles

The model owns the complete profile for each target kind; bodies cannot choose fields, masks, expected digests, or weaker profiles.

| Target | Reviewed facts |
| --- | --- |
| Event | Source identity, event kind/state, effective role/date, accounts, civil dates, and ordered leg structure |
| Leg | The same event context, logical parent, and the addressed leg's stable key, kind, binding, signed amount, commodity, date modes, corresponding balancing structure and transfer ownership |
| Source occurrence | Bound financial facts and effective role, source anchor and status, and its current canonical membership |
| Statement | Financial evidence and its scope, basis, completeness and current scoped dispositions |
| Archived document | Selected archive-only blob identity |
| Structural decision | One compound projection over every named native operand |

Descriptive overrides, classification/rule output, policy revisions, lexical amount precision, and report/attempt seals are not applicability fields. A rule that stops matching does not invalidate a classification decision when its reviewed economics and structure remain unchanged.

Each receipt retains the stable decision key, active entry digest, expected target, **all candidates at that target address**, outcome, and expected/observed projection seals. Structural candidates are composed from the named native operands, so changing either side invalidates a correspondence or revision. The report retains one complete tail inventory for inspection and new decisions, pending and blocked counts, and deterministic proposals that never apply automatically. Receipts also retain reverse changes for addresses whose guard projection, membership, or activity changed between their pre-entry state and the next boundary. Walking these changes backwards from the tail reconstructs any original-position boundary without repeating unrelated records. Empty candidate lists remove addresses introduced by a split; final blocking activity changes belong to the last receipt. Descriptive or category-only changes do not duplicate unchanged guard projections.

| Candidates at the addressed pre-entry boundary | Outcome |
| --- | --- |
| None | `missing` |
| More than one | `ambiguous` |
| One inactive candidate | `inactive` |
| One active candidate with unchanged reviewed facts | `applied` |
| One active candidate with changed reviewed facts | `changed` |

Malformed or unavailable interpretation has a located failure, not invented financial facts. All failed guards block publication. References never redirect to a successor or a similar record.

## Folding and independent verification

Active decisions execute by stage: identity and revisions in merge, assignments and splits in enrich, then transfers and links in link. Within each stage they retain original first-record order. Supersession replaces the revision at that position; it does not move a split behind its child assignments. An identical replacement split preserves child guards. Changing child amounts preserves their IDs but produces `changed`, not `missing`.

Two active assignments to one scalar field conflict. A correction reproducing the rule-only category is informational. Splits must conserve exactly and operate on an eligible boundary leg. Final rows are outputs, never the basis for re-evaluating a guard against its own effects.

The independent consumer reconstructs journal order and boundaries, verifies native profile and identity/parent closure, and recomputes outcomes from retained target-specific pre-entry evidence. Reverse changes have unique addresses and candidates matching their address; each receipt's candidates must equal the reconstructed boundary at its target. Matching expected and observed digests alone is insufficient. Source/model checks and financial golden expectations remain separate from replay.
