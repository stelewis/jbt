import json
from collections import defaultdict
from copy import deepcopy
from datetime import date
from fractions import Fraction
from hashlib import sha256

import pytest
from tests.integration.conformance.corpus import (
    FixtureCase,
    _Bundle,
    coverage_index,
    fixture_cases,
    load_case,
)

from jbt.contracts.catalog import catalog
from jbt.contracts.declarations import validate_declaration
from jbt.contracts.publication import _active_declarations
from jbt.contracts.validation import (
    ContractError,
    validate_source_sequence_directions,
    validate_tables,
)
from jbt.domain.canonical import encode_json
from jbt.domain.errors import IdentityError
from jbt.domain.identity import (
    AcquiredAnchor,
    IdentityDecision,
    IdentityReview,
    Occurrence,
    Retirement,
    child_id,
    declaration_id,
    revision_id,
    successor_lot_id,
    validate_membership,
    validate_transition,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey

pytestmark = [pytest.mark.integration, pytest.mark.golden]
CASE_IDS = tuple(case.case_id for case in fixture_cases())


def _number(row: dict, field: str) -> Fraction | None:
    coefficient = row[field + "_coefficient"]
    if coefficient is None:
        return None
    return Fraction(int(coefficient), 10 ** row[field + "_scale"])


def _known_number(row: dict, field: str) -> Fraction:
    value = _number(row, field)
    assert value is not None, field
    return value


def _text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _inventory(case: FixtureCase) -> dict:
    totals: dict[str, dict[str, Fraction | None]] = defaultdict(
        lambda: defaultdict(Fraction)
    )
    for row in case.tables["inventory_changes"]:
        position_id = row["position_id"]
        assert isinstance(position_id, str)
        for component in ("units", "principal", "capitalized_fee", "expensed_fee"):
            value = _number(row, component + "_delta")
            previous = totals[position_id][component]
            totals[position_id][component] = (
                None if value is None or previous is None else previous + value
            )
    return totals


def test_account_objects_preserve_defaults_and_irregular_periods() -> None:
    periods = [{"start": "2026-11-02", "end": "2026-11-08", "due": "2026-11-10"}]
    bundle = _Bundle(
        {
            "case_id": "synthetic-account",
            "expected_answers": {},
            "oracle_notes": [],
            "events": [],
            "commodities": [],
            "accounts": [
                {"id": "A"},
                {
                    "id": "P",
                    "fields": {
                        "opened_date": "2026-10-20",
                        "closed_date": "2026-11-08",
                        "statement_cadence": "irregular",
                        "civil_zone": "America/New_York",
                        "coverage_start_date": "2026-10-20",
                    },
                    "expected_periods": periods,
                    "publication_lag_days": 2,
                    "period_rule": "explicit",
                },
            ],
            "categories": {},
            "policies": [],
            "positions": [],
        }
    )
    bundle.dimensions()
    ordinary, irregular = bundle.rows["accounts"]
    assert ordinary["statement_cadence"] == "monthly"
    assert ordinary["closed_date"] is None
    payload = bundle.payloads["declaration:P"]
    validate_declaration(payload)
    assert all(
        payload[name] == value
        for name, value in irregular.items()
        if name not in {"entity_id", "declaration_id"}
    )
    assert payload["expected_periods"] == periods
    assert payload["publication_lag_days"] == 2
    assert payload["period_rule"] == "explicit"
    complete = bundle.finish()
    assert complete.tables["build"][0]["as_of"] == "2026-12-31"
    validate_tables(complete.tables)


def test_declaration_and_successor_ids_match_domain_without_display_labels() -> None:
    merger = load_case("mixed-merger")
    pools = load_case("pools")
    for case in (merger, pools):
        declarations = {
            row["declaration_id"]: row for row in case.tables["declarations"]
        }
        entity = EntityId(_text(case.tables["build"][0]["entity_id"]))
        for table, field, kind in (
            ("positions", "position_id", RecordKind.POSITION),
            ("inventory_pools", "pool_id", RecordKind.POOL),
        ):
            for row in case.tables[table]:
                key = _text(declarations[row["declaration_id"]]["declaration_key"])
                assert (
                    row[field] == declaration_id(entity, kind, SemanticKey(key)).value
                )
    entity = EntityId(_text(merger.tables["build"][0]["entity_id"]))
    root = child_id(
        RecordId(entity, RecordKind.LEG, merger.aliases["merger-q"]),
        RecordKind.LOT,
        SemanticKey("pre-merger"),
    )
    assert merger.aliases["parent"] == root.value
    expected = successor_lot_id(
        root,
        RecordId(entity, RecordKind.EVENT, merger.aliases["merger"]),
        SemanticKey("merger-stock-output"),
    )
    assert merger.aliases["child"] == expected.value
    renamed = _Bundle({"case_id": "renamed-display"})
    renamed.aliases["parent"] = root.value
    renamed.lot_roots["parent"] = root.value
    renamed.step_events["step:merger"] = merger.aliases["merger"]
    renamed.add(
        "lots",
        lot_id="@child",
        parent_lot_id="@parent",
        creation_step_id="@step:merger",
        origin_key="merger-stock-output",
        broker_lot_id="Renamed display label",
    )
    assert renamed.aliases["child"] == expected.value
    altered = deepcopy(merger.tables)
    child = next(
        row for row in altered["lots"] if row["lot_id"] == merger.aliases["child"]
    )
    child["origin_key"] = "different-output-role"
    with pytest.raises(ContractError, match="lot_identity"):
        validate_tables(altered)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_complete_specimen_validates_real_tables_and_declarations(case_id: str) -> None:
    case = load_case(case_id)
    validate_tables(case.tables)
    for declaration in case.tables["declarations"]:
        payload = declaration["payload_json"]
        assert isinstance(payload, str)
        validate_declaration(json.loads(payload))
    assert set(case.tables) == {table.name for table in catalog()}
    assert case.oracle_notes
    assert case.domain_records


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_retained_invented_evidence_matches_every_source_digest(case_id: str) -> None:
    case = load_case(case_id)
    for source in case.tables["source_records"]:
        text = case.evidence["blobs"][source["source_blob_digest"]]
        assert text.startswith("INVENTED")
        assert sha256(text.encode()).hexdigest() == source["source_blob_digest"]


def test_declaration_revision_bytes_agree_for_null_and_dated_starts() -> None:
    case = load_case("cash")
    declaration = case.tables["declarations"][0]
    payload_json = declaration["payload_json"]
    entity_id = declaration["entity_id"]
    declaration_key = declaration["declaration_key"]
    assert isinstance(payload_json, str)
    assert isinstance(entity_id, str)
    assert isinstance(declaration_key, str)
    payload = json.loads(payload_json)
    entity = EntityId(entity_id)
    key = SemanticKey(declaration_key)
    unbounded = revision_id(
        entity, key, effective_from=None, effective_to=None, payload=payload
    )
    dated = revision_id(
        entity,
        key,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        payload=payload,
    )
    assert declaration["valid_from"] is None
    assert (
        declaration["declaration_id"]
        == declaration["revision_digest"]
        == unbounded.value
    )
    assert dated.value != unbounded.value
    for valid_from, identity in ((None, unbounded), ("2026-01-01", dated)):
        meaning = {
            "entity_id": entity.value,
            "declaration_key": key.value,
            "valid_from": valid_from,
            "valid_to": None,
            "kind": payload["kind"],
            "schema_version": payload["schema_version"],
            "payload": payload,
        }
        assert (
            identity.value
            == sha256(encode_json(meaning, integer_strings=False)).hexdigest()
        )
    changed = deepcopy(case.tables)
    changed["declarations"][0]["valid_from"] = "2026-01-01"
    with pytest.raises(ContractError, match="declaration_revision_identity"):
        validate_tables(changed)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_recorded_deltas_replay_to_hand_calculated_inventory(case_id: str) -> None:
    case = load_case(case_id)
    totals = _inventory(case)
    for alias, expected in case.expected_answers.get("inventory", {}).items():
        actual = totals[case.aliases[alias]]
        for component, value in expected.items():
            assert actual[component] == (None if value is None else Fraction(value)), (
                case_id,
                alias,
                component,
            )


def test_cash_source_and_model_reconciliation_have_distinct_failure_modes() -> None:
    case = load_case("cash")
    cash = case.aliases["cash-A"]
    movements = {
        row["posting_id"]: _known_number(row, "amount")
        for row in case.tables["postings"]
        if row["position_id"] == cash
    }
    assert sum(movements.values()) == Fraction(115)
    assert sum(
        value for key, value in movements.items() if key != case.aliases["debit-cash"]
    ) == Fraction(120)
    assert Fraction(100) + 20 - 5 + 5 - 5 == Fraction(115)
    assert len(movements) == 3


def test_fee_policy_changes_book_result_not_original_component_lineage() -> None:
    expensed = load_case("compound-expensed")
    capitalized = load_case("compound-capitalized")
    assert expensed.aliases["L"] == capitalized.aliases["L"]
    assert expensed.aliases["buy"] == capitalized.aliases["buy"]
    assert Fraction(40) - 25 == Fraction(
        expensed.expected_answers["disposal"]["gross_result"]
    )
    assert Fraction(40) - 25 - Fraction("0.50") == Fraction(
        capitalized.expected_answers["disposal"]["gross_result"]
    )
    assert expensed.expected_answers["disposal"]["net_performance"] == "13.50"
    assert capitalized.expected_answers["disposal"]["net_performance"] == "13.50"


def test_backfill_preserves_acquisition_and_changes_only_dependent_components() -> None:
    original = load_case("compound-expensed")
    revised = load_case("compound-backfill")
    assert original.aliases["buy"] == revised.aliases["buy"]
    assert original.aliases["L"] == revised.aliases["L"]
    assert original.aliases["split"] != revised.aliases["split"]
    assert _inventory(revised)[revised.aliases["AQ"]]["principal"] == 80
    assert _inventory(revised)[revised.aliases["BQ"]]["principal"] == Fraction("7.50")


def test_identical_transfer_quantity_does_not_determine_transferred_principal() -> None:
    first = load_case("multi-origin")
    alternative = load_case("multi-origin-alternative")
    a = _inventory(first)[first.aliases["BQ"]]
    b = _inventory(alternative)[alternative.aliases["BQ"]]
    assert a["units"] == b["units"] == 6
    assert a["principal"] == 100
    assert b["principal"] == 90
    assert first.tables["disposals"] == alternative.tables["disposals"] == []
    assert len(first.tables["links"]) == len(alternative.tables["links"]) == 2


def test_pool_reader_retains_contributors_without_selecting_tax_origins() -> None:
    case = load_case("pools")
    totals = defaultdict(lambda: defaultdict(Fraction))
    for row in case.tables["inventory_changes"]:
        key = row["pool_id"] or row["lot_id"]
        totals[key]["units"] += _known_number(row, "units_delta")
        totals[key]["principal"] += _known_number(row, "principal_delta")
    for pool, expected in case.expected_answers["pools"].items():
        for field, value in expected.items():
            assert totals[case.aliases[pool]][field] == Fraction(value)
    assert totals[case.aliases["L1"]]["units"] == 0
    assert totals[case.aliases["L2"]]["units"] == 0
    sale = next(
        row
        for row in case.tables["inventory_changes"]
        if row["change_id"] == case.aliases["p-c7"]
    )
    assert sale["lot_id"] is None
    assert sale["pool_id"] == case.aliases["P"]
    foreign_root = next(
        row for row in case.tables["lots"] if row["lot_id"] == case.aliases["LEUR"]
    )
    assert foreign_root["book_commodity_id"] == case.aliases["EUR"]


def test_recorded_reference_settlements_are_not_calculated_again_by_reader() -> None:
    case = load_case("reference")
    settlements = [
        _known_number(row, "amount") for row in case.tables["reference_settlements"]
    ]
    assert sorted(settlements) == [20, 30]
    assert sum(settlements) == 50
    collateral = sum(
        _known_number(row, "amount")
        for row in case.tables["postings"]
        if row["position_id"] == case.aliases["margin"]
    )
    assert collateral == 160
    assert (
        sum(
            _known_number(row, "units_delta")
            for row in case.tables["inventory_changes"]
        )
        == 0
    )
    quotes = {row["basis"]: _known_number(row, "rate") for row in case.tables["prices"]}
    assert quotes == {"mark": 103, "index": 102, "trade": 104}
    assert 2 * quotes["mark"] * 10 == 2060
    assert "settlement" not in quotes


def test_gross_assertions_do_not_certify_netting_counterexamples() -> None:
    case = load_case("brokerage")
    gross = [Fraction(value) for value in case.expected_answers["opening_gross"]]
    false = [Fraction(value) for value in case.expected_answers["false_gross_same_net"]]
    assert sum(gross) == sum(false) == 60
    assert gross[0] != false[0]
    assert gross[1] != false[1]
    by_scope = {
        row["assertion_set_id"]: row["scope_id"]
        for row in case.tables["balance_assertions"]
    }
    by_kind = {
        row["scope_id"]: row["scope_kind"] for row in case.tables["assertion_scopes"]
    }
    for row in case.tables["balances"]:
        assert (row["position_id"] is None) == (
            by_kind[by_scope[row["assertion_set_id"]]] == "account_net"
        )


def test_complete_empty_account_and_full_position_enumeration() -> None:
    empty = load_case("empty-scope")
    validate_tables(empty.tables)
    assertion = next(
        row
        for row in empty.tables["balance_assertions"]
        if row["assertion_set_id"] == empty.aliases["empty-close"]
    )
    assert assertion["is_complete"]
    assert not [
        row
        for row in empty.tables["balances"]
        if row["assertion_set_id"] == assertion["assertion_set_id"]
    ]
    assert not [
        row
        for row in empty.tables["positions"]
        if row["account_id"] == empty.aliases["C"]
    ]
    selected_zero = [
        row
        for row in empty.tables["balances"]
        if row["assertion_set_id"] == empty.aliases["zero-close"]
    ]
    assert len(selected_zero) == 2
    assert all(_known_number(row, "amount") == 0 for row in selected_zero)
    omitted_zero = deepcopy(empty.tables)
    omitted_zero["balances"] = [
        row
        for row in omitted_zero["balances"]
        if row["position_id"] != empty.aliases["AR"]
    ]
    omitted_zero["provenance"] = [
        row
        for row in omitted_zero["provenance"]
        if row["record_kind"] != "balances"
        or json.loads(_text(row["record_id"]))[0] != empty.aliases["zero-R"]
    ]
    with pytest.raises(ContractError, match="complete_balance_scope"):
        validate_tables(omitted_zero)

    gross = load_case("brokerage")
    validate_tables(gross.tables)
    selected = next(
        row
        for row in gross.tables["assertion_scopes"]
        if row["scope_id"] == gross.aliases["gross"]
    )
    gross_rows = [
        row
        for row in gross.tables["balances"]
        if row["assertion_set_id"] == gross.aliases["gross-close"]
    ]
    position_ids = selected["position_ids"]
    assert isinstance(position_ids, list)
    assert {row["position_id"] for row in gross_rows} == set(position_ids)
    omitted = deepcopy(gross.tables)
    omitted["balances"] = [
        row
        for row in omitted["balances"]
        if row["position_id"] != gross.aliases["debt"]
    ]
    with pytest.raises(ContractError, match="complete_balance_scope"):
        validate_tables(omitted)
    falsely_empty = deepcopy(empty.tables)
    falsely_empty["assertion_scopes"][0]["account_id"] = empty.aliases["A"]
    with pytest.raises(ContractError, match="complete_balance_scope"):
        validate_tables(falsely_empty)


def _correction_stage(case: FixtureCase, alias: str, amount: str) -> dict:
    stage = deepcopy(case.tables)
    active_id = case.aliases["declaration:" + alias]
    replacements = {
        case.aliases["credit-cash"]: amount,
        case.aliases["credit-income"]: "-" + amount,
    }
    for table in ("postings", "posting_weights"):
        for row in stage[table]:
            replacement = replacements.get(row["posting_id"])
            if replacement is not None:
                _change_number(row, "amount", replacement)
    if alias != "retract-credit":
        for row in stage["provenance"]:
            if row["record_kind"] == "postings" and row["field_name"] == "amount":
                posting = json.loads(_text(row["record_id"]))[0]
                if posting in replacements:
                    row["value_origin"] = (
                        "correction"
                        if posting == case.aliases["credit-cash"]
                        else "booking"
                    )
                    row["evidence_kind"] = "declaration"
                    row["evidence_id"] = active_id
                    if posting == case.aliases["credit-income"]:
                        row["derivation_id"] = "booked-counterpart-from-correction-v1"
            elif (
                row["record_kind"] == "posting_weights"
                and row["field_name"] == "amount"
                and json.loads(_text(row["record_id"]))[0] in replacements
            ):
                row["evidence_kind"] = "declaration"
                row["evidence_id"] = active_id
                row["derivation_id"] = "booked-weight-from-correction-v1"
    return stage


def test_guarded_correction_history_replays_complete_replacements_and_retraction() -> (
    None
):
    case = load_case("correction-history")
    validate_tables(case.tables)
    original_event = case.aliases["credit"]
    original_leg = case.aliases["credit-cash"]
    corrections = ("correct-credit", "replace-credit", "retract-credit")
    for alias, amount, expected in zip(
        corrections,
        ("25", "22", "20"),
        case.expected_answers["correction_cash"],
        strict=True,
    ):
        assert Fraction(expected) == 100 + Fraction(amount) - 5
        stage = _correction_stage(case, alias, amount)
        active_id = case.aliases["declaration:" + alias]
        validate_tables(stage)
        _active_declarations(
            {
                "effective_declarations": [
                    {
                        "entity_id": stage["build"][0]["entity_id"],
                        "declaration_id": active_id,
                    }
                ]
            },
            stage,
        )
        assert case.aliases["credit"] == original_event
        assert case.aliases["credit-cash"] == original_leg
        cash = sum(
            _known_number(row, "amount")
            for row in stage["postings"]
            if row["position_id"] == case.aliases["cash-A"]
        )
        assert cash == Fraction(expected)
    conflict = {
        "effective_declarations": [
            {
                "entity_id": case.tables["build"][0]["entity_id"],
                "declaration_id": case.aliases["declaration:" + alias],
            }
            for alias in ("correct-credit", "replace-credit")
        ]
    }
    with pytest.raises(ContractError, match="active_correction_chain"):
        _active_declarations(conflict, case.tables)
    bad_prior = deepcopy(case.tables)
    replacement = next(
        row
        for row in bad_prior["declarations"]
        if row["declaration_id"] == case.aliases["declaration:replace-credit"]
    )
    payload = json.loads(_text(replacement["payload_json"]))
    payload["prior_declaration_id"] = case.aliases["declaration:book"]
    replacement["payload_json"] = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ContractError, match="correction_prior_reference"):
        _active_declarations({"effective_declarations": []}, bad_prior)
    changed = deepcopy(case.tables)
    credit = next(
        row
        for row in changed["postings"]
        if row["posting_id"] == case.aliases["credit-cash"]
    )
    credit["commodity_id"] = case.aliases["EUR"]
    with pytest.raises(ContractError, match="guard_projection_changed"):
        _active_declarations(
            {
                "effective_declarations": [
                    {
                        "entity_id": case.tables["build"][0]["entity_id"],
                        "declaration_id": case.aliases["declaration:correct-credit"],
                    }
                ]
            },
            changed,
        )


