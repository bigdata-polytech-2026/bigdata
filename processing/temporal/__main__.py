import argparse, json
from pathlib import Path
from .pipeline import MAX_RELATIONS, MAX_SNAPSHOTS, run
p=argparse.ArgumentParser(); p.add_argument("--input-dir",type=Path,default=Path("data/datasets/pilot-v1-normalized/normalized")); p.add_argument("--output-dir",type=Path,default=Path("data/datasets/pilot-v1-temporal/temporal")); p.add_argument("--relation-limit",type=int,default=MAX_RELATIONS); p.add_argument("--snapshots-per-relation",type=int,default=MAX_SNAPSHOTS)
if __name__ == "__main__":
 a=p.parse_args(); print(json.dumps(run(a.input_dir,a.output_dir,a.relation_limit,a.snapshots_per_relation),sort_keys=True))
