"""Distribution contracts: atomic retries, cache integrity, readable preflight errors."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from infra.distribution.runner import stage,require,data
from analysis.common import digest

class DistributionTest(unittest.TestCase):
    def test_repeat_reuses_verified_output_and_input_change_creates_new_run(self):
        previous=Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp);source=Path('input.json');source.write_text('one');calls=[]
                def identity(inputs,params):return digest(inputs[0])[:16],{'input':digest(inputs[0])}
                def work(out):calls.append(1);out.mkdir();(out/'table.csv').write_text('a,b\n1,2\n')
                with patch('infra.distribution.runner.signature',side_effect=identity):
                    first=stage('demo','example',[source],{},work)
                    self.assertEqual(first,stage('demo','example',[source],{},work));self.assertEqual(len(calls),1)
                    source.write_text('two');second=stage('demo','example',[source],{},work)
                    self.assertNotEqual(first,second);self.assertTrue(first.exists());self.assertEqual(len(calls),2)
                    (second/'table.csv').write_text('corrupted')
                    with self.assertRaisesRegex(ValueError,'verification failed'):stage('demo','example',[source],{},work)
            finally:os.chdir(previous)
    def test_failed_run_can_retry_without_manual_removal(self):
        previous=Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp);source=Path('input');source.write_text('x')
                def failed(out):out.mkdir();(out/'partial').write_text('x');raise RuntimeError('simulated interruption')
                def good(out):out.mkdir();(out/'complete').write_text('yes')
                with patch('infra.distribution.runner.signature',return_value=('fixed',{})):
                    with self.assertRaisesRegex(RuntimeError,'interruption'):stage('demo','retry',[source],{},failed)
                    result=stage('demo','retry',[source],{},good)
                self.assertTrue((result/'complete').exists());self.assertFalse((result/'partial').exists())
            finally:os.chdir(previous)
    def test_missing_data_message_names_recovery_command(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError,'make data'):require(Path(temp)/'missing')

    def test_missing_credentials_is_explicit_without_live_network(self):
        from botocore.exceptions import ProfileNotFound
        with patch('botocore.session.Session',side_effect=ProfileNotFound(profile='absent')):
            with self.assertRaisesRegex(ValueError,'DVC/S3 credentials unavailable'):data('demo')