def test_reviewed_merge_split_retains_retirement_without_reusing_old_identity() -> None:
    case = load_case("identity-history")
    validate_tables(case.tables)
    entity = EntityId(_text(case.tables["build"][0]["entity_id"]))
    names = ("old-m", "canonical", "split-new")
    sources = {row["source_record_id"]: row for row in case.tables["source_records"]}
    occurrences = {}
    for name in names:
        source = sources[case.aliases["source:" + name]]
        occurrences[name] = Occurrence(
            AcquiredAnchor(
                _text(source["source_blob_digest"]),
                SemanticKey("synthetic-fixture"),
                SemanticKey(_text(source["source_section"])),
                SemanticKey(_text(source["record_locator"])),
            ),
            SemanticKey(name),
        )
    retained = tuple(occurrences.values())
    original = validate_membership(entity, retained, (), required_correspondences=())
    merged = validate_membership(
        entity,
        retained,
        (
            IdentityDecision(
                occurrences["canonical"],
                (occurrences["canonical"], occurrences["old-m"]),
                (SemanticKey("reviewed-merge"),),
            ),
        ),
        required_correspondences=((occurrences["canonical"], occurrences["old-m"]),),
    )
    identifiers = {
        name: RecordId(entity, RecordKind.EVENT, case.aliases[name]) for name in names
    }
    retirement = Retirement(identifiers["old-m"], identifiers["canonical"])
    validate_transition(
        original,
        merged,
        (retirement,),
        prior_retirements=(),
        review=IdentityReview(
            frozenset(identifiers.values()),
            frozenset(identifiers.values()),
        ),
    )
    split = validate_membership(
        entity,
        retained,
        (
            IdentityDecision(
                occurrences["split-new"],
                (occurrences["old-m"], occurrences["split-new"]),
                (SemanticKey("reviewed-split"),),
            ),
        ),
        required_correspondences=((occurrences["old-m"], occurrences["split-new"]),),
    )
    validate_transition(
        merged,
        split,
        (),
        prior_retirements=(retirement,),
        review=IdentityReview(
            frozenset(identifiers.values()),
            frozenset(identifiers.values()),
        ),
    )
    with pytest.raises(IdentityError, match="unreviewed_affected_targets"):
        validate_transition(
            merged,
            split,
            (),
            prior_retirements=(retirement,),
            review=IdentityReview(frozenset(identifiers.values()), frozenset()),
        )
    assert {
        member.event
        for member in split
        if member.occurrence in (occurrences["old-m"], occurrences["split-new"])
    } == {identifiers["split-new"]}
    assert {
        row["record_id"]
        for row in case.tables["economic_identities"]
        if row["identity_state"] == "retired"
    } == {case.aliases["old-m"]}
    for decision in ("merge-reviewed", "split-reviewed"):
        _active_declarations(
            {
                "effective_declarations": [
                    {
                        "entity_id": entity.value,
                        "declaration_id": case.aliases["declaration:" + decision],
                    }
                ]
            },
            case.tables,
        )
    with pytest.raises(ContractError, match="identity_membership_disjoint"):
        _active_declarations(
            {
                "effective_declarations": [
                    {
                        "entity_id": entity.value,
                        "declaration_id": case.aliases["declaration:" + decision],
                    }
                    for decision in ("merge-reviewed", "split-reviewed")
                ]
            },
            case.tables,
        )
    wrong_survivor = deepcopy(case.tables)
    merge = next(
        row
        for row in wrong_survivor["declarations"]
        if row["declaration_id"] == case.aliases["declaration:merge-reviewed"]
    )
    payload = json.loads(_text(merge["payload_json"]))
    payload["successor_ids"] = [case.aliases["split-new"]]
    merge["payload_json"] = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ContractError, match="identity_successor_active"):
        _active_declarations(
            {
                "effective_declarations": [
                    {
                        "entity_id": entity.value,
                        "declaration_id": case.aliases["declaration:merge-reviewed"],
                    }
                ]
            },
            wrong_survivor,
        )
    wrong_anchor = deepcopy(case.tables)
    canonical = next(
        row
        for row in wrong_anchor["economic_identities"]
        if row["record_id"] == case.aliases["canonical"]
    )
    canonical["event_key"] = "invented-different-key"
    with pytest.raises(ContractError, match="economic_identity_canonical"):
        validate_tables(wrong_anchor)


