"""CLI: python -m aoob_pipeline --pver 4105 --order 1"""

from __future__ import annotations

import argparse
import json
import sys

from aoob_pipeline.config import load_env, project_root_from
from aoob_pipeline.orchestrator import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AOOB multi-agent triage pipeline")
    parser.add_argument("--pver", required=True, help="PVER folder id under PVERs/")
    parser.add_argument("--order", required=True, help="Alarm group_id / Order")
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run explore/prove pairs one agent at a time (lower VRAM)",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Project root (default: auto-detect from .env / analyzer)",
    )
    args = parser.parse_args(argv)

    root = project_root_from() if not args.root else __import__("pathlib").Path(args.root)
    load_env(root)
    try:
        result = run_pipeline(
            pver_id=args.pver,
            order=args.order,
            project_root=root,
            parallel_explore=not args.sequential,
            parallel_prove=not args.sequential,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
