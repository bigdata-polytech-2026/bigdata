"""Frozen-model inference; does not fit, tune thresholds, or redefine ML outcomes."""
import json,csv
from pathlib import Path
import numpy as np
from analysis.common import verify_manifest,write_json,write_csv,digest
from ml.pipeline import _feature_matrix
from ml.evaluation import weighted_metrics

def run(dataset,models,out,demo=False):
    import joblib,pyarrow.parquet as pq,sklearn
    if sklearn.__version__!='1.7.2':raise ValueError('Frozen models require scikit-learn==1.7.2; run make setup')
    if demo:
        verify_manifest(models,'demo-ml-manifest.json')
        rm=json.loads((models/'run-manifest.json').read_text())
        rows=pq.read_table(dataset).to_pylist()
        counts=list(csv.DictReader((models/'source-class-distribution.csv').open()))
        prevalence=sum(int(r['n']) for r in counts if r['split']=='train' and r['label']=='1')/sum(int(r['n']) for r in counts if r['split']=='train')
    else:
        verify_manifest(dataset.parent,'dataset-manifest.json');rm=verify_manifest(models,'run-manifest.json')
        rows=[r for r in pq.read_table(dataset).to_pylist() if r['split']=='test']
        counts=list(csv.DictReader((dataset.parent/'class-distribution.csv').open()))
        prevalence=sum(int(r['n']) for r in counts if r['split']=='train' and r['label']=='1')/sum(int(r['n']) for r in counts if r['split']=='train')
    out.mkdir()
    y=np.array([r['label'] for r in rows]);x=_feature_matrix(rows)
    scores={'majority':np.full(len(y),prevalence),'persistence':np.ones(len(y)),
            'severity_rule':np.array([r['current_version_lag'] for r in rows])}
    for name in ('logistic','tree'):
        scores[name]=joblib.load(models/f'{name}.joblib').predict_proba(x)[:,1]
    metrics=[dict(model=k,support=len(y),threshold=rm['thresholds'][k],**weighted_metrics(y,v,rm['thresholds'][k])) for k,v in scores.items()]
    if not demo:
        expected=json.loads((models/'metrics.json').read_text())
        for m,e in zip(metrics,expected):
            assert m['model']==e['model']
            for key in ('average_precision','roc_auc','precision','recall','f1','accuracy'):
                if abs(m[key]-e[key])>1e-8:raise ValueError(f'Frozen inference mismatch: {m["model"]}/{key}')
            if m['confusion_matrix']!=e['confusion_matrix']:raise ValueError('Frozen binary predictions differ')
    write_json(out/'metrics.json',metrics)
    write_csv(out/'metrics.csv',('model','support','average_precision','roc_auc','precision','recall','f1'),
        [(r['model'],r['support'],*[r[k] for k in ('average_precision','roc_auc','precision','recall','f1')]) for r in metrics])
    from analysis.common import configure_plotting
    from sklearn.metrics import PrecisionRecallDisplay
    plt=configure_plotting();fig,ax=plt.subplots(figsize=(6,4))
    PrecisionRecallDisplay.from_predictions(y,scores['logistic'],ax=ax,name='Frozen logistic')
    ax.set_title('Demo holdout projection' if demo else 'Full temporal holdout');fig.tight_layout();fig.savefig(out/'pr-curve.png');plt.close(fig)
    write_json(out/'inference-manifest.json',{'mode':'demo projection' if demo else 'full frozen evaluation','fit_called':False,
        'rows':len(y),'source_run_manifest_sha256':digest(models/'run-manifest.json'),'input_dataset_sha256':digest(dataset),
        'full_metrics_verified':not demo,'comparison_tolerance':1e-8,'sklearn':sklearn.__version__})
    return metrics
