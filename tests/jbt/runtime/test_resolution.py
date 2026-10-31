import json
from copy import deepcopy
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

import pytest
from tests.fixtures.synthetic_importer.synthetic_statements.adapter import (
    SECTION,
    Entry,
    Statement,
    importer,
)
from tests.jbt.model.test_quantity import history, run, source

from jbt.artifacts.beancount import BeancountProjectionError
from jbt.artifacts.integrity import byte_digest
from jbt.checks.quantity import CheckKind, CheckStatus
from jbt.domain.canonical import encode_json
from jbt.domain.identity import Occurrence, event_id
from jbt.domain.ids import RecordId, RecordKind, SemanticKey
from jbt.domain.quantity import EventState, FactKind
from jbt.domain.time import TimeRole
from jbt.extraction import ExtractionContext
from jbt.extraction import extract as extract_wire
from jbt.model.quantity import (
    AuthorityMode,
    EventKind,
    FindingCode,
    ResolutionStatus,
    SupportKind,
    resolve,
)
from jbt.runtime.checks import (
    projected_account_checks,
    projected_model_checks,
)
from jbt.runtime.outputs import ledger
from jbt.runtime.project import Declaration, Project, read_project
from jbt.runtime.resolution import (
    ExtractInput,
    Prepared,
    prepare,
    project_resolution,
    source_civil_context,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


def test_dimension_projection_does_not_select_an_earlier_listed_future_revision(
    project_document: dict,
    make_inputs: Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]],
) -> None:
    current = next(
        item
        for item in project_document["declarations"]
        if item["payload"]["kind"] == "account"
    )
    future = deepcopy(current)
    current["valid_to"] = future["valid_from"] = "2027-01-01"
    future["payload"]["institution"] = "Invented future institution"
    project_document["declarations"].insert(0, future)
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    (account,) = prepared.rows.tables["accounts"]
    expected = next(
        declaration
        for declaration in project.declarations
        if declaration.key == current["key"]
        and declaration.valid_from != date(2027, 1, 1)
    )
    assert account["declaration_id"] == expected.revision
    assert account["institution"] == current["payload"]["institution"]


@pytest.mark.parametrize("suffix", [b"", b"\n\n"])
def test_ofx_source_replacement_preserves_identity_and_selected_evidence(
    suffix: bytes,
) -> None:
    original = (
        source(
            bank=(("original-key", "20260120", -5),),
            card=(),
            bank_balance=95,
            card_balance=0,
        ).replace(b"Invented expense", b"Old description")
        + suffix
    )
    corrected = (
        source(
            bank=(("corrected-key", "20260110", -7),),
            card=(),
            bank_balance=93,
            card_balance=0,
        )
        .replace(b"Invented expense", b"Corrected description")
        .replace(
            b"<FITID>corrected-key</FITID>",
            b"<FITID>corrected-key</FITID>"
            b"<CORRECTFITID>original-key</CORRECTFITID><CORRECTACTION>REPLACE</CORRECTACTION>",
        )
    )
    value, extracts = history((original, corrected))
    project = read_project(encode_json(value, integer_strings=False))
    prepared = prepare(
        project,
        tuple(
            ExtractInput(selection, payload)
            for selection, payload in zip(project.sources, extracts, strict=True)
        ),
    )
    assert len(prepared.inputs.source_revisions) == 1
    original_movement = next(
        m
        for m in prepared.inputs.movements
        if m.source_key is not None and m.source_key.source_key.value == "original-key"
    )
    resolution = resolve(prepared.inputs)
    (event,) = [event for event in resolution.events if event.declaration is None]
    assert original_movement.source_key is not None
    assert event.identity == event_id(
        project.entity,
        Occurrence(original_movement.source_key, original_movement.event_key),
    )
    assert event.when == date(2026, 1, 10)
    assert event.legs[0].amount.coefficient == "-7"
    assert {support.kind for support in event.support} == {
        SupportKind.SELECTED,
        SupportKind.REPLACED,
    }
    selected = next(
        support.source_record
        for support in event.support
        if support.kind == SupportKind.SELECTED
    )
    assert (event.support[0].kind == SupportKind.REPLACED) == bool(suffix)
    tables = project_resolution(prepared, resolution)
    transaction = next(
        row for row in tables["transactions"] if row["txn_id"] == event.identity.value
    )
    assert transaction["description_as_stated"] == "Corrected description"
    assert transaction["payee"] == "Corrected description"
    posting = next(
        row
        for row in tables["postings"]
        if row["txn_id"] == event.identity.value and row["leg_kind"] == "position"
    )
    assert posting["amount_coefficient"] == "-7"
    quantity_evidence = [
        row
        for row in tables["provenance"]
        if row["record_kind"] == "postings"
        and row["record_id"]
        == json.dumps([posting["posting_id"]], separators=(",", ":"))
        and row["field_name"] == "amount"
    ]
    assert len(quantity_evidence) == 1
    assert quantity_evidence[0]["evidence_id"] == selected.value
    assert prepared.inputs.statements
    assert all(
        m.statement in {s.identity for s in prepared.inputs.statements}
        for m in prepared.inputs.movements
    )


