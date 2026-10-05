"""Short-lived local mock service used only by the live BYOK UI smoke test."""
import importlib.util
import sys
import json
sys.path.insert(0, '/app')

spec = importlib.util.spec_from_file_location('fixtures', '/app/local_byok_tests/test_byok.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
fixtures.thread.start()
print('READY ' + json.dumps({'base': fixtures.BASE}), flush=True)
try:
    sys.stdin.readline()  # Parent sends STOP; closing stdin also stops the fixture.
finally:
    fixtures.server.shutdown()
    fixtures.server.server_close()
