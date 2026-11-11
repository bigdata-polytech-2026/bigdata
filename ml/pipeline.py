from __future__ import annotations

import json, shutil
from pathlib import Path

from analysis.common import atomic_directory, cleanup_directory, configure_plotting, finalize_directory, output_records, sql_string, write_csv, write_json
from ml.scope.audit import run as scope_audit

SEED=20261109
NUMERIC=("current_time_lag_days","current_version_lag","consumer_age_days","dependency_age_days","dependency_releases_90d","dependency_releases_365d","production_dependency_count","consumer_releases_365d")
CATEGORICAL=("constraint_type",)

def _split(t):
    if t < "2019-07-05T00:00:00Z": return "train"
    if t < "2020-01-01T00:00:00Z": return "purged"
    if t < "2022-07-05T00:00:00Z": return "validation"
    if t < "2023-01-01T00:00:00Z": return "purged"
    return "test"

def _constraint(req):
    req=(req or "").strip()
    if req.startswith("^"): return "caret"
    if req.startswith("~"): return "tilde"
    if req.startswith(("*",">","<","=")) or " " in req or "||" in req: return "range"
    if req.count(".")==2 and all(x.isdigit() for x in req.split(".")): return "exact"
    return "other"

def build(dataset_dir: Path, scope_dir: Path) -> dict:
    """Build ML1 180-day incident dataset; no endpoint data are read from the future as features."""
    import duckdb, pyarrow as pa, pyarrow.parquet as pq
    from datetime import datetime
    scope_audit(scope_dir,60000)
    endpoints=json.loads((scope_dir/'endpoint-audit.json').read_text())
    anchors=[r for r in endpoints if r['horizon']==180 and r['label'] is not None and _split(r['observation_time'])!='purged']
    for r in anchors: r['split']=_split(r['observation_time']); r['constraint_type']=_constraint(r['current_requirement'])
    staging=atomic_directory(dataset_dir); db=duckdb.connect(); db.execute("SET memory_limit='2GB'")
    try:
        db.register('anchors',pa.Table.from_pylist(anchors))
        n=Path('data/datasets/full-v1-normalized/normalized')
        db.execute(f"CREATE VIEW packages AS SELECT package_name,created_at FROM read_parquet({sql_string(n/'packages/*.parquet')})")
        db.execute(f"CREATE VIEW quality AS SELECT package_name,version,published_at FROM read_parquet({sql_string(n/'version_quality.parquet')}) WHERE version_valid AND NOT is_prerelease")
        db.execute(f"CREATE VIEW inputs AS SELECT source_package,source_version,dependency_name,dependency_type,shadowed_by_optional FROM read_parquet({sql_string(n/'technical_lag_inputs.parquet')})")
        db.execute("""CREATE TABLE ml AS WITH depcount AS (SELECT source_package,source_version,count(*) production_dependency_count FROM inputs WHERE dependency_type='dependencies' AND NOT shadowed_by_optional GROUP BY 1,2)
        SELECT a.source_package,a.dependency_name,a.source_version,a.observation_time,a.endpoint,a.label,a.reason,a.split,a.constraint_type,
        a.current_time_lag_days,a.current_version_lag,
        greatest(0,date_diff('day',try_cast(cp.created_at AS TIMESTAMPTZ),try_cast(a.observation_time AS TIMESTAMPTZ))) consumer_age_days,
        greatest(0,date_diff('day',try_cast(dp.created_at AS TIMESTAMPTZ),try_cast(a.observation_time AS TIMESTAMPTZ))) dependency_age_days,
        (SELECT count(*) FROM quality q WHERE q.package_name=a.dependency_name AND try_cast(q.published_at AS TIMESTAMPTZ)>try_cast(a.observation_time AS TIMESTAMPTZ)-INTERVAL '90 days' AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) dependency_releases_90d,
        (SELECT count(*) FROM quality q WHERE q.package_name=a.dependency_name AND try_cast(q.published_at AS TIMESTAMPTZ)>try_cast(a.observation_time AS TIMESTAMPTZ)-INTERVAL '365 days' AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) dependency_releases_365d,
        coalesce(d.production_dependency_count,0) production_dependency_count,
        (SELECT count(*) FROM quality q WHERE q.package_name=a.source_package AND try_cast(q.published_at AS TIMESTAMPTZ)>try_cast(a.observation_time AS TIMESTAMPTZ)-INTERVAL '365 days' AND try_cast(q.published_at AS TIMESTAMPTZ)<=try_cast(a.observation_time AS TIMESTAMPTZ)) consumer_releases_365d
        FROM anchors a LEFT JOIN packages cp ON cp.package_name=a.source_package LEFT JOIN packages dp ON dp.package_name=a.dependency_name LEFT JOIN depcount d USING(source_package,source_version)""")
        pq.write_table(db.execute("SELECT * FROM ml").fetch_arrow_table(),staging/'ml_dataset.parquet',compression='zstd')
        counts=db.execute("SELECT split,label,count(*) n FROM ml GROUP BY 1,2 ORDER BY 1,2").fetchall(); write_csv(staging/'class-distribution.csv',('split','label','n'),counts)
        report={'rows':len(anchors),'splits':{s:sum(r[2] for r in counts if r[0]==s) for s in ('train','validation','test')},'features':list(NUMERIC+CATEGORICAL),'label':'major_lag_at_t_plus_180d'}
        write_json(staging/'dataset-manifest.json',report); db.close(); finalize_directory(staging,dataset_dir); return report
    except BaseException: db.close(); cleanup_directory(staging); raise

