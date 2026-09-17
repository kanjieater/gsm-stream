"""Prove discovery rejects unsafe integration imports, without importing GSM."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class UpgradeDiscoverySafety(unittest.TestCase):
    def test_discovery_skips_before_application_imports(self):
        tests = Path(__file__).resolve().parent
        # A subprocess avoids sys.modules cache hiding an import. The finder is
        # also a fail-closed safety net if the integration module guard regresses.
        probe = r'''
import importlib.abc
import sys
import unittest

attempted = []
class BlockApplicationImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'bridge', 'controller', 'stream', 'GameSentenceMiner', 'PIL'}:
            attempted.append(fullname)
            raise AssertionError('Unsafe application import during discovery: ' + fullname)

sys.meta_path.insert(0, BlockApplicationImports())
suite = unittest.defaultTestLoader.discover(sys.argv[1], pattern='test_upgrade.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert result.wasSuccessful(), 'Discovery failed'
assert result.testsRun == 1 and len(result.skipped) == 1, 'Expected module-level skip'
assert not attempted, attempted
print('No application/GSM import attempted')
'''
        for value in (None, '', '0', 'true'):
            with self.subTest(opt_in=value):
                env = os.environ.copy()
                env.pop('GSM_TEST_ISOLATED', None)
                env['PYTHONDONTWRITEBYTECODE'] = '1'
                if value is not None:
                    env['GSM_TEST_ISOLATED'] = value
                result = subprocess.run(
                    [sys.executable, '-c', probe, str(tests)], env=env,
                    text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('No application/GSM import attempted', result.stdout)


if __name__ == '__main__':
    unittest.main()
