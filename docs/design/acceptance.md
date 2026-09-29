# Acceptance Cases

These cases specify the full system's behavior. Use them to establish shared schemas and equations before implementing the pipeline, then turn them into executable tests as each processor is built.

Each implemented case supplies synthetic source bytes or authored evidence, independently calculated expected records, and the relevant model results, findings, and outputs. This inventory specifies required tests; it is not a claim that fixtures or processors already exist. A valid supported event must produce its expected result. Missing required facts, invalid input, and operations outside the pipeline's responsibility have explicit failure or downstream-policy outcomes.

## Cross-capability histories

These histories exercise interfaces that isolated cash or instrument examples miss. Use their complete records when choosing identities, field types, relationships, and artifact grains.

[Schema handoff exercises](./schema-exercises.md) supply concrete table rows and payloads for mixed brokerage relationships, pools, source status, precedence, contract terms, N-ary links, concurrent quotes, and numeric limits. Their negative cases are part of acceptance, not optional documentation examples.

### Acquisition, split, transfer, disposal, and backfill

One entity owns accounts A and B. Instrument Q is a synthetic share. Policy keeps principal book cost and acquisition fees separately traceable and allocates fees pro rata by units.

| Event | Expected inventory and components |
| --- | --- |
| Acquire 10 Q in A for principal 100 and fee 2 | One acquisition lineage; quantity 10, principal cost 100, fee component 2; cash effect -102 |
| Apply a 2-for-1 split | Quantity 20, principal cost 100, fee component 2; same acquisition origin; no cash flow |
| Transfer 8 Q from A to B | A retains 12 with principal 60 and acquisition fee 1.20; B receives 8 with principal 40 and acquisition fee 0.80 |
| Dispose of 5 Q from B for proceeds 40 and fee 1 | Matched principal 25 and acquisition fee 0.50; cash effect +39; B retains 3 with principal 15 and acquisition fee 0.30 |

The remaining 15 Q carry principal 75 and acquisition fee 1.50. Their accounts and portions differ, but all trace to the same acquisition. The disposal exports proceeds 40, principal 25, acquisition fee 0.50, and disposal fee 1 separately. A tax consumer can apply different matching and cost treatment.

Test both book policies. Expensing the acquisition fee when paid leaves matched book cost 25 and gross disposal book result 15; the disposal fee is a separate expense of 1. A matched-trade performance calculation can report 13.50 after both allocated fees, without posting the acquisition fee a second time. Capitalizing acquisition fees instead makes matched book cost 25.50 and gross disposal book result 14.50, with no acquisition-time fee expense. The original principal and fee components survive either policy.

Give the security and cash legs distinct trade/settlement dates, with settlement across a statement boundary. Source checks use the appropriate leg dates. Rendering preserves the same inventory and cash effects, using explicit clearing movements where the ledger representation requires them.

Then correct the split evidence to 4-for-1 while leaving the stated transfer and disposal quantities unchanged. Correct final inventory is A: 32 Q/principal 80; B: 3 Q/principal 7.50. The disposal matches principal 12.50 and acquisition fee 0.25. Dependent results change; acquisition identity and raw evidence persist. A lot-selection correction guarded by the earlier inventory must be reviewed rather than silently reused.

This history must survive extraction, linking, booking, table export, ledger rendering, and clean replay. Counting the incoming transfer as a new purchase, keying a lot by current cost, or storing only a rounded unit cost fails it.

### Contract settlement and collateral

A declared linear contract has multiplier 10. Open one long contract at 100, settle daily at 103, and close at 105. The source states variation settlement of +30 followed by closing settlement of +20.

The published transitions are entry at 100, reset from 100 to 103 with settlement attribution 30, and close from 103 at a closing price of 105 with settlement attribution 20. The original 100 remains at entry. Total settlement is 50, not 80. The independent reader replays these transitions without calculating settlement or resetting a reference itself.

Keep collateral 200 in a separate position and include a withdrawal of 40. That withdrawal changes collateral to 160 without changing contract profit. A second source describing variation as collateral rather than final settlement requires the corresponding contract policy; matching numbers cannot erase the distinction.

