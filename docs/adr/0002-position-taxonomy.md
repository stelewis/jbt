---
id: 0002
title: "Position Taxonomy: Property, Claims, and Contracts"
status: accepted
date: 2026-08-19
tags: [positions, claims, derivatives, margin, counterparty]
supersedes: null
superseded_by: null
---

## Context

A single signed quantity held at cost cannot faithfully represent every financial position. Selling borrowed shares creates cash and an obligation to return shares, not ownership of negative shares. A cash-settled future is an agreement measured against a reference, not an asset purchased for its notional value. A broker cash balance is a claim against a counterparty; banknotes held directly are not.

This distinction precedes investment booking. Cash, securities, debt, shorts, and derivatives belong to the same model; matching rules and sink formats must not decide what exists.

## Decision

### Position, instrument, and account

The model has three position kinds:

| Kind | Relationship represented | Sign and counterparty |
| --- | --- | --- |
| `property` | A thing held directly | Nonnegative ownership; no obligor is invented |
| `claim` | A right to receive or duty to deliver an amount or quantity | Positive right or negative obligation against a named counterparty |
| `contract` | An agreement with payoff and lifecycle terms | Direction or contractual side, with a named counterparty |

An obligation is a negative claim, not a fourth kind. A short sale creates a share-delivery obligation and a separate cash claim. Long and short options are contracts; sign and terms distinguish their rights, obligations, premium flows, and asymmetric risk. A contract's notional is neither an owned asset nor an acquisition cost.

An instrument identifies what is traded; a position records a particular account's relationship to it. An account is a statement and reconciliation boundary, not a single position kind or counterparty. It may contain securities, cash claims, debt, and contracts at once. Classification follows the actual holding and custody arrangement, not just the ticker: a security held directly and an entitlement held through a custodian may have different legal exposure. Where the source and declarations do not establish that relationship, the model must not invent it.

Income, expense, and equity categories classify boundary legs and have their own identities; they are not accounts with statements or positions. A position leg names its custody account, while a boundary leg names a category or remains explicitly unclassified. Ledger roots and paths are a sink mapping over these distinct records, not the financial model's account taxonomy.

Counterparty and custody relationships come from evidence or explicit reviewed declarations. Each position has a stable declared identity and source bindings. Instrument defaults may supply terms, but cannot replace a position's actual lender or custodian. A broker, stock lender, and issuer can be different parties even within one account. Assets, debt, and collateral remain separate positions; net exposure is an analysis, not destructive netting of the record.

### Measurement and lifecycle

Positions carry the measurement appropriate to their terms:

| Measurement | Required information | Example |
| --- | --- | --- |
| Acquisition cost | Amount and currency, acquisition date, units, attributable fees | Purchased shares, a purchased note, an option premium |
| Principal or quantity owed | Amount or quantity, denomination, direction, counterparty, terms | Bank deposit, loan, borrowed shares |
| Entry reference | Reference value and denomination, entry/reset date, units, multiplier and settlement terms | A reference-settled future or forward |

Reference measurement is for contracts, not for property acquired at an index-linked price. A purchased exchange-traded note can have an acquisition cost while its market value follows an index. Principal and economic acquisition cost can coexist on a claim; they answer different questions. No rule says every claim was bought or every contract has a reference price. Missing acquisition basis remains explicit if later gain determination needs it; zero cannot stand in for unknown.

Events determine changes in position state. A future's daily variation margin may be **settlement of gain**, resetting its remaining entry reference, or a **collateral movement** that leaves the reference intact; the terms and source evidence must decide which. Booking the entire opening-to-closing gain again after daily settlement would count it twice. Options exercise and assignment can create underlying positions, but no market-price inference may silently manufacture an unstated lifecycle event. If a necessary event or term is unknown, the relevant determination fails rather than becoming a guess in a sink.

### Worked cases

- **Cash-settled future:** open five contracts at 4,500 with multiplier 50 and post 25,000 initial margin. Record the contract and the margin asset or claim according to its custody terms, not 1,125,000 of purchased property. Under daily-settlement terms, a move to 4,540 settles 10,000 and resets the reference; closing at 4,560 settles another 5,000. Under collateral-only variation terms, record those collateral transfers separately and settle the 15,000 contract gain once at close, with collateral release accounted for separately.
- **Margin-funded long:** buy 100 shares at 50 with 2,500 borrowed. Preserve the 100-share position and the 2,500 debt, with financing cash movements and subsequent interest separately traceable. A net value of 2,500 is not the inventory.
- **Short sale:** sell 100 borrowed shares at 50. Record a 100-share delivery obligation to the lender and 5,000 cash proceeds at the broker. Covering at 40 discharges the obligation and yields 1,000 before fees. Borrow fees and substitute dividends remain separate linked movements; they do not turn the obligation into negative ownership.
- **Options:** preserve premium paid or received when opening. Evidenced expiry closes the contract; cash settlement records its payment; exercise or assignment links the terminated contract to delivery of the underlying and strike cash. Carrying premium into underlying economic cost follows the declared booking treatment, not an assumed universal tax rule.
- **Cash and custody:** 1,000 in banknotes is property; 1,000 deposited at a bank is a claim. An ETF interest and an unsecured index-linked note are not interchangeable because they track the same index; custody and issuer obligations determine the relationship, not ticker similarity.

### Determination boundary

The model records and applies evidenced lifecycle events, including expiry, exercise, assignment, default, barrier activation, and autocall events. It does not infer a barrier breach from an unstated intraday price path or price an illiquid structured contract to manufacture missing events. Contract terms use established domain vocabulary, including the ACTUS data dictionary where its meaning fits, rather than conflating payoff, settlement, and basis. Market-path inference and contract cash-flow simulation belong to a separately validated downstream engine; adopting a standard vocabulary does not require a pricing-engine dependency.

Sinks may flatten these distinctions into account paths and metadata. They must not change the upstream classification or imply a notional holding absent from the model. Valuation, counterparty exposure, and risk calculations remain analysis questions.

## Consequences

Booking and reconciliation operate on typed positions and evidenced movements rather than one universal signed inventory. Assertions remain within an account but distinguish position-level scope from explicitly net scope, with a declared measurement and recognition basis. Gross claims against different counterparties cannot be checked by one net currency total; a cash balance cannot validate a contract's notional or reference state. The [tabular contract](../design/analysis-boundary.md#balances-prices-and-actions) owns scope fields and completeness checks.

Declared counterparty, custody, and contract terms create input obligations but make debts, short obligations, collateral, and derivative lifecycles representable. Kind alone is not a risk calculation, and recording an event does not certify an unstated market-path inference.

## Alternatives considered

### One signed quantity, with permissions for negative holdings

Rejected: permission does not turn borrowed shares into owned property or a derivative notional into an acquired asset.

### Separate obligations or commitments into more position kinds

Rejected: signed claims express duties to deliver. Pending authorizations, unsettled trades, and scheduled forecasts need their own event status or date, not an additional kind of holding.

### Replace accounts with a counterparty graph

Rejected: accounts remain the institution's statement, balance, and coverage boundary. Exposure aggregation and collateral netting sets are downstream analyses, not substitutes for that boundary.

### A full pricing and exposure engine

Rejected: determining unstated contract events or risk from market data requires separate inputs and policies. It is not an evidence-preserving build operation.

## Related

- [Booking and lot identity](0003-booking-and-lot-identity.md)
- [Corporate actions](0005-corporate-actions.md)
