# Build a Reconciled Checking and Card Record

This pre-alpha example starts with invented OFX evidence, not already booked transactions. Separate checking and card accounts share USD but keep independent quantity positions. Retained opening points support authored initialization; independent source and model checks prove the January balances. A real Beancount loader checks the recognized ledger projection.

Use a local filesystem on macOS or Linux and supported Python 3.14 or later. Install a published release with `uv`; no Git checkout or project dependency lock is required. Provisioning may fetch packages, Python, or advisory data before `jbt` starts. `jbt` itself does not install dependencies or make automatic network requests. Keep real financial records outside any public checkout.

## Install and acquire the example

Replace `<release>` with the selected published version:

```bash
uv tool install 'jbt==<release>'
```

Save the synthetic [project declaration](../../examples/checking-card/project.json), [prior OFX evidence](../../examples/checking-card/prior.ofx), and [main OFX evidence](../../examples/checking-card/main.ofx) as `project.json`, `prior.ofx`, and `main.ofx` in a local example directory. From that directory, create the financial root and copy the authored project:

```bash
ROOT="$PWD/checking-card-record"
mkdir "$ROOT"
cp project.json "$ROOT/project.json"
```

The selected root must not be a symlink. The authored project declares accounts, quantity positions, commodity, categories, dated account/section bindings and rules, horizon, evidence-citing opening movements, and output requirements. Its source selections are accession IDs; acquisition receipts own importer and source scope. The example IDs correspond to the exact files and arguments below. These are repeatable example facts, not timestamps to reuse for real acquisitions.

```bash
jbt acquire --root "$ROOT" prior.ofx --retry-key prior.ofx --acquired-at 2026-02-02T00:00:00Z --source-scope bank-feed --importer ofx
jbt acquire --root "$ROOT" main.ofx --retry-key main.ofx --acquired-at 2026-02-02T00:00:00Z --source-scope bank-feed --importer ofx
```

Acquisition copies evidence; it never moves or deletes the originals. Repeat the same request with the same retry key to recover an interrupted attempt. Use a different key for an intentional new accession. Reusing a key with changed bytes or acquisition context fails.

`--importer` names an installed `jbt.importers` entry point; acquisition records the selection without importing the plugin. Omit it to retain archive-only evidence. Select archive-only accession IDs in the project's `documents` array, not `sources`; use an empty array when none are selected. Building resolves every selected importer name uniquely before retaining execution inputs; missing and duplicate registrations fail explicitly.

## Build and inspect

```bash
jbt build --root "$ROOT" --project project.json > "$ROOT/build.json"
```

The project filename is relative to the selected root; absolute paths, traversal, and symlinked descendants are rejected. The result identifies an immutable generation, descriptor digest, `attempt_digest`, and `review_report_digest`. That generation contains the complete shared Parquet table set, extraction and model artifacts, financial findings, the ledger, and the summary. Its provenance records the immutable published release version, source commit when available, and observed Python, platform, and visible distribution versions. The current reference changes only after the entire generation has been validated and installed.

To request a change summary against a prior generation, add `--comparison-baseline <descriptor-digest>` to `build`. Without that option, comparison is absent rather than selected from current state. The prior generation supplies comparison data; its own earlier comparison history is not recursively rebuilt.

The January summary should report:

| Account | Opening | Credits | Debits | Closing |
| --- | --- | --- | --- | --- |
| Checking | 100 USD | 20 USD | -12 USD | 108 USD |
| Card | 0 USD | 15 USD | -9 USD | 6 USD |

The authored initialization is dated December 31, before the independently observed January 1 midnight points. February 1 midnight points check January's closing quantities. The card position retains its identity and liability rendering while its signed quantity crosses zero. A pending −9 USD card row remains visible but contributes neither booked quantities nor source equations.

For subsequent downloads, acquire the new bytes and add their accession IDs to `sources` before rebuilding. Repeated scoped `FITID`s support one event; distinct keys remain distinct even for identical purchases. Review purchases, refunds and interest at their stated signed account amounts. A foreign purchase retains its booked `CURDEF` amount and stated original currency/rate, not an invented original amount. Keyless pending authorizations remain separate until reviewed correspondence establishes their posted identity.