### Shared ownership and authority

Owners A and B retain different-format evidence of one joint account. Both show opening 100 and closing 110 after an external credit of 10. The consolidation registry maps their local account IDs and selects A's feed for the period.

Household holdings are 110, not 220. A declared 60/40 beneficial allocation produces 66 and 44 where that reporting basis is requested; it does not change the source account's balance. If B's comparable closing balance is 111, the unresolved financial disagreement blocks consolidation.

Add a transfer of 20 from A's individual account into the joint account. Household external inflow is zero for that transfer; both account effects remain. A later authority switch to B validates the boundary balance. For invested accounts it also maps lot lineage, quantity, principal, and fees; equal aggregate market value is insufficient.

### Corrections, overlap, and publication

A rolling download and a statement observe the same posted debit of 5. A payee rule changes its category; the economic identity and all supporting source references stay intact. A correction targets that identity with an amount guard of 5.

A revised authoritative source changes the debit to 6. The guard fails even if the source transaction ID is unchanged. Review appends a superseding decision; the old entry and evidence remain. Both source reconciliation and model reconciliation run against the appropriate revision.

Interrupt the next build after writing its new table but before its ledger or required checks complete. Readers still see the previous complete generation. A retry produces the expected new generation, and `verify` compares it without replacing the baseline.

### Multi-origin transfer and pooling

Account A holds L1: four Q with principal 40, and L2: six Q with principal 120; fees are known zero. Transfer six Q to B under an explicit selection of two from L1 and four from L2. One outgoing and one incoming economic leg each span two allocations. Their inventory changes are:

| Slice | Units delta | Principal delta |
| --- | --- | --- |
| A/L1 | -2 | -20 |
| B/L1 | +2 | +20 |
| A/L2 | -4 | -80 |
| B/L2 | +4 | +80 |

B holds principal 100. Selecting three from each instead produces principal 90; the same link members and aggregate quantity must not make these artifacts equivalent. A reader must distinguish them without re-running the selection method. Transfer legs have no disposal or book-result component.

In a separate pooled history, contribute ten Q/principal 100 and twenty Q/principal 260 to P. Pool entry removes the individually held balances and preserves both contribution origins. Reducing twelve Q consumes principal 144 and leaves eighteen Q/principal 216. The reduction names P, not either constituent. Transfer six of the remaining pooled units to pool P2 in B: principal 72 moves, P retains twelve/principal 144, and P2 retains the contribution lineage without fabricated acquisition dates. A downstream tax example can rematch the original acquisitions independently.

Also contribute a lot with principal 50 EUR to a USD pool under a declared rate of 2 USD/EUR. Source principal delta is -50 EUR, target is +100 USD, and the conversion records both amounts and the rate. The source lot's book currency is not rewritten. Omitting conversion, adding unlike amounts, or converting an unknown basis as zero fails.

### Exact totals, rounding, and component ownership

Initialize three units with evidenced principal total 10 and known zero fees. Publish exact total 10, not a finite approximation to 10/3. Under an explicit two-decimal half-even allocation policy, sequential one-unit reductions consume 3.33, 3.34, and 3.33: the second allocation uses the remaining 6.67 over two units, and the last consumes the residual. Final units and basis are zero. A different declared residual policy has different intermediate allocations and must publish those decisions.

Repeat with two acquisition origins in one purchase and multiple fee currencies. Assert exact acquisition consideration, per-origin principal, original fee allocations, converted book amounts, disposal proceeds, and remaining components separately. Rounded unit views cannot change any total. Under capitalization, principal plus capitalized fees equals carrying cost; under expensing, fee attribution is not another disposal expense. Source total and source per-unit price can disagree and both remain observations.

For mixed consideration, exchange ten Q with principal 100 for five R assigned principal 70 under reviewed economic policy, plus cash 40. One transformation removes Q/principal 100 and adds R/principal 70; released principal is 30. Its disposal records proceeds 40 and gross book result is 10. The cash statement's later receipt is linked to recognition/settlement, not counted again as consideration. The independent reader must obtain these results from exact changes and weights without choosing the 70 allocation or interpreting a per-unit action term.