def test_ofx_source_deletion_adapts_retracted_not_cancelled_state() -> None:
    original = source(
        bank=(("original-key", "20260112", -5),),
        card=(),
        bank_balance=95,
        card_balance=0,
    )
    deletion = source(
        bank=(("deletion", "20260110", -5),),
        card=(),
        bank_balance=100,
        card_balance=0,
    ).replace(
        b"<FITID>deletion</FITID>",
        b"<FITID>deletion</FITID>"
        b"<CORRECTFITID>original-key</CORRECTFITID><CORRECTACTION>DELETE</CORRECTACTION>",
    )
    value, extracts = history((original, deletion))
    project = read_project(encode_json(value, integer_strings=False))
    prepared = prepare(
        project,
        tuple(
            ExtractInput(selection, payload)
            for selection, payload in zip(project.sources, extracts, strict=True)
        ),
    )
    resolution = resolve(prepared.inputs)
    (event,) = [event for event in resolution.events if event.declaration is None]
    assert event.state == EventState.RETRACTED
    assert not any(f.blocking for f in resolution.findings)


def synthetic_inputs(
    project_document: dict,
    extraction_documents: tuple[dict, ...],
    statements: tuple[Statement, ...],
) -> tuple[Project, tuple[ExtractInput, ...]]:
    documents = [extraction_documents[0]]
    for statement in statements:
        raw = statement.encode()
        context = ExtractionContext(
            "synthetic-feed", byte_digest(raw), "synthetic-statements"
        )
        documents.append(json.loads(extract_wire(importer, raw, context)))
    project_document["sources"] = [d["source_blob_digest"] for d in documents]
    account = next(d for d in project_document["declarations"] if d["key"] == "bank")
    account["payload"]["bindings"].append(
        {
            "source_scope_id": "synthetic-feed",
            "source_section": SECTION,
            "source_position_key": None,
        }
    )
    project_document["configuration"]["as_of"] = "2026-03-02"
    project = read_project(encode_json(project_document, integer_strings=False))
    return project, tuple(
        ExtractInput(selection, encode_json(document, integer_strings=False))
        for selection, document in zip(project.sources, documents, strict=True)
    )


@pytest.mark.parametrize(
    ("complete", "expected"), [(False, CheckStatus.FAIL), (True, CheckStatus.PASS)]
)
def test_synthetic_empty_statement_only_covers_period_when_explicitly_complete(
    project_document: dict,
    extraction_documents: tuple[dict, ...],
    *,
    complete: bool,
    expected: CheckStatus,
) -> None:
    project, extracts = synthetic_inputs(
        project_document,
        extraction_documents,
        (
            Statement(
                "2026-01-01",
                "2026-02-01",
                100,
                (),
                complete=complete,
                replaces=None,
            ),
        ),
    )
    prepared = prepare(project, extracts)
    statement = next(
        s for s in prepared.inputs.statements if s.section.value == SECTION
    )
    assert statement.parts[0].complete == complete
    tables = project_resolution(prepared, resolve(prepared.inputs))
    checks = [
        check
        for check in projected_account_checks(project, tables)
        if check.kind == CheckKind.COVERAGE
    ]
    assert checks
    assert {check.status for check in checks} == {expected}


