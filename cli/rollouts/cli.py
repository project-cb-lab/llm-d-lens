"""The public rollout command line. No cluster access until explicitly requested."""

import argparse
import json
import sys
from pathlib import Path

from .common import RolloutError
from .prepare import prepare
from .testing import test_rollout
from .validate import validate


def parser():
    root = argparse.ArgumentParser(prog="lens-rollouts", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="snapshot Git refs as v0/v1 and generate version isolation")
    prep.add_argument("--repo", type=Path, required=True, help="path to the curvebender-tools checkout")
    prep.add_argument("--from", dest="from_ref", default="main")
    prep.add_argument("--to", dest="to_ref", required=True, help="commit SHA or pr-<number>")
    prep.add_argument("--model", required=True)
    prep.add_argument("--environment", required=True)
    prep.add_argument(
        "--output", type=Path, help="new rollout directory (default: REPO/.rollouts/MODEL/ENV/FROM-TO)"
    )
    prep.add_argument("--remote", default="origin", help="Git remote used to fetch PR heads")
    prep.add_argument("--chart", help="override the chart repository/name for both refs")
    prep.add_argument("--chart-version", help="override the pinned chart version for both refs")
    check = commands.add_parser("validate", help="render both versions and verify content and isolation")
    check.add_argument("rollout", type=Path)
    check.add_argument(
        "--server-dry-run", action="store_true", help="also run Kubernetes admission validation"
    )
    check.add_argument("--context", help="explicit Kubernetes context for server dry-run")
    test = commands.add_parser("test", help="plan or deploy v1 and run a supplied functional-test Job")
    test.add_argument("rollout", type=Path)
    test.add_argument("--context")
    test.add_argument("--job", type=Path, help="batch/v1 Job; required for --apply")
    test.add_argument("--apply", action="store_true", help="execute the plan against --context")
    test.add_argument(
        "--bootstrap-v0",
        action="store_true",
        help="also create the isolated baseline in a practice environment",
    )
    test.add_argument(
        "--timeout", type=int, default=1800, help="readiness and Job timeout in seconds (default: 1800)"
    )
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            options = vars(args).copy()
            options.pop("command")
            path, state = prepare(**options)
            print(f"Prepared {path}")
            for name, info in state["versions"].items():
                print(f"{name}: {info['commit'][:12]} -> {info['label']}")
            if state["noOp"]:
                print("No-op: v0 and v1 have identical effective serving content.")
        elif args.command == "validate":
            state, docs = validate(args.rollout, context=args.context, server_dry_run=args.server_dry_run)
            print(f"Validated v0 ({len(docs['v0'])} resources) and v1 ({len(docs['v1'])} resources).")
            if state["noOp"]:
                print("No-op: identical versions, no parallel deployment needed.")
        elif args.command == "test":
            result = test_rollout(
                args.rollout,
                context=args.context,
                apply=args.apply,
                bootstrap_v0=args.bootstrap_v0,
                job_path=args.job,
                timeout=args.timeout,
            )
            if args.apply:
                print(f"Functional test {result['status']}: Job {result['job']}")
            else:
                print(json.dumps(result, indent=2))
                print("Plan only. Use --apply --context CONTEXT --job JOB.yaml to execute.")
        return 0
    except (RolloutError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; inspect test-result.yaml for any resources already deployed.", file=sys.stderr)
        return 130
