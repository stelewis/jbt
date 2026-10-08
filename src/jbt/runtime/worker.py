"""Build complete generations and compare uncached financial replay."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from jbt.artifacts.beancount import BeancountProjectionError
from jbt.artifacts.integrity import decode_document, require_digest
from jbt.artifacts.reader import PinnedSnapshot, read_pinned, read_snapshot
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json
from jbt.domain.cash import CashError
from jbt.domain.errors import DomainError
from jbt.importers.ofx import OfxError
from jbt.runtime.build import execute
from jbt.runtime.execution import ObservedExecution, decode_execution
from jbt.runtime.inputs import capture_inputs, confined, load_inputs
from jbt.runtime.network import processing
from jbt.runtime.project import document, objects, text
from jbt.storage.local import Current, publish, read_retained, writer_lock

if TYPE_CHECKING:
    from jbt.runtime.inputs import InputSnapshot

type ReplayFailure = (
    ContractError | DomainError | CashError | OfxError | BeancountProjectionError
)


class BuildReport(TypedDict):
    """Published generation identity and application execution counts."""

    generation: str
    descriptor_digest: str
    manifest_digest: str
    financial_digest: str
    executed: int
    reused: int
    producer: dict[str, object]


class VerificationReport(TypedDict):
    """Uncached financial comparison with both observed environments."""

    baseline: str
    equal: bool
    executed: int
    reused: int
    producer: dict[str, object]
    recorded_environment: dict[str, object]
    observed_environment: dict[str, object]


def _environment(execution: ObservedExecution) -> dict[str, object]:
    return {
        key: value
        for key, value in execution.document().items()
        if key in {"runtime", "distributions"}
    }


class ReplayMismatchError(ContractError):
    """Located financial failure with descriptive execution facts."""

    def __init__(
        self,
        failure: ReplayFailure,
        recorded: ObservedExecution,
        observed: ObservedExecution,
    ) -> None:
        """Keep the original failure's location and both observed executions."""
        code = failure.constraint if isinstance(failure, DomainError) else failure.code
        location = (
            failure.record_id
            if isinstance(failure, BeancountProjectionError)
            else failure.location
        )
        super().__init__(code, location)
        self.recorded = recorded
        self.observed = observed

    def __str__(self) -> str:
        """Describe the mismatch without exposing financial field values."""
        environments = json.dumps(
            {
                "recorded_environment": _environment(self.recorded),
                "observed_environment": _environment(self.observed),
            },
            sort_keys=True,
        )
        return f"{ContractError.__str__(self)}; {environments}"


def _pin(root: Path, digest: str) -> PinnedSnapshot:
    require_digest(digest)
    return read_pinned(root, digest)


def _selection(baseline: PinnedSnapshot) -> str:
    payload = baseline.outputs.get("inputs.json")
    if payload is None:
        code = "missing_input_selection"
        raise ContractError(code, "verify")
    selector = document(decode_document(payload, "inputs.json"))
    require(
        set(selector) == {"schema_version", "snapshot_digest", "selection"}
        and type(selector["schema_version"]) is int
        and selector["schema_version"] == 1,
        "input_selector_schema",
        "verify",
    )
    digest = text(selector["snapshot_digest"], "snapshot_digest")
    require_digest(digest)
    return digest


def _financial_outputs(snapshot: PinnedSnapshot) -> dict[str, bytes]:
    return {
        text(item["path"], "output"): snapshot.outputs[text(item["path"], "output")]
        for item in objects(snapshot.manifest["artifacts"], "artifacts")
        if item["kind"] in {"ledger", "summary"}
    }


def _compare(baseline: PinnedSnapshot, candidate: PinnedSnapshot) -> None:
    """Compare financial facts without requiring identical runtime metadata."""
    require(
        set(baseline.tables) == set(candidate.tables),
        "table_inventory_mismatch",
        "verify",
    )
    for name in sorted(baseline.tables):
        if name == "build":
            continue
        expected, observed = baseline.tables[name], candidate.tables[name]
        require(len(expected) == len(observed), "table_row_count_mismatch", name)
        for ordinal, (left, right) in enumerate(zip(expected, observed, strict=True)):
            for field in sorted(set(left) | set(right)):
                require(
                    field in left and field in right and left[field] == right[field],
                    "table_mismatch",
                    f"{name}[row={ordinal}].{field}",
                )
    expected_outputs, observed_outputs = (
        _financial_outputs(baseline),
        _financial_outputs(candidate),
    )
    require(
        expected_outputs.keys() == observed_outputs.keys(),
        "output_inventory_mismatch",
        "verify",
    )
    for path, payload in expected_outputs.items():
        require(payload == observed_outputs[path], "output_mismatch", path)
    for key in (
        "numeric_limits",
        "bindings",
        "effective_declarations",
        "derivations",
        "checks",
        "outputs",
        "logical_content_digest",
    ):
        require(
            baseline.manifest[key] == candidate.manifest[key],
            "financial_manifest_mismatch",
            key,
        )
    require(
        [
            (item["entity_id"], item["as_of"])
            for item in objects(baseline.manifest["entities"], "entities")
        ]
        == [
            (item["entity_id"], item["as_of"])
            for item in objects(candidate.manifest["entities"], "entities")
        ],
        "financial_manifest_mismatch",
        "entities",
    )


