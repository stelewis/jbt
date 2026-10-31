# Civil Time, Coverage, and Account Lifecycle

Coverage answers whether the evidence satisfies an account's declared statement expectations at the committed `as_of`. It does not infer completeness from transaction counts, a reconciled closing balance, or the execution clock.

## Civil dates and reproducibility

Source observations retain their original clocks, offsets, zones, and precision. Runtime resolves each temporal role against its applicable account revision and civil zone before statement membership and booking. Posting determines a recognized movement's binding; initiation determines a pending movement's binding. Observation order cannot choose a policy or resolve an ambiguous binding. A date-only observation stays a civil date; it is not midnight UTC. An exact-offset clock may cross a date boundary when converted into the account zone.

Named zones load only from the Python `tzdata` distribution. The retained input selection and published `inputs.json` record the distribution version, IANA version, digest of the zone rules, and gap/overlap policies. Source/model cache identities include those facts. Checked replay rejects different rules before consulting stage caches; reading an old report does not require the original environment.

A nonexistent local clock fails the conversion that needs it. An overlapping clock requires an explicit offset agreeing with the named zone. An unresolved supplementary role produces a warning and no canonical date for that role; it does not discard an independently resolvable posting. A required binding or statement-membership role blocks its movement when unresolved. Original evidence remains retained; no failure causes a guessed date or a rounded instant.

Explicit account-civil midnight remains evaluable at minute, second, or zero-fraction precision through source checks, model checks, and ledger rendering. A ledger point assertion additionally requires no recognized movement in the affected position on that civil day, because a date-only ledger cannot represent their intraday order.

## Expected periods

An account declares its cadence, civil zone, coverage start, nonnegative publication lag, and registered coverage bases. Coverage start is an adoption commitment, not an account opening or an inferred acquisition date.

Account opening, closure, `as_of`, and explicit statement endpoints are inclusive civil dates. Declaration validity and projected coverage segments are half-open intervals. Thus a statement covering January 1–31 projects as `[2026-01-01, 2026-02-01)`. Its calendar expectation becomes due on January 31 plus publication lag.

- `monthly`, `quarterly`, and `annual` use civil calendar boundaries. Adoption clips the first period; account closure clips the last. A policy revision partitions the applicable expectation without closing the account or bringing the calendar publication deadline forward.
- `irregular` uses ordered, nonoverlapping explicit `{start, end, due}` periods. An explicit due date can delay publication; it cannot bring the deadline before the period's inclusive end plus publication lag.
- `monthly_cycle` uses `period_rule:monthly_cycle`, `cycle_end_day` in 1–31, and `closing_tolerance_days` in 0–13. The nominal closing day clamps to the month length. The tolerance limit keeps adjacent cycle-closing windows disjoint even in February. Other period-rule variants reject cycle fields.
- Accounts without expected statements use `period_rule:none` and no expected periods.

Each due calendar or explicit period is covered by the union of complete, live movement claims in the same account, basis, and date role. An empty complete statement counts. A balance point, a partial movement interval, or an eclipsed/replaced segment does not.

For example, claims `[January 1, January 10)` and `[January 11, February 1)` leave exactly `[January 10, January 11)` uncovered. Overlapping claims count once.

## Card cycles

A full cycle becomes due on its nominal closing date plus tolerance and publication lag. Exactly one complete statement must close within that cycle's tolerance window. The actual statement chain must remain contiguous from the applicable coverage start through its latest actual end, with no overlapping cycles.

If a January 31 cycle allows one day's tolerance, a statement closing January 30 covers that cycle without inventing a January 31 gap while the following statement is not due. The next statement must start January 31, immediately after the previous inclusive close. A missing cycle, duplicate close, overlap, or one-day discontinuity fails.

Adoption and declaration validity intersect cycle periods rather than selecting nominal closing dates. A revision ending January 16 retains the preceding policy's `[January 1, January 16)` obligation and original nominal deadline. A revision or account closure that cuts a cycle short requires complete, nonoverlapping coverage through that boundary, not a fictional nominal close outside the applicable interval. A closure's final fragment becomes due on the inclusive closure date plus tolerance and publication lag, even if adoption and closure both fall between nominal closes.

## Published evidence and findings

`statement_coverage` publishes movement claims after scoped revision and authority resolution. Its local key is `(statement_id, part_key, coverage_basis, period_start)`; fields are `date_role`, half-open `period_start`/`period_end`, `is_complete`, and `is_eclipsed`. It references `statements`; it does not flatten independent bases or apply an envelope-wide replacement to unrelated evidence.

The independent check engine reads these projected claims, effective account declarations, and the recorded horizon. Coverage findings identify account and policy revisions; their expected text value contains the basis and exact half-open period, and their observed text value contains exact gaps, the comparison reason, and the matching-statement count for cycles.

Coverage runs even when no output requires it. Missing due evidence is `fail`, not `not_evaluable`: the expectation is known. A working record and its summary retain the failure. Adding `coverage` to an output's `required_checks` makes every coverage result a publication requirement; rejection preserves the preceding generation and retains the failed attempt for review.

Account lifecycle is mandatory independently of optional output checks. Recognized movements use their resolved posting date; pending evidence uses its retained initiation date rather than an invented posting date. Movements outside the applicable account opening/closure dates fail; an unavailable required date is not evaluable. Declaration revision boundaries do not become holding lifetimes. Beancount positions use the latest account revision starting on or before `as_of`, even after that revision expires; positions remain open across policy revisions. Their close directive uses the day after the account's inclusive closure date, allowing the final day's closing balance assertion to render at its next-day comparison boundary.