def test_section_reissue_retains_native_statement_parts_and_source_scope(
    project_document: dict, extraction_documents: tuple[dict, ...]
) -> None:
    old = Statement(
        "2026-01-01",
        "2026-02-01",
        95,
        (Entry("removed", "2026-01-12", -5, "Invented expense"),),
        complete=True,
        replaces=None,
    )
    reissue = Statement(
        "2026-01-01",
        "2026-02-01",
        100,
        (),
        complete=True,
        replaces=byte_digest(old.encode()),
    )
    later = Statement(
        "2026-02-01",
        "2026-03-01",
        105,
        (Entry("reversal", "2026-02-12", 5, "Invented income"),),
        complete=True,
        replaces=None,
    )
    project, extracts = synthetic_inputs(
        project_document, extraction_documents, (old, reissue, later)
    )
    prepared = prepare(project, extracts)
    assert len(prepared.inputs.source_statement_revisions) == 1
    revision = prepared.inputs.source_statement_revisions[0]
    statement = next(
        s for s in prepared.inputs.statements if s.identity == revision.statement
    )
    assert statement.scope.value == "synthetic-feed"
    assert statement.parts[0].date_role == TimeRole.POSTED
    assert statement.parts[0].basis.value == "posted"
    balance = next(
        part for part in statement.parts if part.fact_kind == FactKind.BALANCE
    )
    assert balance.point is not None
    assert balance.source_records
    assert len(balance.balances) == 1
    native_balance = balance.balances[0]
    assert native_balance.source_record in balance.source_records
    assert native_balance.observation_key.value
    assert native_balance.account.value == "bank"
    assert native_balance.position is None
    assert native_balance.commodity.value == "USD"
    assert native_balance.amount.coefficient == "100"
    resolution = resolve(prepared.inputs)
    assert not any(f.blocking for f in resolution.findings), resolution.findings
    assert {
        (e.state, e.when, e.legs[0].amount.coefficient)
        for e in resolution.events
        if e.declaration is None
    } == {
        (EventState.RETRACTED, date(2026, 1, 12), "-5"),
        (EventState.RECOGNIZED, date(2026, 2, 12), "5"),
    }
    original_statement = next(
        s
        for s in prepared.inputs.statements
        if s.blob_digest == revision.target_blob_digest
        and s.account == statement.account
    )
    original_balance_sources = {
        record
        for part in original_statement.parts
        if part.fact_kind == FactKind.BALANCE
        for record in part.source_records
    }
    from jbt.artifacts.projection import projection_key  # noqa: PLC0415

    original_assertions = {
        projection_key("assertion", record.value) for record in original_balance_sources
    }
    assert original_assertions <= {
        row["assertion_set_id"] for row in prepared.rows.tables["balance_assertions"]
    }
    tables = project_resolution(prepared, resolution)
    assert not original_assertions & {
        row["assertion_set_id"] for row in tables["balance_assertions"]
    }
    assert original_assertions <= {
        row["assertion_set_id"] for row in prepared.rows.tables["balance_assertions"]
    }


def test_authority_adapter_resolves_registry_basis_role_and_source_scope_names() -> (
    None
):
    value, extracts = history()
    project = read_project(encode_json(value, integer_strings=False))
    configuration = project.configuration_document()
    configuration["source_authority"] = [
        {
            "account_id": "bank",
            "fact_kind": "movement",
            "coverage_basis_id": "posted",
            "period_start": "2026-01-01",
            "period_end": "2026-02-01",
            "mode": "complete_replacement",
            "source_scope_id": "bank-feed",
        }
    ]
    project = replace(
        project, configuration=encode_json(configuration, integer_strings=False)
    )
    prepared = prepare(
        project,
        tuple(
            ExtractInput(selection, payload)
            for selection, payload in zip(project.sources, extracts, strict=True)
        ),
    )
    (authority,) = prepared.inputs.authorities
    assert authority.account.value == "bank"
    assert authority.fact_kind == FactKind.MOVEMENT
    assert authority.date_role == TimeRole.POSTED
    assert authority.validity.start == date(2026, 1, 1)
    assert authority.validity.end == date(2026, 2, 1)
    assert authority.source_scope.value == "bank-feed"
    assert authority.mode == AuthorityMode.COMPLETE_REPLACEMENT


def test_native_balance_guard_evidence_blocks_unresolved_commodity() -> None:
    value, extracts = history()
    retained = [json.loads(payload) for payload in extracts]
    last = retained[-1]
    section = last["source_scope"]["sections"][0]["section_id"]
    assertion = next(
        record
        for record in last["records"]
        if record["kind"] == "assertion" and record["source_section"] == section
    )
    assertion["payload"]["observations"][0]["commodity_token"] = "UNKNOWN"  # noqa: S105
    project = read_project(encode_json(value, integer_strings=False))
    prepared = prepare(
        project,
        tuple(
            ExtractInput(selection, encode_json(payload, integer_strings=False))
            for selection, payload in zip(project.sources, retained, strict=True)
        ),
    )
    native = next(
        statement
        for statement in prepared.inputs.statements
        if statement.blob_digest == last["source_blob_digest"]
        and statement.account.value == "bank"
    )
    balance = next(part for part in native.parts if part.fact_kind == FactKind.BALANCE)
    assert balance.balances == ()
    assert any(
        finding.code == FindingCode.SYMBOL
        and finding.blocking
        and finding.scope.value == assertion["source_record_id"]
        for finding in prepared.inputs.findings
    )


