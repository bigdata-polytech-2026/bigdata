"""Maintainer clean-check driver. Host stdlib only; all project code runs in Docker."""
import argparse,json,os,shutil,subprocess,time
from datetime import datetime,timezone
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase',choices=('prepare','full'),required=True)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--aws-dir',type=Path,required=True)
    p.add_argument('--profile',default='technical-lag')
    p.add_argument('--shared-full-cache',type=Path)
    a=p.parse_args();source=Path.cwd();clone=a.directory.resolve();logs=source/'artifacts/reproducibility-check';logs.mkdir(parents=True,exist_ok=True)
    report=logs/'latest.json'
    record=json.loads(report.read_text()) if report.exists() and a.phase=='full' else {'environment':{'host':os.uname().sysname,'arch':os.uname().machine,'clean_clone':str(clone),'project_runtime':'Docker Linux; no host venv or node_modules','full_cache':'not yet configured'},'commands':[]}
    def save():report.write_text(json.dumps(record,indent=2)+'\n')
    def command(name,argv,cwd,expect_success=True):
        path=logs/(name+'.log');start=time.monotonic();when=datetime.now(timezone.utc).isoformat()
        with path.open('w') as stream:r=subprocess.run(argv,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT)
        row={'name':name,'command':argv,'seconds':round(time.monotonic()-start,3),'exit_code':r.returncode,'started_utc':when,'log':str(path.relative_to(source))}
        record['commands'].append(row);save();print(json.dumps(row),flush=True)
        if expect_success and r.returncode:raise SystemExit(f'{name} failed; inspect {path}')
        if not expect_success and not r.returncode:raise SystemExit(f'{name} unexpectedly succeeded')
    env=clone/'.env'
    if a.phase=='prepare':
        if clone.exists():raise SystemExit('clean directory already exists; choose a new path')
        branch=subprocess.check_output(['git','branch','--show-current'],text=True).strip()
        remote=subprocess.check_output(['git','remote','get-url','origin'],text=True).strip()
        command('clone',['git','clone','--single-branch','--branch',branch,remote,str(clone)],source)
        record['commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
        shutil.copyfile(clone/'.env.example',env)
        with env.open('a') as f:f.write('\nCOMPOSE_PROJECT_NAME=bigdata-a11-check\n')
        command('setup',['make','setup'],clone)
        command('missing-credentials',['make','data'],clone,False)
        assert 'DVC/S3 credentials unavailable' in (logs/'missing-credentials.log').read_text(), 'wrong missing-credentials failure'
        with env.open('a') as f:f.write(f'\nAWS_DIR={a.aws_dir.resolve()}\nAWS_PROFILE={a.profile}\n')
        command('data-demo',['make','data'],clone)
        command('tests',['make','test'],clone)
        command('demo',['make','demo'],clone)
        index=json.loads((clone/'artifacts/demo/latest.json').read_text())
        assert set(index['outputs'])=={'technical-lag','A6','A7','temporal','security','B4','B5','ML'}
        for stage,path in index['outputs'].items():assert (clone/path/'execution.json').exists(),stage
        record['demo_index']=index
        command('demo-repeat',['make','demo'],clone)
        assert json.loads((clone/'artifacts/demo/latest.json').read_text())==index
        record['demo_complete']=True;save()
    else:
        assert clone.exists() and record.get('demo_complete')
        if a.shared_full_cache:
            cache=a.shared_full_cache.resolve();storage=cache.parents[1]
            subdir=clone.relative_to(storage)
            with env.open('a') as f:f.write(f'\nPROJECT_STORAGE_ROOT={storage}\nPROJECT_SUBDIR={subdir}\nDVC_CACHE_DIR=/workspace/{cache.relative_to(storage)}\n')
            record['environment']['full_cache']='shared immutable DVC cache on same filesystem (hardlinks); demo cache was cold'
        else:record['environment']['full_cache']='cold per-clone DVC cache'
        command('data-full',['make','data-full'],clone)
        command('a6-full',['make','a6-full'],clone)
        command('ml-full',['make','ml-full'],clone)
        command('ml-full-repeat',['make','ml-full'],clone)
        record['full_index']=json.loads((clone/'artifacts/reproduced/latest.json').read_text())
        record['full_complete']=True;save()
if __name__=='__main__':main()