def _same_day_quantities(case: FixtureCase) -> dict:
    by_step = {
        row["allocation_id"]: row["step_id"]
        for row in case.tables["inventory_allocations"]
    }
    quantities = defaultdict(Fraction)
    for change in case.tables["inventory_changes"]:
        step_id = by_step[change["allocation_id"]]
        if step_id in {
            case.aliases["step:buy"],
            case.aliases["step:split"],
            case.aliases["step:later-buy"],
        }:
            quantities[step_id] += _known_number(change, "units_delta")
    return quantities


def test_same_day_split_before_buy_has_guarded_dependency_and_distinct_total() -> None:
    case = load_case("ordering-split-buy")
    alternative = load_case("ordering-buy-split")
    for specimen, decision in (
        (case, "split-before-buy"),
        (alternative, "buy-before-split"),
    ):
        validate_tables(specimen.tables)
        _active_declarations(
            {
                "effective_declarations": [
                    {
                        "entity_id": specimen.tables["build"][0]["entity_id"],
                        "declaration_id": specimen.aliases["declaration:" + decision],
                    }
                ]
            },
            specimen.tables,
        )
    entity = case.tables["build"][0]["entity_id"]
    active = {
        "effective_declarations": [
            {
                "entity_id": entity,
                "declaration_id": case.aliases["declaration:split-before-buy"],
            }
        ]
    }
    same_day_steps = [
        row
        for row in case.tables["book_steps"]
        if row["txn_id"] in {case.aliases["split"], case.aliases["later-buy"]}
    ]
    assert len(same_day_steps) == 2
    assert {row["effective_date"] for row in same_day_steps} == {"2026-01-30"}
    assert [row["txn_id"] for row in same_day_steps] == [
        case.aliases["split"],
        case.aliases["later-buy"],
    ]
    assert {
        (row["before_step_id"], row["after_step_id"])
        for row in case.tables["book_step_dependencies"]
    } == {
        (case.aliases["step:buy"], case.aliases["step:split"]),
        (case.aliases["step:split"], case.aliases["step:later-buy"]),
    }
    quantities = _same_day_quantities(case)
    assert [
        quantities[case.aliases["step:" + name]]
        for name in ("buy", "split", "later-buy")
    ] == [10, 10, 5]
    assert (
        sum(quantities.values())
        == Fraction(case.expected_answers["same_day_split_then_buy"])
        == 25
    )
    other = _same_day_quantities(alternative)
    assert [
        other[alternative.aliases["step:" + name]]
        for name in ("buy", "later-buy", "split")
    ] == [10, 5, 15]
    assert (
        sum(other.values())
        == Fraction(alternative.expected_answers["same_day_buy_then_split"])
        == 30
    )
    assert alternative.aliases["buy"] == case.aliases["buy"]
    assert alternative.aliases["later-buy"] == case.aliases["later-buy"]
    assert alternative.aliases["split"] == case.aliases["split"]
    assert alternative.aliases["split-q"] == case.aliases["split-q"]
    without_edge = deepcopy(case.tables)
    without_edge["book_step_dependencies"] = []
    without_edge["provenance"] = [
        row
        for row in without_edge["provenance"]
        if row["record_kind"] != "book_step_dependencies"
    ]
    validate_tables(without_edge)
    with pytest.raises(ContractError, match="ordering_dependency"):
        _active_declarations(active, without_edge)
    cycle = deepcopy(case.tables)
    cycle["book_step_dependencies"].append(
        {
            **cycle["book_step_dependencies"][0],
            "before_step_id": case.aliases["step:later-buy"],
            "after_step_id": case.aliases["step:split"],
        }
    )
    cycle["book_step_dependencies"].sort(
        key=lambda row: (
            row["entity_id"],
            row["before_step_id"],
            row["after_step_id"],
            row["constraint_kind"],
        )
    )
    with pytest.raises(ContractError, match="precedence_forward"):
        validate_tables(cycle)