def test_disagreeing_status_semantics_in_one_scope_block_affected_events() -> None:
    first = source(
        bank=(("A", "20260112", -5),),
        card=(),
        bank_balance=95,
        card_balance=0,
    )
    value, extracts = history((first, first + b"\n"))
    retained = [json.loads(payload) for payload in extracts]
    for mapping in retained[-1]["status_mappings"]:
        if mapping["candidate_state"] == "recognized":
            mapping["candidate_state"] = "pending"
    project = read_project(encode_json(value, integer_strings=False))
    prepared = prepare(
        project,
        tuple(
            ExtractInput(selection, encode_json(payload, integer_strings=False))
            for selection, payload in zip(project.sources, retained, strict=True)
        ),
    )
    resolution = resolve(prepared.inputs)
    assert any(
        f.code == FindingCode.CONTENT and f.blocking for f in resolution.findings
    )
    assert all(e.declaration is not None for e in resolution.events)


def test_reviewed_membership_and_retirement_publish_declaration_provenance() -> None:
    value, extracts = history()
    project = read_project(encode_json(value, integer_strings=False))
    prepared = prepare(
        project,
        tuple(
            ExtractInput(selection, payload)
            for selection, payload in zip(project.sources, extracts, strict=True)
        ),
    )
    resolution = resolve(prepared.inputs)
    original = next(e for e in resolution.events if e.support)
    decision = RecordId(project.entity, RecordKind.DECLARATION_REVISION, "a" * 64)
    retired = replace(
        original,
        state=EventState.RETRACTED,
        identity_decision=decision,
        support=tuple(
            replace(support, decision=decision) for support in original.support
        ),
    )
    resolution = replace(
        resolution,
        determinations=tuple(
            replace(determination, event=retired)
            if determination.identity == original.identity
            else determination
            for determination in resolution.determinations
        ),
        retired=(original.identity,),
    )
    tables = project_resolution(prepared, resolution)
    identity = next(
        row
        for row in tables["economic_identities"]
        if row["record_id"] == original.identity.value
    )
    assert identity["identity_state"] == "retired"
    assert identity["decision_declaration_id"] == decision.value
    provenance = [
        row for row in tables["provenance"] if row["evidence_id"] == decision.value
    ]
    assert all(row["value_origin"] == "correction" for row in provenance)
    assert not {
        row["field_name"] for row in provenance if row["record_kind"] == "event_support"
    }
    assert {
        row["field_name"]
        for row in provenance
        if row["record_kind"] == "economic_identities"
    } >= {"anchor_id", "anchor_kind", "identity_state"}
    live_resolution = replace(
        resolution,
        retired=(),
        determinations=tuple(
            replace(determination, event=replace(retired, state=EventState.RECOGNIZED))
            if determination.identity == original.identity
            else determination
            for determination in resolution.determinations
        ),
    )
    live_tables = project_resolution(prepared, live_resolution)
    assert {
        row["field_name"]
        for row in live_tables["provenance"]
        if row["record_kind"] == "event_support"
        and row["evidence_id"] == decision.value
    } == {"txn_id", "support_kind"}


