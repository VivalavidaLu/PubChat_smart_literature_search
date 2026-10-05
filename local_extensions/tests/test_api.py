"""Backend tests inside search-server; local mock APIs only, no Celery enqueue."""
import asyncio
import importlib.util
import json
import pathlib
import sys
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, '/app')
# Reuse the clearly labelled local fixture HTTP handler, not an external service.
fixture_path = pathlib.Path('/app/local_byok_tests/test_byok.py')
spec = importlib.util.spec_from_file_location('byok_fixtures', fixture_path)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
fixtures.thread.start()
from backend.search_server import searchServer as api
import asyncpg
import redis.asyncio as redis


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = api.app.test_client()
        self.ids = []
        fixtures.requests_seen.clear()
        await api.ensure_byok_schema()

    async def asyncTearDown(self):
        conn = await asyncpg.connect(**api.DB_CONFIG)
        r = redis.Redis(host='redis', decode_responses=True)
        try:
            for task_id in self.ids:
                await conn.execute('DELETE FROM "userSchema".tasks WHERE id=$1::uuid', uuid.UUID(task_id))
                for suffix in ['byok_keys', 'info', 'celery_id']:
                    await r.delete(f'task:{task_id}:{suffix}')
        finally:
            await conn.close()
            await r.aclose()

    async def test_live_test_route_three_protocols(self):
        for protocol in ['openai', 'gemini', 'anthropic']:
            with self.subTest(protocol=protocol):
                response = await self.client.post('/byok/test', json={'llm_config': fixtures.config(protocol)})
                self.assertEqual(response.status_code, 200)
                payload = await response.get_json()
                self.assertTrue(payload['success'])
                self.assertNotIn(fixtures.SENTINEL, json.dumps(payload))

    async def test_invalid_protocol_is_rejected_before_network(self):
        c = fixtures.config()
        c['byok']['protocol'] = 'invalid'
        response = await self.client.post('/byok/test', json={'llm_config': c})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fixtures.requests_seen, [])

    async def test_wrong_origin_rejected(self):
        response = await self.client.post('/byok/test', json={'llm_config': fixtures.config()}, headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(fixtures.requests_seen, [])

    async def test_task_persistence_and_postcommit_dispatch(self):
        observed = {}
        def fake_send_task(name, args, queue):
            # Called in API's thread only after commit. A new sync connection isn't
            # installed here, so check dispatch args and read row after response.
            observed.update(name=name, args=args, queue=queue)
            return SimpleNamespace(id='offline-celery-' + str(uuid.uuid4()))
        task = {'user_query': '[BYOK offline API regression fixture]', 'outputlanguage': 'en',
                'search_settings': {'max_refinement_attempts': 1, 'min_study_threshold': 1},
                'search_filters': {}, 'journal_filters': {}, 'llm_config': fixtures.config()}
        with patch.object(api.celery_app, 'send_task', side_effect=fake_send_task):
            response = await self.client.post('/task', json=task)
        self.assertEqual(response.status_code, 200)
        payload = await response.get_json()
        self.assertTrue(payload['success'])
        task_id = payload['data']['search_task_id']
        self.ids.append(task_id)
        self.assertEqual(observed['name'], 'search_workflow.run_search')
        self.assertEqual(observed['args'], [task_id])
        self.assertEqual(observed['queue'], 'search_queue')
        conn = await asyncpg.connect(**api.DB_CONFIG)
        try:
            row = await conn.fetchrow('SELECT model, api, pubmed_api, byok_config FROM "userSchema".tasks WHERE id=$1::uuid', uuid.UUID(task_id))
            self.assertEqual(row['model'], 'byok')
            self.assertEqual(row['api'], [])
            self.assertEqual(row['pubmed_api'], [])
            self.assertNotIn(fixtures.SENTINEL, row['byok_config'])
        finally:
            await conn.close()
        r = redis.Redis(host='redis', decode_responses=True)
        try:
            raw = await r.get(f'task:{task_id}:byok_keys')
            self.assertEqual(json.loads(raw)['api'], [fixtures.SENTINEL])
            self.assertGreater(await r.ttl(f'task:{task_id}:byok_keys'), 0)
        finally:
            await r.aclose()

    async def test_original_preset_submission_retains_original_config(self):
        c = {'model': 'google_gemini', 'api': [fixtures.SENTINEL], 'pubmed_api': []}
        with patch.object(api.celery_app, 'send_task', return_value=SimpleNamespace(id='offline-celery')):
            response = await self.client.post('/task', json={'user_query': '[BYOK legacy preset regression fixture]', 'outputlanguage': 'en', 'llm_config': c})
        self.assertEqual(response.status_code, 200)
        task_id = (await response.get_json())['data']['search_task_id']
        self.ids.append(task_id)
        conn = await asyncpg.connect(**api.DB_CONFIG)
        try:
            row = await conn.fetchrow('SELECT model, api, byok_config FROM "userSchema".tasks WHERE id=$1::uuid', uuid.UUID(task_id))
            self.assertEqual(row['model'], 'google_gemini')
            self.assertEqual(row['api'], [fixtures.SENTINEL])
            self.assertIsNone(row['byok_config'])
        finally:
            await conn.close()


if __name__ == '__main__':
    try:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(APITests))
        sys.exit(0 if result.wasSuccessful() else 1)
    finally:
        fixtures.server.shutdown()
        fixtures.server.server_close()
