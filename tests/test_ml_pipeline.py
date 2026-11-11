import unittest
from ml.pipeline import _split

class MLPipelineTest(unittest.TestCase):
 def test_temporal_split_boundaries(self):
  self.assertEqual(_split('2019-07-04T23:59:59Z'),'train')
  self.assertEqual(_split('2019-07-05T00:00:00Z'),'purged')
  self.assertEqual(_split('2020-01-01T00:00:00Z'),'validation')
  self.assertEqual(_split('2023-01-01T00:00:00Z'),'test')