def test_preparation_decodes_declarations_once_and_preserves_authored_metadata(
    mocker: MockerFixture,
) -> None:
    value, extracts = history()
    record = next(d for d in value["declarations"] if d["key"] == "initial-cash")[
        "payload"
    ]["record"]
    record["description_as_stated"] = "  retained e\u0301vidence\n"
    record["links"] = ["receipt"]
    record["tags"] = ["seed"]
    value["declarations"].append(
        {
            "key": "opening-tags",
            "valid_from": None,
            "valid_to": None,
            "payload": {
                "schema_version": 1,
                "kind": "rule",
                "order": 2,
                "matches": [
                    {
                        "field": "transactions.event_kind",
                        "operator": "equals",
                        "value": "opening",
                    }
                ],
                "assignments": [
                    {"field": "transactions.tags", "value": ["opening", "seed"]}
                ],
                "split": [],
            },
        }
    )
    record["legs"][0]["posting_role"] = "principal"
    record["legs"][0]["symbol_as_stated"] = "stated USD"
    project = read_project(encode_json(value, integer_strings=False))
    inputs = tuple(
        ExtractInput(selection, payload)
        for selection, payload in zip(project.sources, extracts, strict=True)
    )
    decoded = mocker.spy(Declaration, "decode")
    prepared = prepare(project, inputs)
    assert decoded.call_count == len(project.declarations)
    assert all(
        set(fields) <= {"description_as_stated", "links", "tags"}
        for fields in prepared.transaction_fields.values()
    )

    resolution = resolve(prepared.inputs)
    tables = project_resolution(prepared, resolution)
    assert decoded.call_count == len(project.declarations)
    revision = next(d.revision for d in project.declarations if d.key == "initial-cash")
    event = next(
        event
        for event in resolution.events
        if event.declaration is not None and event.declaration.value == revision
    )
    transaction = next(
        t for t in tables["transactions"] if t["txn_id"] == event.identity.value
    )
    assert transaction["description_as_stated"] == record["description_as_stated"]
    assert transaction["links"] == ["receipt"]
    assert transaction["tags"] == ["opening", "seed"]
    tag_evidence = {
        row["evidence_id"]
        for row in tables["provenance"]
        if row["record_kind"] == "transactions"
        and row["record_id"]
        == json.dumps([event.identity.value], separators=(",", ":"))
        and row["field_name"] == "tags"
    }
    assert tag_evidence == {
        revision,
        next(d.revision for d in project.declarations if d.key == "opening-tags"),
    }
    posting = next(
        p
        for p in tables["postings"]
        if p["txn_id"] == event.identity.value and p["posting_index"] == 0
    )
    assert posting["posting_role"] == "principal"
    assert posting["symbol_as_stated"] == "stated USD"
    assert posting["amount_coefficient"] == "100"


def test_source_supplemental_fields_follow_the_selected_support() -> None:
    raw = source(
        bank=(("credit", "20260112", 20),), card=(), bank_balance=120, card_balance=0
    )
    value, extracts = history((raw, raw + b"\n"))
    retained = []
    descriptions = {}
    for index, encoded in enumerate(extracts):
        document = json.loads(encoded)
        for record in document["records"]:
            if record["kind"] == "transaction":
                description = f"  selected description {index}\n"
                record["payload"]["description_as_stated"] = description
                descriptions[record["source_record_id"]] = description
        retained.append(encode_json(document, integer_strings=False))
    _, _, resolution, tables = run(value, tuple(retained))
    event = next(event for event in resolution.events if len(event.support) == 2)
    transaction = next(
        t for t in tables["transactions"] if t["txn_id"] == event.identity.value
    )
    assert (
        transaction["description_as_stated"]
        == descriptions[
            next(
                support.source_record.value
                for support in event.support
                if support.kind == SupportKind.SELECTED
            )
        ]
    )


def test_authored_currency_is_native_and_projects_under_derived_leg_keys(
    project_document: dict,
    make_inputs: Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]],
) -> None:
    opening = next(
        d for d in project_document["declarations"] if d["key"] == "initial-cash"
    )
    for leg in opening["payload"]["record"]["legs"]:
        leg["original_commodity_id"] = "USD"
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    resolution = resolve(prepared.inputs)
    event = next(e for e in resolution.events if e.kind == EventKind.OPENING)
    assert all(leg.original_commodity == SemanticKey("USD") for leg in event.legs)
    derived = replace(
        event,
        legs=tuple(
            replace(leg, key=SemanticKey(f"derived:{leg.key.value}"))
            for leg in event.legs
        ),
    )
    tables = project_resolution(
        prepared,
        replace(
            resolution,
            determinations=tuple(
                replace(d, event=derived) if d.identity == event.identity else d
                for d in resolution.determinations
            ),
        ),
    )
    postings = [
        row for row in tables["postings"] if row["txn_id"] == event.identity.value
    ]
    assert len(postings) == len(event.legs)
    assert all(row["original_commodity_id"] == "USD" for row in postings)
    assert all(row["original_amount_coefficient"] is None for row in postings)