def test_source_sequence_requires_same_evidenced_scope_and_domain() -> None:
    case = load_case("source-sequence")
    validate_tables(case.tables)
    assert {
        row["effective_date"]
        for row in case.tables["book_steps"]
        if row["txn_id"] in {case.aliases["credit"], case.aliases["debit"]}
    } == {"2026-01-12"}
    edge = next(
        row
        for row in case.tables["book_step_dependencies"]
        if row["constraint_kind"] == "source_sequence"
    )
    assert (edge["before_step_id"], edge["after_step_id"]) == (
        case.aliases["step:credit"],
        case.aliases["step:debit"],
    )
    cross_domain = deepcopy(case.tables)
    domain = next(
        row
        for row in cross_domain["observations"]
        if row["observation_id"] == case.aliases["debit-domain"]
    )
    domain["text_value"] = "unrelated-statement"
    with pytest.raises(ContractError, match="source_sequence_domain"):
        validate_tables(cross_domain)
    cross_scope = deepcopy(case.tables)
    source = next(
        row
        for row in cross_scope["source_records"]
        if row["source_record_id"] == case.aliases["source:debit"]
    )
    source["source_scope_id"] = "unrelated-scope"
    with pytest.raises(ContractError, match="source_sequence_domain"):
        validate_tables(cross_scope)
    nonintegral = deepcopy(case.tables)
    sequence = next(
        row
        for row in nonintegral["observations"]
        if row["observation_id"] == case.aliases["debit-sequence"]
    )
    sequence["decimal_value_scale"] = 1
    with pytest.raises(ContractError, match="source_sequence_observation"):
        validate_tables(nonintegral)


