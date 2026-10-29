from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis.lag_distribution.pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--normalized-dir", type=Path, required=True)
    parser.add_argument("--lag-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bootstrap-repetitions", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261027)
    args = parser.parse_args()
    result = run(args.dataset_id, args.normalized_dir, args.lag_dir, args.output_dir,
                 args.bootstrap_repetitions, args.seed)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
