"""Distribution commands: immutable inputs, atomic cached outputs, no Registry calls."""
import argparse,hashlib,json,logging,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
from analysis.common import digest,output_records,verify_manifest,write_json

LOG=logging.getLogger(__name__)
D=Path('data/distribution/demo-v1');N=Path('data/datasets/full-v1-normalized/normalized');L=Path('data/datasets/full-v1-technical-lag/technical_lag')
OSV=Path('data/distribution/pilot-osv')
ML=Path('artifacts/ml/full-v1-corrected-v2')
FULL_TARGETS=['data/datasets/full-v1-normalized.dvc','data/datasets/full-v1-technical-lag.dvc',
 'data/datasets/pilot-v1-normalized/normalized','data/datasets/pilot-v1-temporal/temporal',
 'data/datasets/pilot-v1-security/security','data/distribution/pilot-osv.dvc','artifacts/ml/full-v1-corrected-v2.dvc']

def require(path):
    if not path.exists():raise ValueError(f'Missing data: {path}. Run make data (demo) or make data-full / make data-ml.')

def doctor():
    import duckdb,numpy,scipy,pyarrow,sklearn
    if sys.version_info[:3]!=(3,14,8):raise ValueError('Use make setup: distribution requires Python 3.14.8')
    for folder in ('processing/technical_lag','processing/normalization'):
        p=subprocess.run(['node','-p',"require('./node_modules/semver/package.json').version"],cwd=folder,capture_output=True,text=True)
        if p.returncode or p.stdout.strip()!='7.8.4':raise ValueError('Missing Node semver@7.8.4; run make setup')
    print(json.dumps({'python':sys.version.split()[0],'node':subprocess.check_output(['node','--version'],text=True).strip(),
        **{m.__name__:m.__version__ for m in (duckdb,numpy,scipy,pyarrow,sklearn)}}))

def data(mode):
    targets={'demo':['data/distribution/demo-v1.dvc','data/distribution/pilot-osv.dvc'],'full':FULL_TARGETS,
             'ml':['artifacts/ml/full-v1-corrected-v2.dvc'],'raw':['data/datasets/full-v1.dvc']}[mode]
    # Credentials remain mounted read-only or supplied by the standard AWS provider chain.
    from botocore.session import Session
    profile=os.environ.get('AWS_PROFILE','technical-lag')
    try:
        session=Session(profile=profile)
        if session.get_credentials() is None:raise ValueError('no AWS credentials')
    except Exception as exc:raise ValueError('DVC/S3 credentials unavailable. Set AWS_PROFILE and AWS_DIR in .env; see README Data. No credentials belong in source code.') from exc
    subprocess.run(['dvc','remote','modify','--local','yandex-s3','profile',profile],check=True)
    subprocess.run(['dvc','config','--local','cache.type','hardlink,copy'],check=True)
    if os.environ.get('DVC_CACHE_DIR'):
        subprocess.run(['dvc','config','--local','cache.dir',os.environ['DVC_CACHE_DIR']],check=True)
    result=subprocess.run(['dvc','pull','-j','2',*targets])
    if result.returncode:raise ValueError('DVC pull failed. Check profile permissions, endpoint/network and free disk; see README Troubleshooting.')

def signature(inputs,params):
    code=[]
    for root in ['infra/distribution','analysis','processing','ml']:
        for p in sorted(Path(root).rglob('*')):
            if p.suffix not in ('.py','.cjs','.json','.lock') or any(s in p.parts for s in ('node_modules','__pycache__','coverage')):continue
            code.append((str(p),digest(p)))
    payload=dict(inputs={str(p):digest(p) for p in inputs},parameters=params,source=code,
                 runtime=sys.version.split()[0],lock=digest(Path('infra/distribution/requirements.lock')),container=digest(Path('Dockerfile')))
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:16],payload

def stage(mode,name,inputs,params,work):
    for p in inputs:require(p)
    key,identity=signature(inputs,params)
    base=Path('artifacts/demo' if mode=='demo' else 'artifacts/reproduced')
    target=base/name/key
    if target.exists():
        verify_manifest(target,'execution.json');LOG.info('reuse %s',target);return target
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.run-',dir=target.parent));output=tmp/'output';started=time.time()
    try:
        work(output)
        records=output_records(output)
        if (output/'analysis-manifest.json').exists():
            records.append({'path':'analysis-manifest.json','sha256':digest(output/'analysis-manifest.json'),'bytes':(output/'analysis-manifest.json').stat().st_size})
        write_json(output/'execution.json',dict(stage=name,mode=mode,identity=identity,seconds=time.time()-started,
            outputs=records))
        os.rename(output,target)
    finally:shutil.rmtree(tmp,ignore_errors=True)
    LOG.info('completed %s in %.2fs -> %s',name,time.time()-started,target)
    return target

