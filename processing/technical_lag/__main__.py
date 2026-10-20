from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m processing.technical_lag",
                                     description="Calculate historical technical-lag metrics")
    datasets = parser.add_subparsers(dest="dataset", required=True)
    pilot = datasets.add_parser("pilot", help="calculate lag for every normalized pilot requirement")
    pilot.add_argument("--input-dir", type=Path,
                       default=Path("data/datasets/pilot-v1-normalized/normalized"))
    pilot.add_argument("--output-dir", type=Path,
                       default=Path("data/datasets/pilot-v1-technical-lag/technical_lag"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from processing.technical_lag.pipeline import run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        report = run(args.input_dir, args.output_dir)
    except ImportError as exc:
        parser.error(f"{exc}; install processing/technical_lag/requirements.txt")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"technical-lag calculation failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
