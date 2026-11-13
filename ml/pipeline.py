"""Scope-compliant, point-in-time ML construction and frozen holdout evaluation."""
from __future__ import annotations
import json, logging, platform, shutil, subprocess
from datetime import datetime, timezone
from pathlib import Path
from analysis.common import (atomic_directory,cleanup_directory,configure_plotting,digest,finalize_directory,
    output_records,sql_string,verify_manifest,write_csv,write_json)
from ml.scope.audit import run as scope_audit
from ml.features import classify_requirements,add_history
from ml.evaluation import SEED,majority_probability,choose_threshold,weighted_metrics,bootstrap_intervals

NUMERIC=('current_time_lag_days','current_version_lag','major_available','minor_available','patch_available',
 'consumer_age_days','consumer_stable_age_days','dependency_age_days','dependency_releases_90d',
 'dependency_releases_365d','production_dependency_count','consumer_releases_365d',
 'prior_version_lag_30d','prior_version_lag_90d','prior_major_30d','nonmonotone_publish_order')
CATEGORICAL=('constraint_type',)
CONSTRAINT_CATEGORIES=tuple(sorted(('exact','caret','tilde','wildcard','range','prerelease','unsupported','invalid','unknown')))
LOG=logging.getLogger(__name__)

def _split(t):
    date=datetime.fromisoformat(t.replace('Z','+00:00')).astimezone(timezone.utc)
    for boundary,name in [('2019-07-05','train'),('2020-01-01','purged'),('2022-07-05','validation'),('2023-01-01','purged')]:
        if date<datetime.fromisoformat(boundary).replace(tzinfo=timezone.utc):return name
    return 'test'

def _feature_matrix(rows):
    import numpy as np
    return np.array([[np.nan if r.get(f) is None else r[f] for f in NUMERIC]
        +[r.get('constraint_type') if r.get('constraint_type') in CONSTRAINT_CATEGORIES else 'unknown']
        for r in rows],dtype=object)

def _sources():
    paths=['ml/pipeline.py','ml/reporting.py','ml/features.py','ml/history.cjs','ml/evaluation.py','ml/scope/audit.py',
        'ml/scope/evaluate.cjs','analysis/version_constraints/classifier.cjs','processing/technical_lag/core.cjs',
        'processing/technical_lag/package-lock.json','docs/ml-features.csv','docs/ml-scope.md','analysis/requirements.txt']
    return {p:digest(Path(p)) for p in paths}