### Participation and replay

| History | Required output or failure |
| --- | --- |
| Pending authorization 5 later recognized with a changed source ID | One canonical event after explicit identity correspondence; source statuses retained; only the recognized interpretation has book steps |
| Lifecycle evidence confirms a pending authorization was cancelled | Cancelled state, no recorded-balance effect; review status cannot make it recognized |
| Pending authorization is absent from a later rolling window | Absence alone cannot establish cancellation or retraction; retain the pending observation and require decisive lifecycle or complete-replacement evidence |
| Failed transfer with paid network fee 1 | Cancelled attempt plus linked recognized fee effect; fee remains in balances |
| Ten Q, same-day two-for-one split and acquisition of five Q | Evidenced split-before-buy yields 25; buy-before-split yields 30; absent decisive order blocks booking |
| Add an unrelated earlier event | Step sequence can change; existing step and event IDs do not |
| Order constraints form a cycle | Explicit failure; no arbitrary topological fallback |
| One source fill reverses long one contract to short one | Separate closing/opening economic legs and determinations; only one unit closes; common source evidence retained |
| Reset only part of a contract lot | Named branches preserve origin and distinct current references; no scalar reference overwrites both portions |
| Open two reference contracts with an attributed fee of 2 EUR, then close one | The close removes a 1 EUR acquisition-fee slice under declared pro-rata attribution; the remaining slice retains 1 EUR; neither changes the reference or creates a second expense |
| Transfer has separate departure and arrival dates | Declared in-transit position preserves the held slice between steps; no temporary disappearance or invented acquisition |

### Gross assertions and identity survival

One account holds a +100 USD claim against X and a -40 USD obligation to Y. A complete position assertion has two rows and checks each relationship. A net assertion of 60 has a different scope and cannot pass either gross check. A model with +110 and -50 passes the net check but fails both gross checks. A complete empty position set fails if any in-scope position is nonzero; a partial set never asserts omitted positions are zero.

For identity, start with singleton acquisition A and add confirming statement B. Explicit membership retains A's anchor even if B becomes authoritative for facts. Clean reconstruction from the final inputs, reversed acquisition order, and a prior incremental build produce the same event, leg, and root-lot IDs. Adding B without the required decision fails rather than selecting a new anchor. Revised values fail existing correction guards without re-identifying the event.

Also exercise explicit merge and split decisions: retired IDs remain addressable, redirects do not apply corrections automatically, contradictory membership and retirement cycles fail, and raw evidence persists. Reacquiring identical bytes in one source namespace adds no occurrence; binding identical bytes to distinct genuine contexts does not collapse them. An authored fact keeps its stable anchor when its payload revision changes.

## Reproducibility and preservation

| Case | Required outcome |
| --- | --- |
| Build twice with identical retained inputs | Identical text bytes and typed logical tables; no cache required |
| Change locale, working directory, process hash seed, or file enumeration order | No semantic or output change |
| Change tool/plugin code without changing its version label | Execution fingerprint changes or the build refuses an unverifiable producer |
| Upgrade only the Parquet writer | Execution fingerprint and possibly byte digest change; equal typed content has equal logical digest |
| Swap two differently named input roles or repeat an input edge | Execution identity distinguishes roles and multiplicity |
| Missing/corrupt vault object, acquisition record, plugin, or required model asset | Verification fails with a safe, specific diagnostic |
| Interrupt acquisition before or after writing the object | Original remains; retry verifies or completes one consistent accession |
| Acquire identical bytes twice for distinct contexts | One object may have multiple accession/binding records; no invented economic duplicate |
| Fail a check or interrupt publication | Prior complete generation remains published; attempted inputs are not represented as certified |
| Two commands attempt to publish or append simultaneously | Exclusive local write or explicit refusal; no interleaving or lost decision |
| Restore on a clean machine from the retained recovery set | No hidden keyring, cache, external path, or network dependency |
| Build, analyze, inspect, or diagnose with network access monitored and denied | No attempted telemetry, crash upload, update check, or other automatic connection |
| Explicitly request a supported fetch outside a build | Destination/purpose visible; only the requested fetch occurs; retained result becomes an input before any offline use |
| Change a schema or correct an old importer bug | Reviewed expected-result changes; old derived artifacts are rebuilt, not silently migrated |