def test_source_sequence_direction_requires_one_matching_retained_rule() -> None:
    case = load_case("source-sequence")
    rule = {
        "source_scope_id": "synthetic-fixture",
        "sequence_domain": "statement-events",
        "field_name": "source.sequence",
        "direction": "ascending",
    }
    validate_source_sequence_directions(case.tables, [{"source_sequences": [rule]}])
    with pytest.raises(ContractError, match="source_sequence_registry"):
        validate_source_sequence_directions(case.tables, [])
    with pytest.raises(ContractError, match="source_sequence_registry"):
        validate_source_sequence_directions(
            case.tables, [{"source_sequences": [rule]}, {"source_sequences": [rule]}]
        )
    with pytest.raises(ContractError, match="source_sequence_registry"):
        validate_source_sequence_directions(
            case.tables,
            [{"source_sequences": [{**rule, "field_name": "unrelated.ordinal"}]}],
        )
    descending = {**rule, "direction": "descending"}
    with pytest.raises(ContractError, match="source_sequence_direction"):
        validate_source_sequence_directions(
            case.tables, [{"source_sequences": [descending]}]
        )
    reversed_ordinals = deepcopy(case.tables)
    for row in reversed_ordinals["observations"]:
        if row["field_name"] == "source.sequence":
            row["decimal_value_coefficient"] = (
                "11" if row["record_id"] == f'["{case.aliases["credit"]}"]' else "10"
            )
    validate_tables(reversed_ordinals)
    validate_source_sequence_directions(
        reversed_ordinals, [{"source_sequences": [descending]}]
    )