def test_authored_assertion_and_timed_note_project_actual_records_and_provenance() -> (
    None
):
    value, extracts = history()
    _, _, _, initial = run(value, extracts)
    scope = next(s for s in initial["assertion_scopes"] if s["account_id"] == "bank")
    assertion = next(
        a for a in initial["balance_assertions"] if a["scope_id"] == scope["scope_id"]
    )
    declaration = deepcopy(
        next(d for d in value["declarations"] if d["key"] == "initial-cash")
    )
    declaration["key"] = "authored-bank-point"
    declaration["payload"]["fact_kind"] = "assertion"
    declaration["payload"]["record"] = {
        k: v
        for k, v in assertion.items()
        if k not in {"entity_id", "assertion_set_id", "statement_id"}
    }
    record = declaration["payload"]["record"]
    record.update(date="2026-01-31", assertion_kind="closing")
    for key in record:
        if key.startswith("timestamp_"):
            record[key] = None
    record["observations"] = [
        {
            "observation_key": "net",
            "position_id": "cash",
            "commodity_id": "USD",
            "amount": {"coefficient": "108", "scale": 0, "source_scale": None},
        }
    ]
    value["declarations"].append(declaration)
    note = deepcopy(declaration)
    note["key"] = "authored-note"
    note["payload"]["fact_kind"] = "note"
    note["payload"]["record"] = {
        "date": "2026-01-12",
        "narration": "Invented audit note",
        "timestamp_date": "2026-01-12",
        "timestamp_local": "12:34:56.123",
        "timestamp_offset_minutes": 0,
        "timestamp_zone": "GMT",
        "timestamp_precision": "fractional_second",
        "timestamp_fraction_digits": 3,
    }
    value["declarations"].append(note)
    project, _, resolution, tables = run(value, extracts)
    assert not any(f.blocking for f in resolution.findings)
    assert all(
        c.status == CheckStatus.PASS
        for c in projected_model_checks(tables, frozenset({CheckKind.MODEL}))
    )
    (note_row,) = [r for r in tables["transactions"] if r["event_kind"] == "note"]
    (time,) = [r for r in tables["event_times"] if r["record_id"] == note_row["txn_id"]]
    assert (time["date_role"], time["timestamp_local"]) == ("occurred", "12:34:56.123")
    assert not [p for p in tables["postings"] if p["txn_id"] == note_row["txn_id"]]
    assert any(
        p["record_kind"] == "event_times" and p["evidence_kind"] == "declaration"
        for p in tables["provenance"]
    )
    with pytest.raises(BeancountProjectionError, match="note_account_required"):
        ledger(project, tables)
    mapping = deepcopy(value["configuration"]["rendering"][0])
    mapping["target"] = {"table": "transactions", "key": [note_row["txn_id"]]}
    value["configuration"]["rendering"].append(mapping)
    rendered_project = read_project(encode_json(value, integer_strings=False))
    assert b"Invented audit note" in ledger(rendered_project, tables)


def test_authored_fact_policy_revisions_select_one_identity_at_the_horizon() -> None:
    value, extracts = history()
    first = next(d for d in value["declarations"] if d["key"] == "initial-cash")
    later = deepcopy(first)
    first["valid_to"] = later["valid_from"] = "2026-01-15"
    value["declarations"].append(later)
    project, _, resolution, tables = run(value, extracts)
    assert not any(f.blocking for f in resolution.findings)
    assert len([t for t in tables["transactions"] if t["event_kind"] == "opening"]) == 2
    current = next(
        d.revision
        for d in project.declarations
        if d.key == "initial-cash" and d.applies(date(2026, 2, 1))
    )
    assert any(p["evidence_id"] == current for p in tables["provenance"])


def test_uninterpretable_keyed_confirmation_has_one_blocked_determination() -> None:
    good = source(
        bank=(("credit", "20260112", 20),), card=(), bank_balance=120, card_balance=0
    )
    bad = good.replace(
        b"<DTPOSTED>20260112</DTPOSTED>", b"<DTPOSTED>00000229</DTPOSTED>"
    )
    _, _, resolution, tables = run(*history((good, bad)))
    blocked = [
        d for d in resolution.determinations if d.status == ResolutionStatus.BLOCKED
    ]
    assert len(blocked) == 1
    assert not any(
        t["txn_id"] == blocked[0].identity.value for t in tables["transactions"]
    )
    assert len({d.identity for d in resolution.determinations}) == len(
        resolution.determinations
    )