## Sources, identity, and correction

Use a statement with opening 100.00, credit 20.00, debit 5.00, and closing 115.00. Independently assert both source and model reconciliation.

| Variation | Required outcome |
| --- | --- |
| Drop the debit during model merge | Source check passes, model check fails |
| Omit both a +5.00 and -5.00 transaction | Arithmetic can pass; occurrence/coverage checks remain independently required |
| Two identical purchases on one day | Both survive unless source identity proves they are repeated observations |
| Overlapping downloads plus a complete statement | Duplicates resolve with retained provenance; declared authority applies only to its covered scope |
| Two authoritative statements disagree | Review required; arrival order does not pick a winner |
| Reissue retracts an item; later statement instead posts a reversal | Retraction and new reversal produce different histories |
| Pending ID changes at posting, or pending transaction disappears | Pending activity never silently becomes a posted movement or duplicate |
| Same correction key survives but guarded source value changes | Correction is not applied blindly |
| Importer changes row ordering or adds an unrelated field | Stable targets remain stable where evidence permits; ambiguous bindings fail visibly |
| Two corrections conflict; later entry retracts one | Explicit journal order and retraction determine the result without deleting history |
| Rule stops matching or correction becomes redundant | Diagnostic, not loss of the recorded decision |
| An unclassified backlog remains | Working output exposes its status; a report requiring classification rejects it |
| A source field is unsupported, or a PDF cannot be parsed | Bytes can be archived; unsupported economic content is not certified as absent |
| Account number changes without a new economic account | Acquisition binding preserves account identity |
| A file contains several accounts | One extract retains all source sections; explicit bindings resolve each |
| Adopt an existing ledger mid-history | Opening inventory has explicit provenance; no invented bank evidence or automatic balance plug |
| Reuse a legacy importer through the deferred beangulp adapter | Same retained-source/extraction boundary; missing dates, status, assertions, and components remain explicit fidelity limits, not fabricated facts |

## Time, coverage, and assertions

| Case | Required outcome |
| --- | --- |
| 23:50 local at the end of a statement period | Correct source civil-period membership without UTC date substitution |
| Authorization abroad, posting at home, earlier value date | All stated dates survive; each check names its date convention |
| Security and cash legs settle on different dates; FX legs settle in different centres | Per-leg dates survive and assertions count each relevant leg at the correct boundary |
| Date only, offset timestamp, named-zone time, and unzoned time | Distinct representations; no invented midnight or offset |
| Daylight-saving gap/overlap, or historical tzdb revision | Preserve observation; an unresolved required conversion fails; ruleset changes invalidate execution |
| Source lacks economically significant same-day sequence | Commutative sums can proceed; order-dependent booking/assertions require a decision |
| One event has different trade and settlement instants, including leg-specific times | Each temporal role survives separately; explicit unknown or date override does not borrow another role's timestamp |
| Closed period with zero transactions | Covered interval, not a gap |
| Monthly, quarterly, and non-calendar statement cycles | Expected cadence and publication lag are explicit |
| Run the same snapshot tomorrow | Same coverage result; updating `as_of` is an explicit input change |
| Closed account or adoption later than account opening | No demand for statements outside the declared expectation window |
| Rolling window with only an ending balance | Point evidence, not a complete closed-period oracle |
| Accreting units without itemized movements | No fabricated transactions; point checks and unavailable continuity are reported |
| Auto-compounding share value without unit changes | Price change is not an unexplained quantity change |
| Complete holdings list omits a position the model holds | Complete-scope check fails even if all listed per-commodity checks pass |
| Statement closing balance rendered to Beancount | Beginning-of-day assertion date translated correctly |
| Intraday balances or balance quoted in another currency | Preserve scope/basis; unsupported sink assertion is not silently substituted |

