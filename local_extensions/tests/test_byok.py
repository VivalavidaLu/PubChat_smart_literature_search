"""Offline BYOK regression suite. All responses are explicitly local fixtures.
Run on host for adapters; add --worker inside the worker for integration checks.
No real credentials, PubMed calls or paid model calls are used.
"""
from __future__ import annotations
import contextlib
import copy
import json
import os
import pathlib
import sys
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
if ROOT.name == 'local_byok':
    ROOT = pathlib.Path('/app')
sys.path.insert(0, str(ROOT))
from common_utils.byok_protocols import (BYOKError, ConfigError, embed, generate, restore_config, split_secrets, test_connection as connection_test, validate_config)

# This is a public mock sentinel, not a usable API credential.
SENTINEL = 'local-test-sentinel'
requests_seen = []


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests_seen.append({'path': self.path, 'payload': payload, 'headers': {k.lower(): v for k, v in self.headers.items()}})
        if self.path.startswith('/error/'):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(('upstream echoed ' + SENTINEL).encode())
            return
        if self.path.startswith('/redirect/'):
            self.send_response(307)
            self.send_header('Location', '/v1/chat/completions')
            self.end_headers()
            return
        if self.path.endswith('/chat/completions'):
            if payload.get('model') == 'fallback-model' and 'max_tokens' in payload:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"max_tokens is unsupported; use max_completion_tokens"}')
                return
            response = {'choices': [{'message': {'content': 'MOCK_OK'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}
        elif self.path.endswith(':generateContent'):
            response = {'candidates': [{'content': {'parts': [{'text': 'private reasoning', 'thought': True}, {'text': 'MOCK_OK'}]}, 'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 4, 'candidatesTokenCount': 2}}
        elif self.path.endswith('/messages'):
            response = {'content': [{'type': 'thinking', 'thinking': 'private reasoning'}, {'type': 'text', 'text': 'MOCK_OK'}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 4, 'output_tokens': 2}}
        elif self.path.endswith('/embeddings'):
            response = {'data': [{'index': i, 'embedding': [1.0, 0.5, -0.5]} for i in reversed(range(len(payload['input'])))]}
        elif self.path.endswith(':batchEmbedContents'):
            response = {'embeddings': [{'values': [1.0, 0.5, -0.5]} for _ in payload['requests']]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
BASE = 'http://127.0.0.1:' + str(server.server_port)
thread = threading.Thread(target=server.serve_forever, daemon=True)


def config(protocol='openai', embedding=True):
    return {'model': 'byok', 'api': [SENTINEL], 'pubmed_api': [], 'byok': {
        'protocol': protocol, 'base_url': BASE + ('/v1' if protocol == 'openai' else ''),
        'model_pro': 'exact/Model-ID', 'model_flash': 'exact/Model-ID',
        'embedding': {'enabled': True, 'protocol': 'openai', 'base_url': BASE + '/v1', 'model': 'exact-embedding', 'api': [SENTINEL]} if embedding else {'enabled': False}}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        requests_seen.clear()

    def test_three_text_protocols(self):
        for protocol in ['openai', 'gemini', 'anthropic']:
            with self.subTest(protocol=protocol):
                cfg = config(protocol)['byok']
                out = generate(cfg, SENTINEL, cfg['model_pro'], 'test')
                self.assertEqual(out.content, 'MOCK_OK')
                self.assertEqual((out.input_tokens, out.output_tokens), (4, 2))
                seen = requests_seen[-1]
                self.assertNotIn(SENTINEL, seen['path'])
                if protocol == 'openai':
                    self.assertEqual(seen['path'], '/v1/chat/completions')
                    self.assertEqual(seen['payload']['model'], 'exact/Model-ID')
                    self.assertEqual(seen['headers']['authorization'], 'Bearer ' + SENTINEL)
                elif protocol == 'gemini':
                    self.assertEqual(seen['path'], '/v1beta/models/exact/Model-ID:generateContent')
                    self.assertEqual(seen['headers']['x-goog-api-key'], SENTINEL)
                else:
                    self.assertEqual(seen['path'], '/v1/messages')
                    self.assertEqual(seen['headers']['x-api-key'], SENTINEL)
                    self.assertEqual(seen['headers']['anthropic-version'], '2023-06-01')

    def test_openai_token_parameter_fallback(self):
        cfg = config()['byok']
        self.assertEqual(generate(cfg, SENTINEL, 'fallback-model', 'test').content, 'MOCK_OK')
        self.assertEqual(len(requests_seen), 2)
        self.assertIn('max_completion_tokens', requests_seen[-1]['payload'])

    def test_openai_embedding_order(self):
        cfg = config()['byok']['embedding']
        self.assertEqual(embed(cfg, SENTINEL, ['one', 'two']), [[1.0, 0.5, -0.5], [1.0, 0.5, -0.5]])
        self.assertEqual(requests_seen[-1]['payload']['input'], ['one', 'two'])

    def test_gemini_query_and_document_semantics(self):
        cfg = {'protocol': 'gemini', 'base_url': BASE + '/v1beta', 'model': 'models/mock-embedding'}
        for is_query in [True, False]:
            self.assertEqual(len(embed(cfg, SENTINEL, ['test'], is_query=is_query)), 1)
            self.assertEqual(requests_seen[-1]['path'], '/v1beta/models/mock-embedding:batchEmbedContents')
            self.assertEqual(requests_seen[-1]['payload']['requests'][0]['taskType'], 'RETRIEVAL_QUERY' if is_query else 'RETRIEVAL_DOCUMENT')

    def test_no_embedding_input_no_request(self):
        self.assertEqual(embed(config()['byok']['embedding'], SENTINEL, []), [])
        self.assertEqual(requests_seen, [])

    def test_connection_checks_models_and_embedding(self):
        c = config()
        c['byok']['model_flash'] = 'second-model'
        self.assertTrue(connection_test(c)['success'])
        self.assertEqual(len(requests_seen), 3)

    def test_disabled_embedding_explicit(self):
        c = config(embedding=False)
        self.assertTrue(connection_test(c)['success'])
        self.assertEqual(len(requests_seen), 1)

    def test_credentials_not_in_database_config(self):
        c = config()
        c['pubmed_api'] = ['local-pubmed-sentinel']
        safe, secrets = split_secrets(c)
        self.assertNotIn(SENTINEL, json.dumps(safe))
        self.assertNotIn('api', safe['embedding'])
        self.assertEqual(restore_config(safe, secrets), validate_config(c))

    def test_invalid_protocol_never_falls_back(self):
        c = config()
        c['byok']['protocol'] = 'unsupported'
        with self.assertRaises(ConfigError):
            validate_config(c)
        self.assertEqual(requests_seen, [])

    def test_missing_embedding_config_rejected(self):
        c = config()
        del c['byok']['embedding']
        with self.assertRaises(ConfigError):
            validate_config(c)

    def test_url_credentials_and_query_rejected(self):
        for url in ['https://user:pass@localhost/v1', 'https://localhost/v1?key=sentinel', 'ftp://localhost', 'https://localhost/v1/chat/completions']:
            with self.subTest(url=url):
                c = config()
                c['byok']['base_url'] = url
                with self.assertRaises(ConfigError):
                    validate_config(c)

    def test_exact_model_id_not_normalized(self):
        c = config()
        self.assertEqual(validate_config(c)['byok']['model_pro'], 'exact/Model-ID')
        c['byok']['model_pro'] = ' exact/Model-ID'
        with self.assertRaises(ConfigError):
            validate_config(c)

    def test_upstream_error_does_not_echo_key(self):
        cfg = config()['byok']
        cfg['base_url'] = BASE + '/error'
        with self.assertRaises(BYOKError) as caught:
            generate(cfg, SENTINEL, 'test', 'test')
        self.assertNotIn(SENTINEL, str(caught.exception))
        self.assertIn('401', str(caught.exception))

    def test_redirect_not_followed(self):
        cfg = config()['byok']
        cfg['base_url'] = BASE + '/redirect'
        with self.assertRaises(BYOKError):
            generate(cfg, SENTINEL, 'test', 'test')
        self.assertEqual(len(requests_seen), 1)


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from local_byok import byok_worker
        cls.worker = byok_worker

    def test_patched_worker_three_protocols_and_token_counts(self):
        for protocol in ['openai', 'gemini', 'anthropic']:
            with self.subTest(protocol=protocol):
                c = config(protocol, embedding=False)
                ai = self.worker.AI(c, task_id='offline-test')
                self.assertEqual(ai._generate_content('local fixture', max_output_tokens=123), 'MOCK_OK')
                self.assertEqual(ai.sdk_type, 'byok')
                self.assertEqual(ai.model_pro_name, 'exact/Model-ID')
                self.assertIsNone(ai.key_pool._log_api_error_async(SENTINEL, 'mock'))
                self.assertGreaterEqual(sum(v.get('input_tokens', 0) for v in ai.token_stats.values()), 4)

    def test_original_provider_resolver_unchanged(self):
        out = self.worker.AI._resolve_provider_settings({'model': 'google_gemini', 'api': [SENTINEL]})
        self.assertEqual(out[:2], ('gemini', 'google'))
        out = self.worker.AI._resolve_provider_settings({'model': 'openrouter_gemini', 'api': [SENTINEL]})
        self.assertEqual(out[:2], ('openrouter', 'openai_compatible'))

    def test_embedding_factory_routes_custom_provider(self):
        provider = self.worker.workflow_module.create_embedding_provider(config(), {})
        self.assertEqual(provider.embed_query('test'), [1.0, 0.5, -0.5])
        self.assertEqual(len(provider.embed_documents(['one', 'two'])), 2)

    def test_task_database_and_redis_configuration_reaches_workflow(self):
        w = self.worker
        import psycopg2
        task_id = str(uuid.uuid4())
        safe, secrets = split_secrets(config())
        with psycopg2.connect(**w.upstream.DB_CONFIG) as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO "userSchema".tasks (id, model, api, pubmed_api, byok_config, user_query) VALUES (%s,%s,%s,%s,%s::jsonb,%s)', (task_id, 'byok', [], [], json.dumps(safe), '[BYOK offline integration test]'))
        r = w._redis()
        r.setex(f'task:{task_id}:byok_keys', 120, json.dumps(secrets))
        capture = {}
        original = w._old_workflow_init
        old_run = w._old_run
        try:
            def capture_init(self, *args, **kwargs):
                bound = w._workflow_signature.bind(self, *args, **kwargs)
                capture.update(bound.arguments['llm_config'])
            w._old_workflow_init = capture_init
            instance = object.__new__(w.upstream.SearchWorkflow)
            w._workflow_init(instance, task_id=task_id, user_query='offline', output_language='en', llm_config={'model': 'byok', 'api': [], 'pubmed_api': []}, search_settings={'max_refinement_attempts': 1, 'min_study_threshold': 1}, search_filters={}, journal_filters={})
            self.assertEqual(capture, validate_config(config()))
            with psycopg2.connect(**w.upstream.DB_CONFIG) as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT api, pubmed_api, byok_config FROM "userSchema".tasks WHERE id=%s', (task_id,))
                    row = cur.fetchone()
            self.assertEqual(row[0], [])
            self.assertNotIn(SENTINEL, json.dumps(row[2]))
            w._old_run = lambda *a, **k: 'offline-result'
            self.assertEqual(w._run(task_id), 'offline-result')
            self.assertFalse(r.exists(f'task:{task_id}:byok_keys'))
        finally:
            w._old_workflow_init = original
            w._old_run = old_run
            r.delete(f'task:{task_id}:byok_keys')
            with psycopg2.connect(**w.upstream.DB_CONFIG) as conn:
                with conn.cursor() as cur:
                    # Remove only this explicitly named synthetic database fixture.
                    cur.execute('DELETE FROM "userSchema".tasks WHERE id=%s', (task_id,))


if __name__ == '__main__':
    thread.start()
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(AdapterTests)
        if '--worker' in sys.argv:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(WorkerTests))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    finally:
        server.shutdown()
        server.server_close()