@pytest.mark.parametrize(
    ("state", "role"),
    [
        ("pending", "authorized"),
        ("cancelled", "posted"),
        ("retracted", "authorized"),
        ("cancelled", "ambiguous"),
        ("recognized", "missing"),
    ],
)
def test_authored_effective_role_controls_dates_without_inventing_participation(
    project_document: dict,
    make_inputs: Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]],
    state: str,
    role: str,
) -> None:
    record = next(
        d for d in project_document["declarations"] if d["key"] == "initial-cash"
    )["payload"]["record"]
    record.update(
        event_kind="transaction",
        event_state=state,
        date_posted="2026-01-24" if role in {"posted", "ambiguous"} else None,
        date_authorized="2026-01-24" if role in {"authorized", "ambiguous"} else None,
    )
    prepared = prepare(*make_inputs(project_document))
    resolution = resolve(prepared.inputs)
    if role in {"ambiguous", "missing"}:
        assert not prepared.inputs.authored
        assert any(
            finding.code == FindingCode.DATE and finding.blocking
            for finding in resolution.findings
        )
        assert any(
            d.status == ResolutionStatus.BLOCKED and d.event is None
            for d in resolution.determinations
        )
    else:
        (event,) = prepared.inputs.authored
        assert event.state == EventState(state)
        assert event.effective_role == TimeRole(role)
        assert event.when == date(2026, 1, 24)
        assert not any(f.blocking for f in resolution.findings)
        tables = project_resolution(prepared, resolution)
        assert not any(
            s["txn_id"] == event.identity.value for s in tables["book_steps"]
        )


@pytest.mark.parametrize(
    "source_time",
    [
        ("0000-02-29", "23:59:00", 0),
        ("2026-01-12", "23:59:60", 0),
    ],
)
def test_exact_source_time_stays_retained_when_interpretation_is_blocked(
    source_time: tuple[str, str, int],
) -> None:
    value, extracts = history()
    document = deepcopy(json.loads(extracts[1]))
    transaction = next(r for r in document["records"] if r["kind"] == "transaction")
    payload = transaction["payload"]
    civil, local, offset = source_time
    payload["date_posted"] = payload["legs"][0]["date_posted"] = civil
    payload["times"][0].update(
        timestamp_date=civil,
        timestamp_local=local,
        timestamp_offset_seconds=offset,
        timestamp_precision="second",
    )
    prepared = prepare_changed_download(value, extracts, document)
    resolution = resolve(prepared.inputs)
    tables = project_resolution(prepared, resolution)
    assert any(f.code == FindingCode.TIME and f.blocking for f in resolution.findings)
    assert transaction["source_record_id"] in {
        r["source_record_id"] for r in tables["source_records"]
    }


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("04:50:00", ("bank", date(2026, 1, 31))),
        ("05:00:00", ("bank-later", date(2026, 2, 1))),
    ],
)
def test_source_context_selects_revision_by_converted_not_source_day(
    clock: str,
    expected: tuple[str, date],
) -> None:
    value, _ = history()
    earlier = next(d for d in value["declarations"] if d["key"] == "bank")
    earlier["payload"]["civil_zone"] = "America/New_York"
    later = deepcopy(earlier)
    earlier["valid_to"] = later["valid_from"] = "2026-02-01"
    later["key"] = later["payload"]["account_id"] = "bank-later"
    value["declarations"].append(later)
    project = read_project(encode_json(value, integer_strings=False))
    binding = earlier["payload"]["bindings"][0]
    source = {
        "timestamp_date": "2026-02-01",
        "timestamp_local": clock,
        "timestamp_offset_seconds": 0,
        "timestamp_zone": None,
        "timestamp_precision": "second",
        "timestamp_fraction_digits": None,
    }
    scope = SemanticKey(binding["source_scope_id"])
    section = SemanticKey(binding["source_section"])
    account, zone, converted = source_civil_context(
        project, scope=scope, section=section, time=source
    )
    assert (account.value, converted.date) == expected
    assert zone == "America/New_York"


