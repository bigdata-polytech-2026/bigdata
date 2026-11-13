import unittest
import numpy as np
from ml.evaluation import majority_probability, choose_threshold, weighted_metrics, bootstrap_intervals
from ml.features import classify_requirements
from ml.pipeline import _split

class CorrectionTests(unittest.TestCase):
 def test_majority_follows_train_prevalence(self):
  self.assertTrue(np.all(majority_probability([0,1,1],3)>=.5))
  self.assertTrue(np.all(majority_probability([0,0,1],3)<.5))
 def test_a7_classifier_preserves_full_semver_categories(self):
  c=classify_requirements(['=1.2.3','1.x','^1.2.3 || ^2.0.0','~1.0.0','*'])
  self.assertEqual([c[x] for x in ['=1.2.3','1.x','^1.2.3 || ^2.0.0','~1.0.0','*']],['exact','wildcard','range','tilde','wildcard'])
 def test_threshold_ties_prefer_precision_then_threshold(self):
  self.assertEqual(choose_threshold([1,0,0,1],[.9,.8,.7,.6]),.9)
 def test_metrics_match_sklearn_with_ties_and_weights(self):
  from sklearn.metrics import average_precision_score,roc_auc_score,f1_score
  y=np.array([1,0,1,0,1]);p=np.array([.9,.5,.5,.2,.2]);w=np.array([2,1,3,1,2])
  r=weighted_metrics(y,p,.5,w)
  self.assertAlmostEqual(r['average_precision'],average_precision_score(y,p,sample_weight=w))
  self.assertAlmostEqual(r['roc_auc'],roc_auc_score(y,p,sample_weight=w))
  self.assertAlmostEqual(r['f1'],f1_score(y,p>=.5,sample_weight=w))
 def test_cluster_bootstrap_is_paired_and_reproducible(self):
  kw=dict(y=[0,1,0,1],scores={'a':np.array([.1,.9,.2,.8]),'b':np.array([.1,.9,.2,.8])},thresholds={'a':.5,'b':.5},clusters=['p','p','q','q'],repetitions=20,seed=5)
  a=bootstrap_intervals(**kw);self.assertEqual(a,bootstrap_intervals(**kw))
  self.assertEqual(a['paired_differences']['a_minus_b']['average_precision']['ci_low'],0)
 def test_unknown_category_has_explicit_representation(self):
  from ml.pipeline import _feature_matrix
  self.assertEqual(_feature_matrix([{'constraint_type':'new-parser-category'}])[0,-1],'unknown')
 def test_fractional_boundary_not_misclassified(self):
  self.assertEqual(_split('2019-07-05T00:00:00.001Z'),'purged')

class HistoricalFeatureTests(unittest.TestCase):
 def test_prior_lag_uses_prior_manifest_and_filters_future_dependency_releases(self):
  import tempfile,json,subprocess
  from pathlib import Path
  history=dict(package_name='lib',provenance={'retrieved_at':'2022-01-01T00:00:00Z'},versions=[
    dict(version='1.0.0',published_at='2019-01-01T00:00:00Z'),
    dict(version='2.0.0',published_at='2020-01-01T00:00:00Z'),
    dict(version='3.0.0',published_at='2021-01-01T00:00:00Z')])
  row=dict(source_package='app',dependency_name='lib',days=30,prior_version='1.0.0',
    prior_published_at='2019-01-01T00:00:00Z',consumer_retrieved_at='2022-01-01T00:00:00Z',
    cutoff='2020-02-01T00:00:00Z',requirement='^1.0.0',dependency_type='dependencies',shadowed_by_optional=False,tied=False)
  cases=[row,{**row,'requirement':'^2.0.0'},{**row,'prior_version':None},{**row,'tied':True}]
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'histories.jsonl';path.write_text(json.dumps(history)+'\n')
   result=subprocess.run(['node','ml/history.cjs',str(path)],input=''.join(json.dumps(r)+'\n' for r in cases),capture_output=True,text=True,check=True)
  out=[json.loads(line) for line in result.stdout.splitlines()]
  self.assertEqual(out[0]['version_lag'],1)
  self.assertEqual(out[0]['major'],1)
  self.assertEqual(out[1]['version_lag'],0)
  self.assertIsNone(out[2]['version_lag'])
  self.assertEqual(out[3]['reason'],'consumer_release_time_tie')