OFX supplies search bounds, not a complete-statement guarantee or whole-statement reissue evidence. Missing due coverage appears as `fail`; requiring coverage blocks that output. This is a reconciled working record, not a complete-account attestation. Complete and empty statement periods and whole-statement reissues are exercised by the synthetic importer, which explicitly states those facts. The [coverage contract](../developer/coverage.md) explains due periods, mid-month card cycles and publication policy; the [supported OFX profile](../developer/ofx-profile.md) explains source limitations.

Run the same release build again to reuse verified stages. Corrupt derived cache entries are reported and recomputed. Missing or corrupt retained financial inputs fail even with a warm cache. A failed required financial check cannot replace the current generation.

## Review and correct a record

Copy `attempt_digest` from the build result. A blocked build prints `attempt=<digest>` in its diagnostic; that retained report remains available even though no new generation was selected.

The supplied example's rules already categorize its recognized boundary legs. Select one to exercise the correction command, or select a null-category boundary posting in your own working record to clear an actual backlog:

```bash
ATTEMPT="<attempt_digest>"
jbt review --root "$ROOT" --attempt "$ATTEMPT"
POSTING="<boundary posting_id from the working tables or rendered output>"
jbt show "$POSTING" --root "$ROOT" --attempt "$ATTEMPT"
jbt correct --root "$ROOT" --attempt "$ATTEMPT" --target "$POSTING" --key purchase-category --set postings.category_id=expense --reason "Reviewed this purchase"
jbt build --root "$ROOT" --project project.json > "$ROOT/build.json"
```

`review` returns the complete report: resolved and blocked determinations, scoped findings, check results, complete rule evaluations, pre-entry evidence and correction receipts. `show` narrows to the selected event or posting, its source support, observations, provenance, rule predicates and competing assignments, and correction lineage. A full ID or unique lowercase hexadecimal prefix of at least 12 characters is accepted. Commands never rerun the project to choose a basis.

Use the summary's uncategorized counts and amounts to locate the backlog, then inspect each boundary posting's source facts and rule explanation before assigning a category. Rebuild after each decision, copy the new `attempt_digest` into `ATTEMPT`, and review the remaining work. Neither marking an event reviewed nor editing generated ledger text classifies its boundary legs. The final build's descriptor, not the initial working build's descriptor, is the pin to verify after clearing the backlog.

Choose a stable `--key` for the decision, not a value-derived name. It must be distinct from authored declaration keys in the shared declaration namespace. The command constructs the complete required guard from the retained interpretation and appends only if that attempt's journal head is still current. Another writer, a stale head, or unresolved/ambiguous target causes refusal. Rebuild and select the resulting attempt before another append.

Assignments support event payee, narration, tags, review state, cancellation and the five civil dates (`authorized`, `traded`, `posted`, `settled`, `value`), and posting category and dates. Text remains exact; tags use a JSON array with sorted unique replacement semantics. Categories must be registered logical keys. Dates use ISO `YYYY-MM-DD`; posting dates also accept `inherit` or `unknown`. Use `--clear <field>` for an explicitly unknown nullable value. A recognized event still needs its effective posted date. `--set transactions.event_state=cancelled` cancels participation; retracting that decision restores the source-derived state. It cannot promote pending evidence to recognized or replace a source revision. No scalar amount, account/position binding, source pointer, or arbitrary field-path assignment is allowed.

Split a boundary posting into stable authored child keys with repeated `--split` options instead of `--set`:

```bash
jbt correct --root "$ROOT" --attempt "$ATTEMPT" --target "$POSTING" --key purchase-split --split food=3 --split supplies=5 --reason "Allocate an 8 USD boundary posting"
```

At least two child keys are required, and their signed amounts must exactly conserve the parent. Classify children with later decisions. An identical split supersession preserves child IDs and dependent corrections; changed child amounts require dependent guards to be reviewed.

Every append returns `entry_digest`. To revise a decision, use its stable key and `--supersede <active-entry-digest>` against the latest retained attempt. This also works after changed economics block publication: the new report supplies the original-position facts to review, not the previous correction's output. To disable it, run `jbt retract <active-entry-digest> --root "$ROOT" --attempt "<latest-attempt>" --reason "<nonempty reason>"`. Retraction disables the decision without deleting custody or retracting the financial event. A retracted key cannot be recorded again.

