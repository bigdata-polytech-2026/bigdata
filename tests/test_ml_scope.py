import json,subprocess,unittest
class MLScopeTest(unittest.TestCase):
 def evaluate(self,changes=None):
  r=dict(source_package='app',dependency_name='lib',future_version='1.0.0',future_published_at='2020-01-01T00:00:00Z',consumer_retrieved_at='2022-01-01T00:00:00Z',endpoint='2020-07-01T00:00:00Z',requirement='^1.0.0',dependency_type='dependencies',shadowed_by_optional=False,censored=False,tied=False)
  r.update(changes or {})
  h=dict(package_name='lib',provenance=dict(retrieved_at='2022-01-01T00:00:00Z'),versions=[dict(version='1.0.0',published_at='2019-01-01T00:00:00Z'),dict(version='2.0.0',published_at='2020-07-01T00:00:00Z'),dict(version='3.0.0',published_at='2021-01-01T00:00:00Z')])
  return json.loads(subprocess.run(['node','ml/scope/evaluate.cjs'],input=json.dumps(dict(rows=[r],histories=[h])),text=True,capture_output=True,check=True).stdout)[0]
 def test_endpoint_inclusive_and_future_release_not_used(self):
  r=self.evaluate();self.assertEqual(r['label'],1);self.assertEqual(r['future_version_lag'],1)
 def test_consumer_updated_range_can_clear_major_lag(self):
  self.assertEqual(self.evaluate(dict(requirement='^2.0.0'))['label'],0)
 def test_censored_and_out_of_scope_are_unknown(self):
  for values in [dict(censored=True),dict(tied=True),dict(dependency_type='optionalDependencies')]:self.assertIsNone(self.evaluate(values)['label'])
 def test_removal_is_separate_negative_outcome(self):
  r=self.evaluate(dict(requirement=None));self.assertEqual(r['label'],0);self.assertEqual(r['reason'],'dependency_removed')