def test_pending_cancelled_and_ignored_are_independent_of_financial_participation() -> (
    None
):
    case = load_case("participation")
    states = {row["txn_id"]: row["event_state"] for row in case.tables["transactions"]}
    cash = sum(
        _known_number(row, "amount")
        for row in case.tables["postings"]
        if row["position_id"] == case.aliases["cash-A"]
        and states[row["txn_id"]] == "recognized"
    )
    assert cash == -1
    assert sum(state != "recognized" for state in states.values()) == 2
    fee = next(
        row
        for row in case.tables["transactions"]
        if row["txn_id"] == case.aliases["paid-network-fee"]
    )
    assert fee["event_state"] == "recognized"
    assert fee["review_state"] == "ignored"


def test_sequential_residuals_close_exact_total_not_rounded_unit_cost() -> None:
    case = load_case("rounding")
    consumed = sorted(
        -_known_number(row, "principal_delta")
        for row in case.tables["inventory_changes"]
        if row["change_role"] == "source"
    )
    assert consumed == [Fraction("3.33"), Fraction("3.33"), Fraction("3.34")]
    assert sum(consumed) == 10
    assert 3 * Fraction("3.33") != 10


def test_transit_retains_acquisition_slices_between_departure_and_arrival() -> None:
    case = load_case("in-transit")
    amounts = defaultdict(Fraction)
    step_dates = {
        row["step_id"]: row["effective_date"] for row in case.tables["book_steps"]
    }
    for posting in case.tables["postings"]:
        step_date = step_dates[posting["step_id"]]
        if (
            posting["commodity_id"] == case.aliases["Q"]
            and isinstance(step_date, str)
            and step_date <= "2026-01-06"
        ):
            amounts[posting["position_id"]] += _known_number(posting, "amount")
    assert amounts[case.aliases["transit-Q"]] == 6
    assert amounts[case.aliases["AQ"]] == 4
    assert amounts[case.aliases["BQ"]] == 0
    assert len(case.tables["lots"]) == 2
    assert case.tables["disposals"] == []