Two active assignments to one field conflict. Missing, ambiguous, inactive, or changed guards are blocking, never waived; reproducing rule-only values is informational. Explicit corrections survive changed rule output when reviewed economics and structure are unchanged.

For a backlog, enable tabular and summary outputs while disabling Beancount in the project's output configuration. Beancount rejects unclassified boundary legs with `unclassified_boundary`; it does not render a placeholder category. Correct the IDs in the published working tables, rebuild, and confirm the summary's uncategorized work is empty. Then enable Beancount, build again and verify that final generation. A classification-requiring output also rejects the backlog even when its sink can preserve null categories.

## Decide correspondence and statement revisions

`review` retains source-occurrence and statement targets even when ambiguous correspondence blocks an economic event. Its counts separate pending events from blocked determinations. Pending-to-posted proposals identify unique same-position, same-quantity observations within seven days; they never merge records automatically. Inspect every operand with `show` before recording a decision.

Use source-record IDs from the retained report, not transaction IDs:

```bash
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key authorization-posting --reason "Reviewed pending and posted evidence" identity --mode key_family --canonical "<posted-source-record-id>" --member "<pending-source-record-id>"
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key reused-key --reason "This occurrence is a separate purchase" identity --mode occurrence --canonical "<source-record-id>" --member "<source-record-id>"
```

`key_family` anchors at the canonical record's stated key and admits all present and future occurrences of other named keys. Keyless members are admitted individually. `occurrence` moves only the named source occurrences to the canonical acquired anchor, leaving other occurrences of a reused key untouched. Merge decisions precede enrichment; retired event and posting IDs do not redirect their corrections to the surviving event. Review, supersede or retract affected corrections explicitly.

A reviewed reissue names old and replacement statement IDs and an exact financial scope:

```bash
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key january-reissue --reason "Reviewed replacement statement" statement-revision --replace "<old-statement-id>" --with "<replacement-statement-id>" --account card --fact-kind movement --basis posted --start 2026-01-01 --end 2026-02-01
```

Both statement sets are nonempty and disjoint. The period is half-open; parallel balance bases and facts outside it remain unchanged. Events whose support is entirely replaced become retracted with retained IDs, while later reversals remain new recognized events. OFX `CORRECTACTION` correspondence is source evidence and needs no human identity decision. Standing source authority belongs in project configuration, not the journal. All structural decisions use the same stale-head protection, original-position supersession, retraction and checked replay as corrections.

## Decide transfers and attach evidence

Internal transfers, including card payments, name source position posting IDs, not their balancing boundary IDs. Declare a quantity position with `purpose=in_transit` for the commodity, in a separate internal account, and include its Beancount rendering mapping. Review proposes opposite single-leg and same-day aggregate candidates across accounts within seven days; suggestions never change treatment automatically.

```bash
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key card-payment --reason "Reviewed checking departure" transfer --outgoing "<checking-posting-id>" --commodity USD
```

A one-sided decision leaves a visible transit balance. Retain its returned `entry_digest`; rebuild and select the new attempt, then inspect the arrival and supersede the decision:

```bash
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key card-payment --supersede "<active-entry-digest>" --reason "Reviewed card arrival" transfer --outgoing "<checking-posting-id>" --incoming "<card-posting-id>" --commodity USD
jbt build --root "$ROOT" --project project.json > "$ROOT/build.json"
```

Repeat either member option for multiple postings; complete transfers must conserve exactly. Transit holdings remain visible between separate departure and arrival dates, including a month-end departure and next-month arrival, without income or expense. Category corrections and splits on transferred boundaries conflict rather than being discarded.

Acquire a receipt without an importer, add its accession ID to `documents`, and rebuild before linking its blob digest from the retained review:

```bash
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key purchase-receipt --reason "Reviewed receipt" document --target "<transaction-or-posting-id>" --document "<blob-digest>"
jbt decide --root "$ROOT" --attempt "$ATTEMPT" --key attempt-fee --reason "This failed attempt incurred the separately posted fee" lifecycle --failed-attempt "<transaction-id>" --fee "<fee-transaction-id>"
```