def train(dataset_dir: Path, out: Path) -> dict:
    import numpy as np, pyarrow.parquet as pq
    from sklearn.compose import ColumnTransformer
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score,roc_auc_score,precision_recall_fscore_support,confusion_matrix,PrecisionRecallDisplay,ConfusionMatrixDisplay,precision_recall_curve
    from sklearn.calibration import calibration_curve
    import joblib
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder,StandardScaler
    rows=pq.read_table(dataset_dir/'ml_dataset.parquet').to_pylist()
    def xy(split):
        x=[r for r in rows if r['split']==split]; y=np.array([r['label'] for r in x]); return x,y
    train_rows,ytr=xy('train'); val_rows,yv=xy('validation'); test_rows,yt=xy('test')
    def matrix(rows): return [[r.get(f) for f in NUMERIC+CATEGORICAL] for r in rows]
    pre=ColumnTransformer([('num',Pipeline([('impute',SimpleImputer(strategy='median',add_indicator=True)),('scale',StandardScaler())]),list(range(len(NUMERIC)))),('cat',Pipeline([('impute',SimpleImputer(strategy='most_frequent')),('onehot',OneHotEncoder(handle_unknown='ignore',sparse_output=False))]),[len(NUMERIC)])],sparse_threshold=0)
    models={'logistic':Pipeline([('pre',pre),('model',LogisticRegression(C=1,max_iter=2000,random_state=SEED))]),'tree':Pipeline([('pre',pre),('model',DecisionTreeClassifier(max_depth=6,min_samples_leaf=50,random_state=SEED))])}
    staging=atomic_directory(out)
    try:
        def metrics(name,y,p,threshold):
            pred=p>=threshold; pr,re,f,_=precision_recall_fscore_support(y,pred,average='binary',zero_division=0); return {'model':name,'support':int(len(y)),'positive_rate':float(y.mean()),'average_precision':float(average_precision_score(y,p)),'roc_auc':float(roc_auc_score(y,p)),'accuracy':float((pred==y).mean()),'precision':float(pr),'recall':float(re),'f1':float(f),'threshold':float(threshold),'confusion_matrix':confusion_matrix(y,pred).tolist()}
        results=[]; predictions=[]; fitted={}
        prevalence=float(ytr.mean()); results.append(metrics('majority',yt,np.full(len(yt),prevalence),1.1)); results.append(metrics('persistence',yt,np.ones(len(yt)),.5))
        for name,m in models.items():
            m.fit(matrix(train_rows),ytr); pv=m.predict_proba(matrix(val_rows))[:,1]; pr,re,thresholds=precision_recall_curve(yv,pv); f1=2*pr[:-1]*re[:-1]/np.maximum(pr[:-1]+re[:-1],1e-12); threshold=float(thresholds[int(np.argmax(f1))]); pt=m.predict_proba(matrix(test_rows))[:,1]; results.append(metrics(name,yt,pt,threshold)); fitted[name]=(m,threshold,pt); joblib.dump(m,staging/f'{name}.joblib'); predictions += [{**{k:r[k] for k in ('source_package','dependency_name','observation_time','constraint_type','current_version_lag')},'label':int(y),'probability':float(p),'prediction':int(p>=threshold)} for r,y,p in zip(test_rows,yt,pt)]
        write_json(staging/'metrics.json',results)
        bm,bt,bp=fitted['tree']; cohort=[]
        for c in sorted({r['constraint_type'] for r in test_rows}):
            mask=np.array([r['constraint_type']==c for r in test_rows]); cohort.append((c,int(mask.sum()),float(yt[mask].mean()),float(metrics('x',yt[mask],bp[mask],bt)['f1'])))
        write_csv(staging/'cohorts.csv',('cohort','n','positive_rate','f1'),cohort)
        write_csv(staging/'error-analysis.csv',tuple(predictions[0]),[tuple(r.values()) for r in predictions if r['prediction']!=r['label']])
        plt=configure_plotting(); (staging/'figures').mkdir(); fig,ax=plt.subplots(figsize=(6,4)); PrecisionRecallDisplay.from_predictions(yt,bp,ax=ax); fig.tight_layout(); fig.savefig(staging/'figures'/'pr-curve.png'); plt.close(fig)
        fig,ax=plt.subplots(figsize=(5,4)); ConfusionMatrixDisplay(confusion_matrix=np.asarray(results[-1]['confusion_matrix'])).plot(ax=ax,colorbar=False); fig.tight_layout(); fig.savefig(staging/'figures'/'confusion-matrix.png'); plt.close(fig)
        frac,mean=calibration_curve(yt,bp,n_bins=10); fig,ax=plt.subplots(figsize=(5,4)); ax.plot(mean,frac,marker='o'); ax.plot([0,1],[0,1],'--'); ax.set(xlabel='Mean predicted probability',ylabel='Observed fraction'); fig.tight_layout(); fig.savefig(staging/'figures'/'calibration.png'); plt.close(fig)
        lm,_,_=fitted['logistic']; names=lm.named_steps['pre'].get_feature_names_out(); coefficients=lm.named_steps['model'].coef_[0]
        write_csv(staging/'feature-importance.csv',('transformed_feature','coefficient','absolute_coefficient'),sorted(((n,float(c),abs(float(c))) for n,c in zip(names,coefficients)),key=lambda x:-x[2]))
        write_json(staging/'run-manifest.json',{'seed':SEED,'features':list(NUMERIC+CATEGORICAL),'outputs':output_records(staging)}); finalize_directory(staging,out); return {'metrics':results}
    except BaseException: cleanup_directory(staging); raise
