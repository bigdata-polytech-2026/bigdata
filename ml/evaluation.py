"""Fixed validation thresholds and paired consumer-cluster uncertainty."""
import numpy as np

SEED = 20261109

def majority_probability(train_y, n):
    return np.full(n, np.mean(train_y), dtype=float)

def choose_threshold(y, scores):
    from sklearn.metrics import precision_recall_curve
    pr, re, thresholds = precision_recall_curve(y, scores)
    f1 = 2*pr[:-1]*re[:-1]/np.maximum(pr[:-1]+re[:-1], 1e-15)
    return float(thresholds[max(range(len(thresholds)), key=lambda i: (f1[i], pr[i], thresholds[i]))])

class MetricPlan:
    def __init__(self, y, scores, threshold):
        self.y=np.asarray(y,dtype=float); self.p=np.asarray(scores,dtype=float)
        self.order=np.argsort(-self.p,kind='stable')
        ordered=self.p[self.order]
        self.ends=np.r_[np.flatnonzero(ordered[:-1]!=ordered[1:]),len(ordered)-1]
        self.pred=self.p>=threshold
    def calculate(self, weights=None):
        w=np.ones(len(self.y)) if weights is None else np.asarray(weights,dtype=float)
        pos=w*self.y; neg=w*(1-self.y); total=pos.sum(); negatives=neg.sum()
        tp=np.cumsum(pos[self.order])[self.ends]; fp=np.cumsum(neg[self.order])[self.ends]
        dp=np.diff(np.r_[0,tp]); dn=np.diff(np.r_[0,fp])
        ap=float(np.sum(dp*np.divide(tp,tp+fp,out=np.zeros_like(tp),where=(tp+fp)>0))/total) if total else None
        auc=float(np.sum(dp*(negatives-fp+.5*dn))/(total*negatives)) if total and negatives else None
        true_positive=float(pos[self.pred].sum()); false_positive=float(neg[self.pred].sum())
        precision=true_positive/(true_positive+false_positive) if true_positive+false_positive else 0.
        recall=true_positive/total if total else 0.
        return dict(average_precision=ap,roc_auc=auc,precision=precision,recall=recall,
                    f1=2*precision*recall/(precision+recall) if precision+recall else 0.,
                    accuracy=float((true_positive+negatives-false_positive)/w.sum()),
                    confusion_matrix=[[int(negatives-false_positive),int(false_positive)],
                                      [int(total-true_positive),int(true_positive)]])

def weighted_metrics(y,p,threshold,weights=None):
    return MetricPlan(y,p,threshold).calculate(weights)

def bootstrap_intervals(y,scores,thresholds,clusters,repetitions=2000,seed=SEED):
    """Pairs use exactly the same resampled consumers; models/thresholds stay frozen."""
    unique,inverse=np.unique(clusters,return_inverse=True)
    if len(unique)<2: raise ValueError('need at least two consumer clusters')
    plans={k:MetricPlan(y,v,thresholds[k]) for k,v in scores.items()}
    keys=('average_precision','roc_auc','precision','recall','f1')
    values={k:{m:[] for m in keys} for k in scores}
    rng=np.random.default_rng(seed)
    for _ in range(repetitions):
        multiplicity=np.bincount(rng.integers(len(unique),size=len(unique)),minlength=len(unique))
        weights=multiplicity[inverse]
        for name,plan in plans.items():
            r=plan.calculate(weights)
            for m in keys: values[name][m].append(r[m] if r[m] is not None else np.nan)
    def interval(v):
        v=np.asarray(v,dtype=float);v=v[np.isfinite(v)]
        return dict(ci_low=float(np.quantile(v,.025)) if len(v) else None,
                    ci_high=float(np.quantile(v,.975)) if len(v) else None,valid_repetitions=len(v))
    paired={}
    names=list(scores)
    for i,a in enumerate(names):
        for b in names[i+1:]:
            paired[a+'_minus_'+b]={m:interval(np.asarray(values[a][m])-np.asarray(values[b][m])) for m in keys}
    return dict(unit='consumer_package',repetitions=repetitions,seed=seed,clusters=len(unique),
                models={k:{m:interval(v) for m,v in ms.items()} for k,ms in values.items()},paired_differences=paired)