Rebuild and select the latest attempt between appends. Receipt metadata contains blob digests, not filesystem paths. A lifecycle link is a human assertion, not a cancellation: it changes neither participation nor amounts, and the recognized fee remains in balances. The [reviewed decision contract](../design/decision-bodies.md) defines operands, stage-specific guards and original-position supersession.

## Verify from retained inputs

Copy `descriptor_digest` from the final build's JSON result into the variable below. Pin the corrected generation explicitly rather than following a moving current reference:

```bash
DESCRIPTOR_DIGEST="<descriptor_digest from build.json>"
jbt verify --root "$ROOT" --baseline "$DESCRIPTOR_DIGEST"
```

Verification requires the recorded published `jbt` and selected importer versions and profiles, loads the generation's historical financial selection, validates its exact retained inventory, and rebuilds without persistent cache reads or writes. It compares financial tables, findings, precision, lineage, and deterministic ledger/summary output, not whole manifests or physical Parquet bytes. Neither success nor failure replaces the baseline or current reference. Success establishes reproducibility in the observed supported environment, not independent financial truth.

Preserve the complete financial root when restoring it: source objects, intents and receipts, exact retained authored selections, journal bodies and bases, attempt witnesses and reports, the pinned generation, and any explicitly selected comparison generation. Historical verification uses only its captured journal head and canonical entries, not later live metadata or uncommitted suffixes. Later decisions, a deleted working project/cache, and root relocation do not change that pin. The inbox, original authored working file, checkout, package cache, and old installation are not recovery inputs. Reinstall the recorded release normally with `uv` before running verification.

Supported Python/platform and compatible dependency versions may differ from the original run; verification reports those facts and succeeds only if financial results match. Timezone rules must match the retained `tzdata` provenance before reconstruction. Source-commit provenance is optional, not an additional release-matching requirement. An unavailable release, unsupported environment, different release version, timezone-rule mismatch, or financial mismatch fails explicitly. `jbt` does not repair the installation. Development builds from editable source are useful for synthetic experiments but bypass persistent cache and cannot verify a published-release baseline.

## Limits and failures

- The processor resolves explicitly bound quantity-only bank/card positions, dated symbol aliases, compatible keyed repeats, and authored transactions, assertions, and notes. Distinct keys remain distinct even with identical amounts and dates. Conflicting keys, ambiguous positions, and unbound sections produce scoped blocking findings; explicit ignored sections retain evidence and their reason.
- [Typed bank/card rules](../developer/rules.md) match original text, balancing amounts, account/commodity keys, dates, and registered source observations through `equals`, `in`, or literal text `contains`. First scalar assignments win; tags union; corrections follow. Unclassified recognized boundary legs remain in working tables and the summary's uncategorized work. A classification-requiring sink refuses them; Beancount reports `unclassified_boundary` rather than inventing a category. Review labels never suppress recognized quantities; unreviewed ledger events use `!`.
- Pending, cancelled and retracted rows do not book. Source authority and source-stated or reviewed revisions select financial evidence without using acquisition order. A pending item disappearing from a later window remains pending; recognition requires recognized supporting evidence. Recurring rule splits, match views for unimplemented processors, and inventory remain later-slice work and fail explicitly when requested.
- Date-only balances do not establish midnight points. Supported comparisons use explicit account-civil boundary evidence and pinned timezone rules. Search intervals are half-open; returned rows outside their bounds remain evidence. Unavailable interpretation blocks only the determination that requires it; unresolved supplementary clocks remain evidence with warnings, never guessed model dates. Account lifecycle is mandatory; optional coverage requirements do not waive lifecycle failures.
- Another active writer causes refusal rather than concurrent mutation. Retry after that writer finishes.
- Supported Python socket and DNS attempts are denied during processing and prevent publication even if caught. This guard is not a native-code sandbox.
- A failure after current-reference replacement but before durability acknowledgement is uncertain acknowledgement: inspect the complete selected generation before retrying. It is not a promise that the old pointer remains selected.

The [implemented architecture](../developer/architecture.md) owns the execution boundaries and durability contract. Later financial families remain separate delivery work.