def test_mixed_merger_preserves_released_basis_and_counts_cash_receipt_once() -> None:
    case = load_case("mixed-merger")
    validate_tables(case.tables)
    basis_application = next(
        row
        for row in case.tables["action_applications"]
        if row["effect_id"] == case.aliases["merger-basis"]
    )
    assert basis_application["target_kind"] == "inventory_change"
    assert basis_application["target_id"] == case.aliases["merger-target"]
    wrong_amount = deepcopy(case.tables)
    effect = next(
        row
        for row in wrong_amount["corporate_action_effects"]
        if row["effect_id"] == case.aliases["merger-basis"]
    )
    effect["basis_total_coefficient"] = "71"
    with pytest.raises(ContractError, match="basis_application_amount"):
        validate_tables(wrong_amount)
    wrong_currency = deepcopy(case.tables)
    effect = next(
        row
        for row in wrong_currency["corporate_action_effects"]
        if row["effect_id"] == case.aliases["merger-basis"]
    )
    effect["basis_commodity_id"] = case.aliases["Q"]
    with pytest.raises(ContractError, match="basis_application_currency"):
        validate_tables(wrong_currency)
    changes = [
        row
        for row in case.tables["inventory_changes"]
        if row["allocation_id"] == case.aliases["merger-transform"]
    ]
    assert sum(_known_number(row, "principal_delta") for row in changes) == -30
    assert _number(case.tables["disposals"][0], "proceeds_total") == 40
    assert Fraction(case.expected_answers["gross_result"]) == 40 - 30
    cash = [
        row
        for row in case.tables["postings"]
        if row["position_id"] == case.aliases["cash-A"]
        and _known_number(row, "amount") > 0
    ]
    assert sum(_known_number(row, "amount") for row in cash) == 40
    child = next(
        row for row in case.tables["lots"] if row["lot_id"] == case.aliases["child"]
    )
    assert child["parent_lot_id"] == case.aliases["parent"]
    assert child["acquisition_date"] == "2026-01-02"


def test_spinoff_basis_applications_bind_exact_parent_and_child_changes() -> None:
    case = load_case("corporate-variants")
    validate_tables(case.tables)
    parent_effect = case.aliases["spinoff-parent-basis"]
    child_effect = case.aliases["spinoff-child-basis"]
    applications = {
        row["effect_id"]: row
        for row in case.tables["action_applications"]
        if row["effect_id"] in {parent_effect, child_effect}
    }
    assert applications[parent_effect]["target_id"] == case.aliases["spinoff-parent"]
    assert applications[child_effect]["target_id"] == case.aliases["spinoff-child"]
    assert all(
        row["target_kind"] == "inventory_change" for row in applications.values()
    )

    swapped = deepcopy(case.tables)
    replacements = {}
    for row in swapped["action_applications"]:
        if row["effect_id"] in applications:
            old_key = (row["effect_id"], row["target_kind"], row["target_id"])
            row["target_id"] = case.aliases[
                "spinoff-child"
                if row["effect_id"] == parent_effect
                else "spinoff-parent"
            ]
            replacements[old_key] = (
                row["effect_id"],
                row["target_kind"],
                row["target_id"],
            )
    for row in swapped["provenance"]:
        if row["record_kind"] == "action_applications":
            key = tuple(json.loads(_text(row["record_id"])))
            if key in replacements:
                row["record_id"] = json.dumps(
                    replacements[key], ensure_ascii=False, separators=(",", ":")
                )
    with pytest.raises(ContractError, match="basis_application_amount"):
        validate_tables(swapped)


def test_reference_transfer_preserves_carried_reference_across_positions() -> None:
    case = load_case("reference-variants")
    validate_tables(case.tables)
    changed = deepcopy(case.tables)
    for alias, field in (
        ("variant-transfer-in", "reference_after"),
        ("variant-three-B-close", "reference_before"),
    ):
        row = next(
            row
            for row in changed["reference_changes"]
            if row["reference_change_id"] == case.aliases[alias]
        )
        _change_number(row, field, "102")
    with pytest.raises(ContractError, match="reference_transfer_continuity"):
        validate_tables(changed)