def analyses(mode,only=None):
    from analysis.lag_distribution.pipeline import run as a6
    from analysis.version_constraints.pipeline import run as a7
    from analysis.temporal.pipeline import run as b4
    from analysis.vulnerabilities.pipeline import run as b5
    from processing.technical_lag.pipeline import run as lag
    from processing.temporal.pipeline import run as temporal
    from processing.security.pipeline import run as security
    from infra.distribution.inference import run as inference
    is_demo=mode=='demo';n=D/'normalized' if is_demo else N
    require(n/'normalization-manifest.json')
    outputs={}
    requested=['technical-lag','A6','A7','temporal','security','B4','B5','ML'] if is_demo else ['A6','A7','B4','B5','ML']
    if only:requested=[only]
    l=Path('data/datasets/full-v1-technical-lag/technical_lag')
    t=Path('data/datasets/pilot-v1-temporal/temporal');s=Path('data/datasets/pilot-v1-security/security')
    pn=Path('data/datasets/pilot-v1-normalized/normalized')
    if is_demo:
        pn=n
        l=stage(mode,'technical-lag',[n/'normalization-manifest.json'],{},lambda out:lag(n,out));outputs['technical-lag']=str(l)
        if only=='technical-lag':requested=[]
        if not only or only in ('B4','B5','temporal'):
            t=stage(mode,'temporal',[n/'normalization-manifest.json'],{'relations':1000,'snapshots':12},lambda out:temporal(n,out,1000,12));outputs['temporal']=str(t)
        if not only or only in ('B5','security'):
            s=stage(mode,'security',[n/'normalization-manifest.json',OSV/'manifest.json'],{},lambda out:security(OSV,n,out));outputs['security']=str(s)
    for name in requested:
        if name in outputs:continue
        dataset_id='demo-v1' if is_demo else 'full-v1'
        repetitions=100 if is_demo else 2000
        if name=='technical-lag':
            result=stage(mode,name,[n/'normalization-manifest.json'],{},lambda out:lag(n,out))
        elif name in ('A6','A7'):
            action=a6 if name=='A6' else a7
            result=stage(mode,name,[n/'normalization-manifest.json',l/'technical-lag-manifest.json'],{'bootstrap':repetitions},lambda out:action(dataset_id,n,l,out,repetitions,20261027))
        elif name=='B4':
            result=stage(mode,name,[pn/'normalization-manifest.json',t/'temporal-manifest.json'],{},lambda out:b4('demo-v1' if is_demo else 'pilot-v1',t,pn,out))
        elif name=='B5':
            result=stage(mode,name,[pn/'normalization-manifest.json',t/'temporal-manifest.json',s/'security-manifest.json',OSV/'manifest.json'],{},lambda out:b5('demo-v1' if is_demo else 'pilot-v1',t,s,pn,OSV,out))
        elif name=='ML-train':
            from ml.pipeline import train
            result=stage(mode,name,[ML/'dataset/dataset-manifest.json'],{'bootstrap':2000,'fit_existing_models':True},lambda out:train(ML/'dataset',out))
        elif name=='ML':
            models=D/'ml' if is_demo else ML/'evaluation';dataset=models/'ml_dataset.parquet' if is_demo else ML/'dataset/ml_dataset.parquet'
            result=stage(mode,name,[models/('demo-ml-manifest.json' if is_demo else 'run-manifest.json'),dataset],{'frozen_inference':True},lambda out:inference(dataset,models,out,is_demo))
        else:continue
        outputs[name]=str(result)
    index=Path('artifacts/demo/latest.json' if is_demo else 'artifacts/reproduced/latest.json')
    if index.exists():
        previous=json.loads(index.read_text()).get('outputs',{});previous.update(outputs);outputs=previous
    write_json(index,{'mode':mode,'outputs':outputs,'demo_is_not_final_inference':is_demo})
    print(json.dumps({'index':str(index),'outputs':outputs},indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=('doctor','data','demo','full'))
    p.add_argument('--mode',choices=('demo','full','ml','raw'),default='demo');p.add_argument('--stage',choices=('technical-lag','A6','A7','B4','B5','ML','ML-train'))
    a=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    try:
        if a.command=='doctor':doctor()
        elif a.command=='data':data(a.mode)
        else:doctor();analyses('demo' if a.command=='demo' else 'full',a.stage)
    except (ValueError,OSError,ImportError,RuntimeError,subprocess.CalledProcessError) as exc:
        p.exit(2,f'distribution: {exc}\n')
if __name__=='__main__':main()
