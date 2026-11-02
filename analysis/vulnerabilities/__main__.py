from __future__ import annotations
import argparse, json
from pathlib import Path
from .pipeline import run

p = argparse.ArgumentParser()
p.add_argument("--dataset-id", required=True); p.add_argument("--temporal-dir", type=Path, required=True)
p.add_argument("--security-dir", type=Path, required=True); p.add_argument("--normalized-dir", type=Path, required=True)
p.add_argument("--pilot-dir", type=Path, required=True); p.add_argument("--output-dir", type=Path, required=True)
if __name__ == "__main__":
    a = p.parse_args(); print(json.dumps(run(a.dataset_id, a.temporal_dir, a.security_dir, a.normalized_dir, a.pilot_dir, a.output_dir), ensure_ascii=False, sort_keys=True))
