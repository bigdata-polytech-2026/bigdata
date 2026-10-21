from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build OSV security features for a normalized pilot dataset")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/datasets/pilot-v1"))
    parser.add_argument("--versions-dir", type=Path, default=Path("data/datasets/pilot-v1-normalized/normalized"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/datasets/pilot-v1-security/security"))
    args = parser.parse_args(argv)
    print(json.dumps(run(args.dataset_dir, args.versions_dir, args.output_dir), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
