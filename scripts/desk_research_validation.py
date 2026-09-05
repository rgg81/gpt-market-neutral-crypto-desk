#!/usr/bin/env python3
"""Register and evaluate frozen PAPER-desk research trials.

This command is measurement-only. It cannot promote a policy or alter the live paper book.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from futures_fund.research_validation import (
    FrozenReturnMatrix,
    TrialDefinition,
    evaluate_frozen_trials,
    load_trial_registry,
    register_frozen_trial,
    validate_frozen_trial_window,
    write_research_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    register = subcommands.add_parser("register", help="append one immutable trial definition")
    register.add_argument("--registry", required=True)
    register.add_argument("--definition", required=True)

    evaluate = subcommands.add_parser("evaluate", help="evaluate common-horizon trial returns")
    evaluate.add_argument("--registry", required=True)
    evaluate.add_argument("--returns", required=True)
    evaluate.add_argument("--benchmark", required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument("--periods-per-year", type=float, default=365.0)
    evaluate.add_argument("--groups", type=int, default=6)
    evaluate.add_argument("--test-groups", type=int, default=2)
    evaluate.add_argument("--purge", type=int, default=0)
    evaluate.add_argument("--embargo", type=int, default=0)
    evaluate.add_argument("--target-sharpe", type=float, default=1.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "register":
        raw = json.loads(Path(args.definition).read_text())
        row = register_frozen_trial(args.registry, TrialDefinition.model_validate(raw))
        print(json.dumps(row.model_dump(mode="json"), sort_keys=True))
        return 0

    returns_path = Path(args.returns)
    returns_bytes = returns_path.read_bytes()
    payload = FrozenReturnMatrix.model_validate_json(returns_bytes)
    raw_returns = payload.returns
    returns = {
        str(name): [float(value) for value in values]
        for name, values in raw_returns.items()
    }
    registry = load_trial_registry(args.registry)
    validate_frozen_trial_window(payload, registry)
    report = evaluate_frozen_trials(
        returns,
        registry,
        benchmark_trial_id=args.benchmark,
        periods_per_year=args.periods_per_year,
        n_groups=args.groups,
        n_test_groups=args.test_groups,
        purge_observations=args.purge,
        embargo_observations=args.embargo,
        target_annualized_sharpe=args.target_sharpe,
    )
    report["return_matrix"] = {
        "as_of_ts": payload.as_of_ts,
        "return_basis": payload.return_basis,
        "observation_start": payload.observation_timestamps[0],
        "observation_end": payload.observation_timestamps[-1],
        "data_lineage_sha256": payload.data_lineage_sha256,
        "universe_lineage_sha256": payload.universe_lineage_sha256,
        "execution_model_sha256": payload.execution_model_sha256,
        "input_file_sha256": hashlib.sha256(returns_bytes).hexdigest(),
    }
    digest = write_research_report(args.output, report)
    print(json.dumps({"output": args.output, "sha256": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