def build(
    root: Path,
    *,
    project: str,
    execution: ObservedExecution,
    comparison_baseline: str | None,
) -> BuildReport:
    """Lock selection, execution, check authorization and publication."""
    with processing() as guard:
        confined(root, project)
        with writer_lock(root) as lock:
            baseline = _pin(root, comparison_baseline) if comparison_baseline else None
            pinned = (
                Current(root / "generations" / comparison_baseline, comparison_baseline)
                if comparison_baseline is not None
                else None
            )
            selected = capture_inputs(
                root,
                project_path=project,
                execution=execution,
                baseline=pinned,
                lock=lock,
            )
            snapshot, project_document = load_inputs(root, selected.digest)
            require(snapshot == selected, "input_snapshot_changed", "build")
            staging = confined(root, "staging")
            staging.mkdir(mode=0o700, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="generation-", dir=staging
            ) as directory:
                destination = Path(directory)
                result = execute(
                    root,
                    snapshot,
                    project_document,
                    destination,
                    execution=execution,
                    cache=confined(root, "cache") if execution.is_release else None,
                    baseline=baseline.tables if baseline else None,
                )
                guard.check()
                published = publish(
                    root, destination, result.snapshot.descriptor_digest, lock=lock
                )
        return {
            "generation": published.path.relative_to(root).as_posix(),
            "descriptor_digest": published.descriptor_digest,
            "manifest_digest": result.snapshot.manifest_digest,
            "financial_digest": result.snapshot.financial_digest,
            "executed": len(result.executed),
            "reused": len(result.reused),
            "producer": execution.document()["producer"],
        }


def verify(
    root: Path, *, baseline_digest: str, execution: ObservedExecution
) -> VerificationReport:
    """Check the recorded producer's result in this observed environment."""
    with processing() as guard:
        baseline = _pin(root, baseline_digest)
        digest = _selection(baseline)
        snapshot, project = load_inputs(root, digest)
        historical = decode_execution(read_retained(root, snapshot.execution_digest))
        require(
            execution.is_release
            and historical.is_release
            and execution.producer_digest == historical.producer_digest,
            "historical_producer_mismatch_install_recorded_release",
            historical.producer_version,
        )
        require(
            baseline.outputs.get("execution.json") == historical.payload
            and baseline.outputs["inputs.json"] == _input_selector(root, snapshot),
            "historical_selection_mismatch",
            "verify",
        )
        with tempfile.TemporaryDirectory(prefix="jbt-verify-") as directory:
            destination = Path(directory)
            try:
                result = execute(
                    root,
                    snapshot,
                    project,
                    destination,
                    execution=execution,
                    cache=None,
                    baseline=_comparison(root, snapshot),
                )
                candidate = read_snapshot(
                    destination, result.snapshot.descriptor_digest
                )
                _compare(baseline, candidate)
            except (
                ContractError,
                DomainError,
                CashError,
                OfxError,
                BeancountProjectionError,
            ) as error:
                raise ReplayMismatchError(error, historical, execution) from error
        guard.check()
        return {
            "baseline": baseline_digest,
            "equal": True,
            "executed": len(result.executed),
            "reused": len(result.reused),
            "producer": execution.document()["producer"],
            "recorded_environment": _environment(historical),
            "observed_environment": _environment(execution),
        }


def _input_selector(root: Path, snapshot: InputSnapshot) -> bytes:
    return encode_json(
        {
            "schema_version": 1,
            "snapshot_digest": snapshot.digest,
            "selection": decode_document(
                read_retained(root, snapshot.digest), "inputs"
            ),
        },
        integer_strings=False,
    )


def _comparison(
    root: Path, snapshot: InputSnapshot
) -> dict[str, list[dict[str, object]]] | None:
    if snapshot.baseline_digest is None:
        return None
    require(
        snapshot.baseline_path == f"generations/{snapshot.baseline_digest}",
        "baseline_path_mismatch",
        "verify",
    )
    return _pin(root, snapshot.baseline_digest).tables
