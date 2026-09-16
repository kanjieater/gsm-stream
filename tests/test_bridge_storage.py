"""Execute the actual generated bridge template with a browser-storage shim."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import unittest


def template():
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath('bridge.py').read_text())
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '_BRIDGE_JS_TEMPLATE' for t in n.targets))


def render(version):
    defaults = {'persistLines': True, 'persistStats': True, 'persistNotes': True,
                'persistActionHistory': False, 'fontSize': 24, 'secondary-websocketUrl': ''}
    return (template().replace('__PROFILES__', '["Fixture"]')
            .replace('__UI_DEFAULTS__', json.dumps(defaults))
            .replace('__DEFAULTS_VER__', json.dumps(version)))


@unittest.skipUnless(shutil.which('node'), 'Node required for JavaScript storage contracts')
class StorageContracts(unittest.TestCase):
    def test_seeding_migration_and_defaults_changes_preserve_progress(self):
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const scripts = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const prefix = 'bannou-texthooker-';
function load(data, script) {
  const store = {getItem: k => data[k] ?? null,
    setItem: (k,v) => { data[k] = String(v); },
    removeItem: k => { throw Error('Bridge must never delete storage: '+k); }};
  vm.runInNewContext(script, {localStorage: store, console, fetch: () => Promise.resolve()});
}
let data = {};
load(data, scripts[0]);
assert.equal(data[prefix+'persistLines'], '1');
assert.equal(data[prefix+'persistStats'], '1');
assert.equal(data[prefix+'persistNotes'], '1');
assert.equal(data[prefix+'persistActionHistory'], '0');
const presets = JSON.parse(data[prefix+'settingPresets']);
assert.equal(presets[0].settings.persistLines$, true);
assert.equal(presets[0].settings.secondaryWebsocketUrl$, '');
data[prefix+'lineData'] = '[{"text":"保存テスト"}]';
data[prefix+'timeValue'] = '123';
data[prefix+'userNotes'] = 'keep notes';
data[prefix+'future-key'] = 'keep unknown data';
data[prefix+'fontSize'] = '32';
data[prefix+'persistLines'] = 'true'; // legacy malformed seed
load(data, scripts[1]); // changed defaults hash must not wipe anything
assert.equal(data[prefix+'persistLines'], '1');
for (const [key,value] of Object.entries({lineData:'[{"text":"保存テスト"}]',
 timeValue:'123',userNotes:'keep notes','future-key':'keep unknown data',fontSize:'32'})) {
 assert.equal(data[prefix+key], value);
}
assert.equal(data[prefix+'settingPresets'], JSON.stringify(presets));
data[prefix+'persistLines'] = '0'; // explicit supported user choice is preserved
load(data, scripts[0]);
assert.equal(data[prefix+'persistLines'], '0');
console.log('storage seeding, legacy repair, reload, changed hash, preservation: PASS');
'''
        result = subprocess.run(['node', '-e', harness], input=json.dumps([render('a'), render('b')]),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
