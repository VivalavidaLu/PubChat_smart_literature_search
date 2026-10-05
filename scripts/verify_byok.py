"""Reproducible local-only verification. Does not call paid model APIs.
This script exercises deliberately labelled mock responses and synthetic tasks.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'local_extensions' / 'verification.json'

def run(name, command, stdin=None):
    proc = subprocess.run(command, cwd=ROOT, input=stdin, text=True, encoding='utf-8', errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    matches = re.findall(r'Ran (\d+) tests', proc.stdout)
    result = {'name': name, 'command': command, 'exit_code': proc.returncode,
              'test_count': int(matches[-1]) if matches else None, 'output': proc.stdout}
    print(name, 'PASS' if proc.returncode == 0 else 'FAIL', 'tests=', result['test_count'])
    if proc.returncode:
        print(proc.stdout)
    return result


def main():
    records = []
    records.append(run('Host adapter tests', [sys.executable, 'local_extensions/tests/test_byok.py']))
    source = (ROOT / 'local_extensions/tests/test_byok.py').read_text(encoding='utf-8')
    records.append(run('Worker adapter and DB/Redis integration', ['docker', 'compose', 'exec', '-T', 'celery-worker', 'python', '-c',
        'import sys; exec(compile(sys.stdin.read(), "/app/local_byok/tests/test_byok.py", "exec"), {"__name__":"__main__", "__file__":"/app/local_byok/tests/test_byok.py"})', '--worker'], source))
    records.append(run('Create isolated API test directory', ['docker', 'compose', 'exec', '-T', 'search-server', 'python', '-c', "import os; os.makedirs('/app/local_byok_tests', exist_ok=True)"]))
    for filename in ['test_byok.py', 'test_api.py', 'live_fixture.py']:
        records.append(run('Copy API test fixture ' + filename, ['docker', 'cp', 'local_extensions/tests/' + filename, 'pubchat-local-search-server:/app/local_byok_tests/' + filename]))
    records.append(run('API tests with local fixture services', ['docker', 'compose', 'exec', '-T', 'search-server', 'python', '/app/local_byok_tests/test_api.py']))
    records.append(run('Compose configuration', ['docker', 'compose', 'config', '--quiet']))
    if '--ui' in sys.argv:
        module = ROOT / 'local_extensions/test_tools/node_modules/playwright'
        if not module.exists():
            raise SystemExit('Playwright test dependency missing; run npm ci --prefix local_extensions/test_tools')
        os.environ['PLAYWRIGHT_MODULE'] = str(module)
        for filename in ['frontend/dist/byok.js', 'frontend/dist/assets/index-D__M8vFc.js']:
            records.append(run('JavaScript syntax ' + filename, ['node', '--check', filename]))
        records.append(run('Isolated React UI regression', ['node', 'local_extensions/tests/byok-ui.test.cjs']))
        records.append(run('Live UI/API with local fixture providers', ['node', 'local_extensions/tests/byok-live.test.cjs']))
    with urllib.request.urlopen('http://127.0.0.1:8000/api/search/health', timeout=10) as response:
        health = {'http_status': response.status, 'body': json.load(response)}
    # Host tests are repeated in worker; report unique backend test count separately.
    unique_backend = sum(r['test_count'] or 0 for r in records if r['name'] in ['Worker adapter and DB/Redis integration', 'API tests with local fixture services'])
    report = {'python_executable': sys.executable, 'python_version': sys.version, 'fixture_only': True,
              'unique_backend_tests': unique_backend, 'api_health': health, 'checks': records,
              'all_passed': all(r['exit_code'] == 0 for r in records)}
    RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('unique_backend_tests=', unique_backend, 'all_passed=', report['all_passed'])
    print('report=', RESULTS)
    return 0 if report['all_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
