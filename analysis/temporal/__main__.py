from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--temporal-dir", type=Path, required=True)
    parser.add_argument("--normalized-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.dataset_id, args.temporal_dir, args.normalized_dir, args.output_dir), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
