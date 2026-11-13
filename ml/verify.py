"""Validate a completed ML run, including serialized inference and PIT features."""
import argparse,json
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
import duckdb,joblib
from sklearn.metrics import average_precision_score,roc_auc_score,f1_score
from analysis.common import verify_manifest,digest,write_json
from ml.pipeline import _sources,_feature_matrix,NUMERIC,_split
from ml.features import classify_requirements
from ml.evaluation import choose_threshold

def verify(dataset,evaluation,scope):
    dm=verify_manifest(dataset,'dataset-manifest.json');rm=verify_manifest(evaluation,'run-manifest.json')
    assert dm['sources']==_sources()==rm['sources']
    assert rm['dataset_manifest_sha256']==digest(dataset/'dataset-manifest.json')
    rows=pq.read_table(dataset/'ml_dataset.parquet').to_pylist()
    groups={s:[r for r in rows if r['split']==s] for s in ('train','validation','test')}
    assert len(rows)==len({(r['source_package'],r['dependency_name']) for r in rows})
    assert all(r['split']==_split(r['observation_time']) for r in rows)
    metrics=json.loads((evaluation/'metrics.json').read_text())
    for name in ('logistic','tree'):
        model=joblib.load(evaluation/f'{name}.joblib')
        scaler=model.named_steps['pre'].named_transformers_['num'].transformer_list[0][1].named_steps['scale']
        assert scaler.n_samples_seen_==len(groups['train'])
        imputer=model.named_steps['pre'].named_transformers_['num'].transformer_list[0][1].named_steps['impute']
        train=_feature_matrix(groups['train'])[:,:len(NUMERIC)].astype(float)
        medians=np.nanmedian(train,axis=0);medians=np.nan_to_num(medians,nan=0)
        assert np.allclose(imputer.statistics_,medians)
        val=model.predict_proba(_feature_matrix(groups['validation']))[:,1]
        assert choose_threshold([r['label'] for r in groups['validation']],val)==rm['thresholds'][name]
        y=np.array([r['label'] for r in groups['test']]);p=model.predict_proba(_feature_matrix(groups['test']))[:,1]
        m=next(r for r in metrics if r['model']==name)
        assert abs(average_precision_score(y,p)-m['average_precision'])<1e-12
        assert abs(roc_auc_score(y,p)-m['roc_auc'])<1e-12
        assert abs(f1_score(y,p>=rm['thresholds'][name])-m['f1'])<1e-12
        assert len(model.named_steps['pre'].named_transformers_['num'].transformer_list[1][1].features_)==len(NUMERIC)
        predictions=pq.read_table(evaluation/name/'test-predictions.parquet').to_pylist()
        assert np.array_equal(p,np.array([r['score'] for r in predictions]))
    db=duckdb.connect();db.execute("SET memory_limit='1GB'");db.execute('SET threads=2');db.execute("SET TimeZone='UTC'")
    db.execute(f"CREATE VIEW m AS SELECT * FROM read_parquet('{dataset}/ml_dataset.parquet')")
    db.execute(f"CREATE VIEW h AS SELECT * FROM read_parquet('{dataset}/prior-feature-audit.parquet')")
    db.execute(f"CREATE VIEW s AS SELECT * FROM read_json_auto('{scope}/endpoint-audit.json') WHERE horizon=180")
    joined,mismatched=db.execute('SELECT count(*),count(*) FILTER(WHERE m.label IS DISTINCT FROM s.label) FROM m JOIN s USING(source_package,dependency_name,observation_time)').fetchone()
    assert joined==len(rows) and mismatched==0
    assert db.execute("SELECT count(*) FROM m WHERE date_diff('second',try_cast(observation_time AS TIMESTAMPTZ),try_cast(endpoint AS TIMESTAMPTZ))<>180*86400 OR (split='train' AND try_cast(endpoint AS TIMESTAMPTZ)>='2020-01-01'::TIMESTAMPTZ) OR (split='validation' AND try_cast(endpoint AS TIMESTAMPTZ)>='2023-01-01'::TIMESTAMPTZ)").fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM h WHERE try_cast(prior_published_at AS TIMESTAMPTZ)>try_cast(cutoff AS TIMESTAMPTZ) OR (version_lag IS NULL)<>(reason IS NOT NULL)').fetchone()[0]==0
    assert db.execute("SELECT count(*) FROM m JOIN h USING(source_package,dependency_name) WHERE date_diff('second',try_cast(cutoff AS TIMESTAMPTZ),try_cast(observation_time AS TIMESTAMPTZ))<>days*86400 OR (days=30 AND (m.prior_version_lag_30d IS DISTINCT FROM h.version_lag OR m.prior_major_30d IS DISTINCT FROM h.major)) OR (days=90 AND m.prior_version_lag_90d IS DISTINCT FROM h.version_lag)").fetchone()[0]==0
    # Canonical A7 labels from the original anchor requirement, not endpoint syntax.
    cur=db.execute('SELECT m.constraint_type,s.current_requirement FROM m JOIN s USING(source_package,dependency_name,observation_time)')
    pairs=cur.fetchall();categories=classify_requirements([r[1] for r in pairs])
    assert all(category==categories[req] for category,req in pairs)
    ci=json.loads((evaluation/'bootstrap-intervals.json').read_text())
    assert ci['repetitions']==2000 and ci['unit']=='consumer_package'
    assert all(v['valid_repetitions']==2000 for ms in ci['models'].values() for v in ms.values())
    assert next(r for r in metrics if r['model']=='majority')['confusion_matrix']==next(r for r in metrics if r['model']=='persistence')['confusion_matrix']
    db.close()
    return dict(status='passed',rows=len(rows),prior_rows=2*len(rows),models_reloaded=['logistic','tree'],
        checks=['source/output hashes','train-only imputation/scaling','frozen validation threshold','serialized predictions/sklearn metrics',
        'unique incidents and unchanged scope labels','UTC horizon and temporal purge','historical feature cutoffs and missingness',
        'canonical A7 categories','paired 2000-cluster-bootstrap metadata','correct majority'],
        inputs=dict(dataset_manifest_sha256=digest(dataset/'dataset-manifest.json'),run_manifest_sha256=digest(evaluation/'run-manifest.json')),
        verifier_sha256=digest(Path(__file__)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dataset-dir',type=Path,required=True);p.add_argument('--evaluation-dir',type=Path,required=True);p.add_argument('--scope-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();result=verify(a.dataset_dir,a.evaluation_dir,a.scope_dir);write_json(a.output,result);print(json.dumps(result))