def test_nary_outcomes_remain_distinct_and_links_do_not_generate_holdings() -> None:
    case = load_case("outcomes")
    outcome_members = [
        row for row in case.tables["links"] if row["link_kind"] == "complete_set"
    ]
    assert {row["member_id"] for row in outcome_members} == {
        case.aliases["O1"],
        case.aliases["O2"],
        case.aliases["O3"],
    }
    assert len(outcome_members) == 3
    assert sum(row["member_role"] == "roll" for row in case.tables["links"]) == 2
    amounts = defaultdict(Fraction)
    for posting in case.tables["postings"]:
        if posting["position_id"]:
            amounts[posting["position_id"]] += _known_number(posting, "amount")
    assert amounts[case.aliases["cash-A"]] == 0
    for name in ("outcome-1", "outcome-2", "outcome-3"):
        assert amounts[case.aliases[name]] == 0
    assert case.tables["inventory_changes"] == []


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_missing_required_nullable_field_rejects_complete_snapshot(
    case_id: str,
) -> None:
    tables = deepcopy(load_case(case_id).tables)
    del tables["transactions"][0]["payee"]
    with pytest.raises(ContractError, match="closed_row"):
        validate_tables(tables)


def _change_number(row: dict, field: str, coefficient: str, scale: int = 0) -> None:
    row[field + "_coefficient"] = coefficient
    row[field + "_scale"] = scale
    row[field + "_source_scale"] = None


def _mutate_references(case: FixtureCase, tables: dict, mutation: str) -> None:
    if mutation == "wrong_position_account":
        position_leg = next(
            row for row in tables["postings"] if row["leg_kind"] == "position"
        )
        position_leg["account_id"] = case.aliases["B"]
    elif mutation == "wrong_account_reference":
        tables["postings"][0]["account_id"] = "nonexistent-account"
    elif mutation == "wrong_declaration_revision":
        tables["declarations"][0]["revision_digest"] = "0" * 64
    else:
        message = f"Unimplemented fixture mutation: {mutation}"
        raise ValueError(message)


def _mutated_tables(case_id: str, mutation: str) -> dict:
    case = load_case(case_id)
    tables = deepcopy(case.tables)
    if mutation == "unknown_field":
        tables["transactions"][0]["invented_unknown_field"] = "rejected"
    elif mutation == "decimal_noncanonical":
        tables["postings"][0]["amount_coefficient"] = "-0"
    elif mutation == "transfer_component":
        allocation = next(
            row
            for row in tables["inventory_allocations"]
            if row["allocation_kind"] == "transfer"
        )
        target = next(
            row
            for row in tables["inventory_changes"]
            if row["allocation_id"] == allocation["allocation_id"]
            and row["change_role"] == "target"
        )
        _change_number(target, "principal_delta", "999")
    elif mutation == "reference_before":
        close = next(
            row for row in tables["reference_changes"] if row["change_kind"] == "close"
        )
        _change_number(close, "reference_before", "100")
    elif mutation == "both_lot_and_pool":
        target = next(
            row for row in tables["inventory_changes"] if row["pool_id"] is not None
        )
        target["lot_id"] = case.aliases["L1"]
    elif mutation == "pending_step":
        pending = next(
            row for row in tables["transactions"] if row["event_state"] == "pending"
        )
        tables["book_steps"][0]["txn_id"] = pending["txn_id"]
    elif mutation == "boundary_account":
        boundary = next(
            row for row in tables["postings"] if row["leg_kind"] == "boundary"
        )
        boundary["account_id"] = case.aliases["A"]
    elif mutation == "final_principal_residual":
        final = next(
            row
            for row in tables["inventory_changes"]
            if row["change_id"] == case.aliases["thirds-c3"]
        )
        _change_number(final, "principal_delta", "-332", 2)
    else:
        _mutate_references(case, tables, mutation)
    return tables


@pytest.mark.parametrize(
    ("case_id", "mutation", "constraint"),
    [
        ("cash", "unknown_field", "closed_row"),
        ("rounding", "decimal_noncanonical", "decimal_coefficient"),
        (
            "compound-expensed",
            "transfer_component",
            "allocation_component_conservation",
        ),
        ("reference", "reference_before", "reference_continuity"),
        ("pools", "both_lot_and_pool", "inventory_slice"),
        ("participation", "pending_step", "recognized_step"),
        ("brokerage", "boundary_account", "boundary_leg"),
        ("brokerage", "wrong_position_account", "position_leg_identity"),
        ("cash", "wrong_account_reference", "foreign_key"),
        ("cash", "wrong_declaration_revision", "declaration_revision_identity"),
        ("rounding", "final_principal_residual", "full_close_components"),
    ],
)
def test_invalid_financial_specimens_fail_the_owning_constraint(
    case_id: str, mutation: str, constraint: str
) -> None:
    with pytest.raises(ContractError, match=constraint):
        validate_tables(_mutated_tables(case_id, mutation))


def test_coverage_index_has_no_unproven_structural_obligations() -> None:
    cases = {case.case_id: case for case in fixture_cases()}
    index = coverage_index()
    assert index
    assert len({entry["family"] for entry in index}) == len(index)
    for entry in index:
        assert entry["case"] in cases
        assert entry["positive"] in cases[entry["case"]].expected_answers
        assert entry["negative"]
        assert entry["later_processor"]
        assert entry["status"] == "met"
        assert not entry.get("missing")
    assert all(
        any(case.tables[table.name] for case in cases.values()) for table in catalog()
    )
