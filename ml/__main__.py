import argparse,json
from pathlib import Path
from .pipeline import build,train
p=argparse.ArgumentParser(); p.add_argument('command',choices=('build','train')); p.add_argument('--dataset-dir',type=Path,required=True); p.add_argument('--scope-dir',type=Path); p.add_argument('--output-dir',type=Path)
if __name__=='__main__':
 a=p.parse_args(); r=build(a.dataset_dir,a.scope_dir) if a.command=='build' else train(a.dataset_dir,a.output_dir); print(json.dumps(r))