def prepare_changed_download(
    value: dict, extracts: tuple[bytes, ...], retained: dict
) -> Prepared:
    project = read_project(encode_json(value, integer_strings=False))
    payloads = (*extracts[:-1], encode_json(retained, integer_strings=False))
    return prepare(
        project,
        tuple(
            ExtractInput(selection, payload)
            for selection, payload in zip(project.sources, payloads, strict=True)
        ),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_movement_binding_is_independent_of_time_role_order(*, reverse: bool) -> None:
    value, extracts = history(
        (
            source(
                bank=(("boundary", "20260201045000[0:GMT]", -5),),
                card=(),
                bank_balance=95,
                card_balance=0,
            ),
        )
    )
    earlier = next(d for d in value["declarations"] if d["key"] == "bank")
    later = deepcopy(earlier)
    earlier["payload"]["civil_zone"] = "America/New_York"
    earlier["valid_to"] = later["valid_from"] = "2026-02-01"
    value["declarations"].append(later)
    retained = json.loads(extracts[-1])
    for section in retained["source_scope"]["sections"]:
        for part in section["evidence_parts"]:
            for field in ("start", "end"):
                if field in part:
                    part[field].update(
                        timestamp_local=None,
                        timestamp_offset_seconds=None,
                        timestamp_zone=None,
                        timestamp_precision="date",
                    )
    record = next(r for r in retained["records"] if r["kind"] == "transaction")
    payload = record["payload"]
    authorization = {
        **payload["times"][0],
        "date_role": "authorized",
        "timestamp_date": "2026-01-30",
    }
    payload["date_authorized"] = "2026-01-30"
    payload["times"].insert(0, authorization)
    if reverse:
        payload["times"].reverse()
    prepared = prepare_changed_download(value, extracts, retained)
    assert not prepared.inputs.movements
    assert any(
        finding.code == FindingCode.AMBIGUOUS_BINDING
        and finding.scope.value == record["source_record_id"]
        and finding.blocking
        for finding in prepared.inputs.findings
    )


@pytest.mark.parametrize(
    ("failed_role", "failure"),
    [
        ("posted", "clock_gap"),
        ("authorized", "clock_gap"),
        ("authorized", "calendar_range"),
    ],
)
def test_unresolved_clock_blocks_only_its_required_movement_role(
    failed_role: str,
    failure: str,
) -> None:
    raw = (
        source(
            bank=(("after-gap", "20260309120000[0:GMT]", -5),),
            card=(),
            bank_balance=95,
            card_balance=0,
        )
        .replace(b"20260101000000", b"20260301000000")
        .replace(b"20260201000000", b"20260401000000")
    )
    value, extracts = history((raw,))
    value["configuration"]["as_of"] = "2026-04-01"
    retained = json.loads(extracts[-1])
    record = next(r for r in retained["records"] if r["kind"] == "transaction")
    payload = record["payload"]
    payload["date_authorized"] = "2026-03-08"
    authorization = {**payload["times"][0], "date_role": "authorized"}
    payload["times"].append(authorization)
    failed = next(t for t in payload["times"] if t["date_role"] == failed_role)
    failed.update(
        timestamp_date="2026-03-08",
        timestamp_local="02:30:00",
        timestamp_offset_seconds=None,
        timestamp_zone="America/New_York",
    )
    payload[f"date_{failed_role}"] = "2026-03-08"
    if failure == "calendar_range":
        payload[f"date_{failed_role}"] = "0000-02-29"
        payload["times"].remove(failed)
    if failed_role == "posted":
        payload["legs"][0]["date_posted"] = payload["date_posted"]
    prepared = prepare_changed_download(value, extracts, retained)
    resolution = resolve(prepared.inputs)
    expected_block = failed_role == "posted"
    assert any(
        finding.code == FindingCode.TIME
        and finding.scope.value == record["source_record_id"]
        and finding.blocking == expected_block
        for finding in resolution.findings
    )
    if expected_block:
        assert not prepared.inputs.movements
    else:
        (movement,) = prepared.inputs.movements
        assert movement.when == date(2026, 3, 9)
        assert movement.dates[0] is None
        assert {role for role, _ in movement.times} == {TimeRole.POSTED}
        tables = project_resolution(prepared, resolution)
        assert any(
            row["date_posted"] == "2026-03-09" and row["date_authorized"] is None
            for row in tables["transactions"]
        )
        assert record["source_record_id"] in {
            row["source_record_id"] for row in tables["source_records"]
        }


def test_pending_binding_uses_initiation_before_supplementary_authorization() -> None:
    value, extracts = history()
    retained = json.loads(extracts[-1])
    record = next(
        r
        for r in retained["records"]
        if r["kind"] == "transaction"
        and any(t["date_role"] == "initiated" for t in r["payload"]["times"])
    )
    payload = record["payload"]
    initiation = next(t for t in payload["times"] if t["date_role"] == "initiated")
    payload["date_authorized"] = "2026-01-23"
    payload["times"].insert(
        0,
        {**initiation, "date_role": "authorized", "timestamp_date": "2026-01-23"},
    )
    prepared = prepare_changed_download(value, extracts, retained)
    (pending,) = [m for m in prepared.inputs.movements if m.state == EventState.PENDING]
    assert pending.when == date(2026, 1, 24)
    assert pending.effective_role == TimeRole.INITIATED
    assert pending.dates[2] is None
    event = next(
        event
        for event in resolve(prepared.inputs).events
        if event.state == EventState.PENDING
    )
    assert event.effective_role == TimeRole.INITIATED
    assert dict(event.times)[TimeRole.INITIATED].date == pending.when
