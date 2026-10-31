"""Local financial commands in the externally installed application."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from jbt.runtime.network import NetworkAccessError, processing


def parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - explicit CLI grammar
    """Select local commands without provisioning an execution environment."""
    root = argparse.ArgumentParser(prog="jbt")
    commands = root.add_subparsers(dest="command", required=True)
    acquisition = commands.add_parser(
        "acquire", help="Preserve original local evidence"
    )
    acquisition.add_argument("--root", type=Path, required=True)
    acquisition.add_argument("source", type=Path)
    acquisition.add_argument("--retry-key", required=True)
    acquisition.add_argument("--acquired-at", required=True)
    acquisition.add_argument("--source-scope", required=True)
    acquisition.add_argument(
        "--importer", help="Importer entry-point name; omit to archive only"
    )
    for name in ("build", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name == "build":
            command.add_argument(
                "--project",
                required=True,
                help=(
                    "root-relative project file (JSON; no symlinks), "
                    "captured before execution"
                ),
            )
            command.add_argument("--comparison-baseline")
        else:
            command.add_argument("--baseline", required=True)
    for name in ("show", "review", "correct", "decide", "retract"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--attempt", required=True)
        if name == "show":
            command.add_argument("identifier")
        elif name == "correct":
            command.add_argument("--target", required=True)
            command.add_argument("--key", required=True)
            operation = command.add_mutually_exclusive_group(required=True)
            operation.add_argument("--set", dest="assignment")
            operation.add_argument("--clear")
            operation.add_argument("--split", action="append", default=[])
            command.add_argument("--supersede")
            command.add_argument("--reason", required=True)
        elif name == "retract":
            command.add_argument("entry")
            command.add_argument("--reason", required=True)
        elif name == "decide":
            command.add_argument("--key", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--supersede")
            decisions = command.add_subparsers(dest="decision", required=True)
            identity = decisions.add_parser("identity")
            identity.add_argument(
                "--mode", choices=("key_family", "occurrence"), required=True
            )
            identity.add_argument("--canonical", required=True)
            identity.add_argument("--member", action="append", required=True)
            revision = decisions.add_parser("statement-revision")
            revision.add_argument("--replace", action="append", required=True)
            revision.add_argument(
                "--with", dest="replacement", action="append", required=True
            )
            revision.add_argument("--account", required=True)
            revision.add_argument(
                "--fact-kind", choices=("movement", "balance"), required=True
            )
            revision.add_argument("--basis", required=True)
            revision.add_argument("--start", required=True)
            revision.add_argument("--end", required=True)
            transfer = decisions.add_parser("transfer")
            transfer.add_argument("--outgoing", action="append", default=[])
            transfer.add_argument("--incoming", action="append", default=[])
            transfer.add_argument("--commodity", required=True)
            document = decisions.add_parser("document")
            document.add_argument("--target", required=True)
            document.add_argument("--document", action="append", required=True)
            lifecycle = decisions.add_parser("lifecycle")
            lifecycle.add_argument("--failed-attempt", required=True)
            lifecycle.add_argument("--fee", required=True)
    return root


def _run(arguments: argparse.Namespace) -> object:  # noqa: C901, PLR0912 - closed CLI dispatch
    from jbt.runtime.execution import observe_execution  # noqa: PLC0415
    from jbt.runtime.inputs import canonical_root, confined  # noqa: PLC0415
    from jbt.storage.local import acquire, writer_lock  # noqa: PLC0415

    root = canonical_root(arguments.root)
    if arguments.command == "acquire":
        with writer_lock(root) as lock:
            return asdict(
                acquire(
                    root,
                    arguments.source.absolute(),
                    retry_key=arguments.retry_key,
                    acquired_at=arguments.acquired_at,
                    lock=lock,
                    source_scope_id=arguments.source_scope,
                    importer_id=arguments.importer,
                )
            )
    if arguments.command in {"show", "review", "correct", "decide", "retract"}:
        from jbt.contracts.primitives import require  # noqa: PLC0415
        from jbt.runtime.decisions import correct, retract, show  # noqa: PLC0415

        if arguments.command in {"show", "review"}:
            return show(
                root,
                attempt=arguments.attempt,
                identifier=arguments.identifier
                if arguments.command == "show"
                else None,
            )
        if arguments.command == "retract":
            return retract(
                root,
                attempt=arguments.attempt,
                decision_key=arguments.entry,
                reason=arguments.reason,
            )
        if arguments.command == "decide":
            from datetime import date  # noqa: PLC0415

            from jbt.domain.decisions import IdentityMode  # noqa: PLC0415
            from jbt.runtime.decisions import (  # noqa: PLC0415
                DocumentLinkRequest,
                IdentityRequest,
                LifecycleLinkRequest,
                RevisionRequest,
                TransferRequest,
                decide,
            )

            if arguments.decision == "identity":
                request = IdentityRequest(
                    IdentityMode(arguments.mode),
                    arguments.canonical,
                    tuple(arguments.member),
                )
            elif arguments.decision == "transfer":
                request = TransferRequest(
                    tuple(arguments.outgoing),
                    tuple(arguments.incoming),
                    arguments.commodity,
                )
            elif arguments.decision == "document":
                request = DocumentLinkRequest(
                    arguments.target, tuple(arguments.document)
                )
            elif arguments.decision == "lifecycle":
                request = LifecycleLinkRequest(arguments.failed_attempt, arguments.fee)
            else:
                try:
                    start, end = (
                        date.fromisoformat(arguments.start),
                        date.fromisoformat(arguments.end),
                    )
                except ValueError as error:
                    from jbt.contracts.primitives import ContractError  # noqa: PLC0415

                    code = "decision_period_invalid"
                    raise ContractError(code, "decide") from error
                request = RevisionRequest(
                    tuple(arguments.replace),
                    tuple(arguments.replacement),
                    arguments.account,
                    arguments.fact_kind,
                    arguments.basis,
                    start,
                    end,
                )
            return decide(
                root,
                attempt=arguments.attempt,
                decision_key=arguments.key,
                reason=arguments.reason,
                request=request,
                supersede=arguments.supersede,
            )
        field = value = None
        if arguments.assignment is not None:
            field, separator, value = arguments.assignment.partition("=")
            require(bool(separator), "assignment_syntax", "correct")
        elif arguments.clear is not None:
            field = arguments.clear
        return correct(
            root,
            attempt=arguments.attempt,
            identifier=arguments.target,
            decision_key=arguments.key,
            reason=arguments.reason,
            field=field,
            value=value,
            children=tuple(arguments.split),
            supersede=arguments.supersede,
        )
    from jbt.runtime.worker import build, verify  # noqa: PLC0415

    if arguments.command == "build":
        confined(root, arguments.project)
        return build(
            root,
            project=arguments.project,
            execution=observe_execution(),
            comparison_baseline=arguments.comparison_baseline,
        )
    return verify(
        root, baseline_digest=arguments.baseline, execution=observe_execution()
    )


def main() -> int:
    """Emit safe diagnostics and refuse attempted application network access."""
    arguments = parser().parse_args()
    try:
        with processing():
            from jbt.artifacts.beancount import (  # noqa: PLC0415
                BeancountProjectionError,
            )
            from jbt.artifacts.integrity import ArtifactIntegrityError  # noqa: PLC0415
            from jbt.contracts.primitives import ContractError  # noqa: PLC0415
            from jbt.domain.errors import DomainError  # noqa: PLC0415
            from jbt.extraction import ExtractionError  # noqa: PLC0415
            from jbt.storage.local import StorageError  # noqa: PLC0415

            try:
                result = _run(arguments)
            except (
                ContractError,
                StorageError,
                ArtifactIntegrityError,
                BeancountProjectionError,
                DomainError,
                ExtractionError,
            ) as error:
                sys.stderr.write(f"jbt: {error}\n")
                return 1
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    except NetworkAccessError as error:
        sys.stderr.write(f"jbt: {error}\n")
        return 1
    except OSError as error:
        sys.stderr.write(f"jbt: filesystem operation failed (errno {error.errno})\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