## Numbers and rendering

| Case | Required outcome |
| --- | --- |
| Eighteen-decimal units, dust below display precision, large magnitude | Exact storage and logical round-trip, or explicit range rejection |
| Total cost 10 over 3 units | Stated total retained; derived per-unit amount and residual obey declared arithmetic |
| Positive and negative half-rounding, negative zero, mixed scales | Symmetric declared rounding; canonical value distinguished from stated scale |
| Fees allocated across several lots | Allocations plus explicit residual equal the original total |
| Source total and units-times-price disagree | Preserve both; report discrepancy rather than choosing silently |
| FX pair quoted in both directions with non-reciprocal rates | Two observations survive; no invented exact reciprocal |
| Concurrent mark, index, and last-trade quotes for one instrument/market/instant | Three typed `mark`/`index`/`trade` prices survive; valuation chooses its declared basis without fallback |
| Negative price, zero-value meaningful leg, multiplier-bearing contract | No generic positivity assumption or pruning; unsupported renderer rejects explicitly |
| Instrument ticker recycled, renamed, numeric, or punctuated | Stable identity separate from source aliases and valid rendered symbol |
| ISIN at one broker and proprietary code at another | Source-scoped alias resolution with ambiguity and validity-date checks |
| Unicode combining marks, emoji, and right-to-left text | Source text survives; canonical comparison rules are explicit |
| Thousands separators or authored arithmetic expressions | Format adapter follows an explicit grammar; no locale guessing or executable expression evaluation |
| Both cost and price stated, total price not exactly divisible | Both facts retained; event-specific weight and rendering rules tested |
| Opening balance, currency exchange, or disposal at gain | Explicit balancing semantics, not an unexplained residual or sink-selected lot |
| Same-day rendered directives | Deterministic total output order that does not override economic order |
| Render a supported event to the deferred hledger sink | Same upstream booking as Beancount and tables; unsupported representation is a named sink limitation, never a different match |

## Investments and contracts

The position, event, and lot schemas represent these cases. Booking and action processors implement their event-specific equations.

| Case family | Cases and expected distinction |
| --- | --- |
| Lot identity | Equal date/cost lots distinguished by broker label; partial disposal spanning three lots; source ordering changes; no correction rebound to an arbitrary duplicate |
| Booking rejection | Missing acquisition, insufficient quantity, ambiguous strict match, or undeclared short creation blocks booking |
| Matching alternatives | Broker allocation, declared FIFO, and jurisdictional pooling/rematching remain distinguishable; an upstream match cannot erase the alternatives |
| Backfill | Earlier acquisition, split, or revised opening changes dependent results; unaffected identities remain stable; guarded decisions are revalidated |
| Transfers | Full and partial moves between controlled accounts preserve acquisition and basis lineage; destination receipt is not a second acquisition |
| Transfer ordering | Disposal after arrival uses transferred inventory; unresolved outgoing/incoming correspondence does not realize a fabricated gain |
| Same-event complexity | Augmentation and reduction of one instrument, average-cost pooling, and fees split across lots require explicit semantics |
| Cash components | Gross foreign dividend, withholding, commission, regulatory fee, and accrued bond interest remain separate |
| Currency | Quote currency differs from settlement currency; all components survive without an upstream tax-gain currency choice |
| Options | Expiry without cash, short-put assignment, long-call exercise, and adjusted deliverables preserve lifecycle and premium components; capitalization is policy, not a universal fact |
| Futures | Daily settlement, multipliers, negative prices, original reference, and settlement-adjusted state remain distinct from owned assets at cost |
| Short sale | Borrowed security obligation, proceeds, borrow fee, substitute dividend, and loss on covering remain separate |
| Margin | Security holding plus debit balance, cross-margin collateral, and partial liquidation across positions are not netted into one position cost |
| Perpetuals | No expiry, repeated funding, inverse-denominated settlement, and venue-initiated liquidation carry explicit terms and stated events |
| Contract observations | Issuer-stated barrier event is evidence; deriving it from an absent intraday path is unsupported |
| Claims | ETN versus ETF, structured note, convertible bond, retirement-scheme interest, and exchange-failure claim retain relevant counterparties and terms without asserting a complete legal taxonomy |