def build(dataset_dir:Path,scope_dir:Path)->dict:
    import duckdb,pyarrow as pa,pyarrow.parquet as pq
    n=Path('data/datasets/full-v1-normalized/normalized');l=Path('data/datasets/full-v1-technical-lag/technical_lag')
    verify_manifest(n,'normalization-manifest.json');lm=verify_manifest(l,'technical-lag-manifest.json')
    if lm['build']['input_normalization_manifest_sha256']!=digest(n/'normalization-manifest.json'):raise ValueError('normalization lineage mismatch')
    semver=subprocess.check_output(['node','-p',"require('./processing/technical_lag/node_modules/semver/package.json').version"],text=True).strip()
    if semver!='7.8.4':raise ValueError('requires semver@7.8.4')
    if scope_dir.exists():
        sm=verify_manifest(scope_dir,'analysis-manifest.json')
        for p,h in sm['input_manifests'].items():
            if digest(Path(p))!=h:raise ValueError('cached scope input mismatch')
        for p,h in sm['sources'].items():
            if digest(Path(p))!=h:raise ValueError('cached scope source mismatch')
    else:scope_audit(scope_dir,60000)
    endpoints=json.loads((scope_dir/'endpoint-audit.json').read_text())
    anchors=[r for r in endpoints if r['horizon']==180 and r['label'] is not None and _split(r['observation_time'])!='purged']
    categories=classify_requirements([r['current_requirement'] for r in anchors])
    for r in anchors:
        r['split']=_split(r['observation_time']);r['constraint_type']=categories[r['current_requirement']]
        t=datetime.fromisoformat(r['observation_time'].replace('Z','+00:00'));u=datetime.fromisoformat(r['endpoint'].replace('Z','+00:00'))
        assert (u-t).total_seconds()==180*86400 and not r['censored']
        if r['split']=='train':assert u<datetime(2020,1,1,tzinfo=timezone.utc)
        if r['split']=='validation':assert u<datetime(2023,1,1,tzinfo=timezone.utc)
    assert len({(r['source_package'],r['dependency_name']) for r in anchors})==len(anchors)
    staging=atomic_directory(dataset_dir);db=duckdb.connect()
    db.execute("SET memory_limit='1GB'");db.execute('SET threads=2');db.execute("SET TimeZone='UTC'")
    db.execute(f'SET temp_directory={sql_string(staging/".spill")}')
    try:
        db.register('anchors',pa.Table.from_pylist(anchors))
        db.execute(f"CREATE VIEW packages AS SELECT * FROM read_parquet({sql_string(n/'packages/*.parquet')})")
        db.execute(f"CREATE VIEW quality AS SELECT * FROM read_parquet({sql_string(n/'version_quality.parquet')})")
        db.execute(f"CREATE VIEW inputs AS SELECT * FROM read_parquet({sql_string(n/'technical_lag_inputs.parquet')})")
        db.execute(f"CREATE VIEW lag AS SELECT * FROM read_parquet({sql_string(l/'technical_lag.parquet')})")
        LOG.info('Reconstructing prior consumer manifests and lag at t-30/t-90')
        add_history(db,staging)
        db.execute("""CREATE VIEW stable AS SELECT * FROM quality WHERE version_valid AND NOT is_prerelease""")
        db.execute("""CREATE TABLE ml AS WITH depcount AS (
          SELECT source_package,source_version,count(*) production_dependency_count FROM inputs
          WHERE dependency_type='dependencies' AND NOT shadowed_by_optional GROUP BY 1,2)
          SELECT a.source_package,a.dependency_name,a.source_version,a.observation_time,a.endpoint,a.label,a.reason,a.split,a.constraint_type,
          a.current_time_lag_days,a.current_version_lag,
          (l.update_type='major')::INT major_available,(l.update_type='minor')::INT minor_available,(l.update_type='patch')::INT patch_available,
          CASE WHEN try_cast(cp.created_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)
            THEN date_diff('microsecond',try_cast(cp.created_at AS TIMESTAMPTZ),try_cast(a.observation_time AS TIMESTAMPTZ))/86400000000.0 END consumer_age_days,
          (SELECT date_diff('microsecond',min(try_cast(q.published_at AS TIMESTAMPTZ)),try_cast(a.observation_time AS TIMESTAMPTZ))/86400000000.0
            FROM stable q WHERE q.package_name=a.source_package AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) consumer_stable_age_days,
          CASE WHEN try_cast(dp.created_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)
            THEN date_diff('microsecond',try_cast(dp.created_at AS TIMESTAMPTZ),try_cast(a.observation_time AS TIMESTAMPTZ))/86400000000.0 END dependency_age_days,
          (SELECT count(*) FROM stable q WHERE q.package_name=a.dependency_name AND try_cast(q.published_at AS TIMESTAMPTZ)>try_cast(a.observation_time AS TIMESTAMPTZ)-INTERVAL '90 days' AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) dependency_releases_90d,
          (SELECT count(*) FROM stable q WHERE q.package_name=a.dependency_name AND try_cast(q.published_at AS TIMESTAMPTZ)>try_cast(a.observation_time AS TIMESTAMPTZ)-INTERVAL '365 days' AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) dependency_releases_365d,
          d.production_dependency_count,
          (SELECT count(*) FROM stable q WHERE q.package_name=a.source_package AND try_cast(q.published_at AS TIMESTAMPTZ)>try_cast(a.observation_time AS TIMESTAMPTZ)-INTERVAL '365 days' AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) consumer_releases_365d,
          p.prior_version_lag_30d,p.prior_version_lag_90d,p.prior_major_30d,l.nonmonotone_publish_order::INT nonmonotone_publish_order
          FROM anchors a JOIN lag l ON a.source_package=l.source_package AND a.source_version=l.source_version
            AND a.dependency_name=l.dependency_name AND l.dependency_type='dependencies'
          LEFT JOIN packages cp ON cp.package_name=a.source_package LEFT JOIN packages dp ON dp.package_name=a.dependency_name
          LEFT JOIN depcount d ON d.source_package=a.source_package AND d.source_version=a.source_version LEFT JOIN priors p ON p.source_package=a.source_package AND p.dependency_name=a.dependency_name""")
        assert db.execute('SELECT count(*) FROM ml').fetchone()[0]==len(anchors)
        assert not db.execute("SELECT count(*) FROM ml WHERE major_available<>1 OR production_dependency_count IS NULL").fetchone()[0]
        pq.write_table(db.execute('SELECT * FROM ml ORDER BY split,source_package,dependency_name').fetch_arrow_table(),staging/'ml_dataset.parquet',compression='zstd')
        counts=db.execute('SELECT split,label,count(*) n FROM ml GROUP BY 1,2 ORDER BY 1,2').fetchall()
        write_csv(staging/'class-distribution.csv',('split','label','n'),counts)
        coverage=[(f,db.execute(f'SELECT count(*) FILTER(WHERE {f} IS NULL),min({f}),max({f}) FROM ml').fetchone()) for f in NUMERIC]
        write_csv(staging/'feature-coverage.csv',('feature','missing','min','max'),[(f,*r) for f,r in coverage])
        report=dict(version='2.0.0',rows=len(anchors),splits={s:sum(r[2] for r in counts if r[0]==s) for s in ('train','validation','test')},
          features=list(NUMERIC+CATEGORICAL),label='major_lag_at_t_plus_180d',sources=_sources(),
          inputs={str(p):digest(p) for p in (n/'normalization-manifest.json',l/'technical-lag-manifest.json',scope_dir/'analysis-manifest.json')},
          runtime=dict(python=platform.python_version(),duckdb=duckdb.__version__,pyarrow=pa.__version__,semver=semver),
          assertions=dict(unique_incidents=True,utc_horizon=True,purged_endpoints=True,feature_cutoffs='current <= t; prior <= t-30d/t-90d'))
        db.close();shutil.rmtree(staging/'.spill',ignore_errors=True)
        report['outputs']=output_records(staging);write_json(staging/'dataset-manifest.json',report)
        finalize_directory(staging,dataset_dir);return report
    except BaseException:
        db.close();cleanup_directory(staging);raise


