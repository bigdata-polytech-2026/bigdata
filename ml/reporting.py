"""Model-specific diagnostics; never pool errors from different estimators."""
import numpy as np
from analysis.common import configure_plotting,write_csv,write_json
from ml.evaluation import weighted_metrics

def diagnostics(out,groups,ys,scores,thresholds,importance):
    import pyarrow as pa,pyarrow.parquet as pq
    from sklearn.metrics import PrecisionRecallDisplay,ConfusionMatrixDisplay
    from sklearn.calibration import calibration_curve
    plt=configure_plotting();rows=groups['test'];y=ys['test']
    seen={r['source_package'] for r in groups['train']}
    for name,p in scores.items():
        target=out/name;target.mkdir()
        pred=p>=thresholds[name]
        records=[dict(model=name,source_package=r['source_package'],dependency_name=r['dependency_name'],
            observation_time=r['observation_time'],outcome_reason=r['reason'],constraint_type=r['constraint_type'],
            consumer_cohort='seen' if r['source_package'] in seen else 'unseen',label=int(label),score=float(score),prediction=int(value))
            for r,label,score,value in zip(rows,y,p,pred)]
        pq.write_table(pa.Table.from_pylist(records),target/'test-predictions.parquet',compression='zstd')
        write_csv(target/'errors.csv',tuple(records[0]),[tuple(r.values()) for r in records if r['label']!=r['prediction']])
        cohorts=[]
        masks=[('constraint',c,np.array([r['constraint_type']==c for r in rows])) for c in sorted({r['constraint_type'] for r in rows})]
        masks += [('consumer',c,np.array([(r['source_package'] in seen)==(c=='seen') for r in rows])) for c in ['seen','unseen']]
        masks += [('negative_outcome',c,np.array([r['reason']==c for r in rows])) for c in sorted({r['reason'] for r in rows if r['label']==0})]
        for kind,label,mask in masks:
            if not mask.any():continue
            result=weighted_metrics(y[mask],p[mask],thresholds[name])
            cohorts.append(dict(kind=kind,cohort=label,n=int(mask.sum()),positive_rate=float(y[mask].mean()),**{k:result[k] for k in ('average_precision','roc_auc','precision','recall','f1')}))
        write_csv(target/'cohorts.csv',tuple(cohorts[0]),[tuple(r.values()) for r in cohorts])
        if name in importance:write_csv(target/'feature-importance.csv',('transformed_feature','value','absolute_value'),importance[name])
        fig,ax=plt.subplots(figsize=(6,4));PrecisionRecallDisplay.from_predictions(y,p,ax=ax,name=name)
        ax.axhline(y.mean(),ls='--',color='gray',label='test prevalence');ax.legend();fig.tight_layout();fig.savefig(target/'pr-curve.png');plt.close(fig)
        fig,ax=plt.subplots(figsize=(5,4));ConfusionMatrixDisplay(np.asarray(weighted_metrics(y,p,thresholds[name])['confusion_matrix'])).plot(ax=ax,colorbar=False)
        ax.set_title(name);fig.tight_layout();fig.savefig(target/'confusion-matrix.png');plt.close(fig)
        if name!='severity_rule':
            frac,mean=calibration_curve(y,p,n_bins=10,strategy='uniform')
            write_csv(target/'calibration.csv',('mean_probability','observed_positive_fraction'),zip(mean,frac))
            fig,ax=plt.subplots(figsize=(5,4));ax.plot(mean,frac,marker='o');ax.plot([0,1],[0,1],'--');ax.set(title=name,xlabel='Mean predicted probability',ylabel='Observed fraction');fig.tight_layout();fig.savefig(target/'calibration.png');plt.close(fig)
            write_json(target/'brier.json',dict(brier_score=float(np.mean((p-y)**2))))
        else:write_json(target/'calibration.json',dict(status='not_applicable',reason='version-lag ranking score is not a probability'))