### Corporate actions

| Case | Required outcome |
| --- | --- |
| Forward/reverse split with fractional units and cash in lieu | Ratio and residual/cash allocation reconcile; pre-event history is unchanged |
| Spinoff allocation published months later | New evidence/decision triggers replay and dependent-basis changes |
| Mixed cash-and-stock merger | Both consideration types and parent/child lineage survive |
| Return of capital exhausts basis | Source cash and basis components survive; jurisdictional excess-gain treatment is downstream |
| Action occurs after full disposal | No position is invented; a backdated action effective before disposal can change the result |
| Dividend reinvestment creates sub-cent lots | Exact quantities and totals preserved |
| Currency redenomination or symbol change | Economic conversion distinguished from alias-only change |

## Additional event families

- Crypto transfer with a fee in another asset; failed transfer that still consumes a fee: independent fee movement remains.
- Wallet transfer, wrapping, unwrapping, and bridging: mechanical events and declared correspondence remain separate from tax characterization.
- Same stablecoin symbol on several chains: chain/contract identities are not collapsed by symbol or a reporting preference.
- Airdrop and fork without stated cost/allocation: missing basis is not automatically zero; unsupported determination remains explicit.
- Liquidity-pool deposit and withdrawal: original assets, pool units, and changed return quantities are all retained.
- NFT: contract address plus token identity prevents fungible aggregation.
- Chain reorganization or disputed settlement reversed later: evidence revision and lifecycle reversal are distinguished.
- Prediction outcome contracts: separate instruments for each outcome, declared complete-set relationship, splitting/merging collateral, and externally stated resolution.
- Stablecoin settlement: the settlement leg is itself an instrument rather than an assumed fiat currency.
- Amortizing loan without stated principal/interest split: no invented amortization schedule; declared contract calculation is a separately gated capability.
- Remortgage: old obligation and new relationship linked explicitly, not identified by a reused label.

## Analysis and sharing

The [analysis boundary](./analysis-boundary.md) owns consumer requirements. Its implementation must demonstrate:

- Multi-table publication is one immutable snapshot; mixed build IDs, incompatible schemas, broken references, and duplicate keys fail.
- A joint account arrives through different formats and periods: explicit authority counts it once, compares compatible overlaps, and rejects an ambiguous authority gap or overlap.
- Authority switches between invested feeds: lot lineage and opening/closing state are reconciled, not spliced by date alone.
- Identical local IDs in independent projects: composite entity keys prevent collision without implying equivalence.
- Group membership and ownership change over time: attribution and internal-flow elimination use explicit effective intervals.
- Missing/stale prices, competing price bases, or missing FX: valuation is unavailable rather than zero or a partial total presented as complete.
- Economic booking differs from tax matching: original flows remain sufficient to derive the alternative.
- A declaration target survives by key but changes materially: guarded resolution fails until reviewed.
- A filtered report excludes an acquisition: it aggregates authoritative results without rebooking a partial history.
- Jurisdiction models satisfy the [release gate](./project-topology.md#jurisdiction-package-release-gate): authoritative validation, reviewed filing cycle, independent second use, and scoped release evidence; failure keeps them private without blocking private implementation.

## Contract coverage

The requirements-to-owner/check map and schema-freeze gate live in [delivery step 0](../plans/delivery.md#0-establish-and-exercise-the-full-contract). They require representations and independent expected results across this entire inventory, not just cash or the worked happy paths. Later stages make the corresponding processors executable. Source acquisition can retain evidence before its processor is available; attempting to publish results that require that processor reports the missing capability.

Hand-calculate financial expectations, check conservation independently, vary input order, and include counterexamples where internally consistent output is still wrong. Update expected artifacts only after explaining the semantic change.
