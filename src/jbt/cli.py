"""Local financial commands in the externally installed application."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from jbt.runtime.network import NetworkAccessError, processing


def parser() -> argparse.ArgumentParser:
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
    acquisition.add_argument("--importer", choices=("ofx",), required=True)
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
    return root


def _run(arguments: argparse.Namespace) -> object:
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
            from jbt.domain.cash import CashError  # noqa: PLC0415
            from jbt.domain.errors import DomainError  # noqa: PLC0415
            from jbt.importers.ofx import OfxError  # noqa: PLC0415
            from jbt.storage.local import StorageError  # noqa: PLC0415

            try:
                result = _run(arguments)
            except (
                ContractError,
                StorageError,
                ArtifactIntegrityError,
                BeancountProjectionError,
                CashError,
                DomainError,
                OfxError,
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