def train(dataset_dir:Path,out:Path,repetitions=2000)->dict:
    import numpy as np,pyarrow.parquet as pq,sklearn,joblib
    from sklearn.compose import ColumnTransformer
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.impute import SimpleImputer,MissingIndicator
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline,FeatureUnion
    from sklearn.preprocessing import OneHotEncoder,StandardScaler
    from sklearn.feature_selection import VarianceThreshold
    from ml.reporting import diagnostics
    dm=verify_manifest(dataset_dir,'dataset-manifest.json')
    if dm['sources']!=_sources():raise ValueError('dataset sources do not match current implementation')
    rows=pq.read_table(dataset_dir/'ml_dataset.parquet').to_pylist()
    groups={s:[r for r in rows if r['split']==s] for s in ('train','validation','test')}
    ys={s:np.array([r['label'] for r in rs]) for s,rs in groups.items()}
    xs={s:_feature_matrix(rs) for s,rs in groups.items()}
    def preprocessing():
        numeric=FeatureUnion([('values',Pipeline([('impute',SimpleImputer(strategy='median',keep_empty_features=True)),('scale',StandardScaler())])),('missing',MissingIndicator(features='all'))])
        return ColumnTransformer([('num',numeric,list(range(len(NUMERIC)))),('cat',OneHotEncoder(categories=[list(CONSTRAINT_CATEGORIES)],handle_unknown='error',sparse_output=False),[len(NUMERIC)])],sparse_threshold=0)
    models={name:Pipeline([('pre',preprocessing()),('variance',VarianceThreshold()),('model',model)]) for name,model in [
        ('logistic',LogisticRegression(C=1,penalty='l2',solver='lbfgs',max_iter=2000,class_weight=None,random_state=SEED)),
        ('tree',DecisionTreeClassifier(max_depth=6,min_samples_leaf=50,random_state=SEED))]}
    staging=atomic_directory(out)
    try:
        scores={'majority':majority_probability(ys['train'],len(ys['test'])),'persistence':np.ones(len(ys['test'])),
                'severity_rule':np.array([r['current_version_lag'] for r in groups['test']],dtype=float)}
        thresholds={'majority':.5,'persistence':.5,'severity_rule':choose_threshold(ys['validation'],[r['current_version_lag'] for r in groups['validation']])}
        validation=[];importance={}
        for name,model in models.items():
            LOG.info('Fitting %s on train; selecting threshold on validation',name)
            model.fit(xs['train'],ys['train'])
            pv=model.predict_proba(xs['validation'])[:,1]
            thresholds[name]=choose_threshold(ys['validation'],pv)
            scores[name]=model.predict_proba(xs['test'])[:,1]
            validation.append(dict(model=name,threshold=thresholds[name],**weighted_metrics(ys['validation'],pv,thresholds[name])))
            joblib.dump(model,staging/f'{name}.joblib')
            names=model.named_steps['pre'].get_feature_names_out(NUMERIC+CATEGORICAL)
            names=names[model.named_steps['variance'].get_support()]
            estimator=model.named_steps['model']
            vals=estimator.coef_[0] if name=='logistic' else estimator.feature_importances_
            importance[name]=sorted([(n,float(v),abs(float(v))) for n,v in zip(names,vals)],key=lambda x:-x[2])
            allnames=model.named_steps['pre'].get_feature_names_out(NUMERIC+CATEGORICAL)
            write_json(staging/f'{name}-dropped-constant-features.json',list(allnames[~model.named_steps['variance'].get_support()]))
        results=[dict(model=name,support=len(ys['test']),positive_rate=float(ys['test'].mean()),threshold=thresholds[name],
                   **weighted_metrics(ys['test'],p,thresholds[name])) for name,p in scores.items()]
        write_json(staging/'metrics.json',results);write_json(staging/'validation-metrics.json',validation)
        LOG.info('Computing %d paired consumer-cluster bootstrap replicates',repetitions)
        intervals=bootstrap_intervals(ys['test'],scores,thresholds,[r['source_package'] for r in groups['test']],repetitions,SEED)
        write_json(staging/'bootstrap-intervals.json',intervals)
        diagnostics(staging,groups,ys,scores,thresholds,importance)
        write_json(staging/'run-manifest.json',dict(version='2.0.0',seed=SEED,primary_model='logistic',tree='prespecified exploratory comparator',
          thresholds=thresholds,threshold_selection='validation max F1; ties precision then threshold',
          dataset_manifest_sha256=digest(dataset_dir/'dataset-manifest.json'),sources=_sources(),features=list(NUMERIC+CATEGORICAL),
          runtime=dict(python=platform.python_version(),sklearn=sklearn.__version__,numpy=np.__version__,joblib=joblib.__version__),
          bootstrap=dict(unit='consumer_package',repetitions=repetitions,seed=SEED),outputs=output_records(staging)))
        finalize_directory(staging,out);return dict(metrics=results)
    except BaseException:cleanup_directory(staging);raise
