# Maintained BYOK frontend extension

`frontend/dist/byok.js` is a standalone readable adapter and UI. `byok.css` is scoped to `#pubchat-byok`. `index.html` loads both before the compiled React module. No backend/worker files are involved.

## Export/validation contract

Node: `const {exportConfig, nonSecret, defaults} = require('../../frontend/dist/byok.js')` (adjust relative path).

`exportConfig(nonSecretSettings, {main: mainKey, embedding: embeddingKey}, originalLlmConfig)` validates and returns:

```text
{...originalLlmConfig,
 model: 'byok', api: [mainKey],
 byok: {protocol, base_url, model_pro, model_flash,
   embedding: {enabled: true, protocol, base_url, model, api: [embeddingKey]}
 }}
```

If the user explicitly disables embedding, `embedding` is `{enabled:false}`. Enabled embedding requires protocol/base URL/model/key; missing settings never silently disable it. Empty flash uses pro. Model IDs are not remapped or trimmed; surrounding whitespace is rejected. URL validation allows HTTP(S) without userinfo, query, or fragment and preserves the entered Base URL. HTTP should only be used on trusted local/private networks. For model servers on the Windows host, use host.docker.internal rather than localhost in Base URL. Full provider safety/compatibility validation remains the backend's responsibility.

Browser `window.PubChatBYOK.isEnabled()` and `.validate()` are used by the tiny compiled React patch. `.validate()` reports errors visibly and returns a boolean. Export is a pure function, not a download of secret-bearing JSON. No complete payload or keys are logged. BYOK keys are kept only in password fields and memory. Session storage serializes an allowlisted non-secret object, including enabled state. Reload clears keys and blocks enabled BYOK until keys are re-entered. Original preset key handling/storage is unchanged; BYOK keys are never copied into it.

## Compiled bundle compatibility

There are no verified original React sources. The adapter mounts immediately before `.ai-api-config-section` and follows SPA remounts with a MutationObserver. The existing Google/OpenRouter Gemini options are untouched.

Exactly three minimal bundle edits in the search handler:

1. Validate active BYOK and bypass custom-model validation only while active.
2. Use temporary `I="byok"` only while active, rather than depending on the original preset.
3. Bypass original AI-key-required validation only while active. PubMed validation stays intact.

The interceptor matches only POST `http://localhost:8000/api/search/task`, with no query or fragment. It preserves original payload fields, filters, PubMed config, headers, credentials and Request signal. Other URLs/methods pass through unchanged. Invalid enabled BYOK yields a local **failure** response (HTTP 400) matching React's error contract, with no network request. Transport failures remain transport failures.

Connectivity button POSTs `{llm_config: exportConfig(...)}` to `http://localhost:8000/api/search/byok/test`, only after an explicit warning/confirmation. The expected response is `{success:boolean,message:{zh:string,en:string}}`. Timeout is 60 seconds. Backend authorization/routing, provider support and CORS must be supplied by the parent implementation. The test button does not prove provider support locally.

A rebuild/replacement of the dist bundle or a selector/API-origin change requires reapplying/reviewing the three patches and rerunning these tests. Original fixed asset filename is intentionally preserved; clients should force-reload cached assets.

## Backups and tests

Original `index.html` and bundle remain available in upstream commit `c72172f17c4f3b1fb9064d054685b04e75dcdb6c`. Deployment-specific backup directories are intentionally not published.

```bash
node --check frontend/dist/byok.js
node --check frontend/dist/assets/index-D__M8vFc.js
npm ci --prefix local_extensions/test_tools
npx --prefix local_extensions/test_tools playwright install chromium
node local_extensions/tests/byok-ui.test.cjs
```

Playwright 1.63.0 is locked in `local_extensions/test_tools`; production frontend has no new dependency. `PLAYWRIGHT_MODULE` may override the dependency path, and `CHROME_PATH` may override the browser executable. Windows uses installed Google Chrome when available; otherwise the test uses Playwright Chromium. The test uses a fresh headless browser context, a short-lived loopback static server, fake local-test sentinel keys, and explicitly mocked backend responses labelled `LOCAL MOCK ONLY`. External resources are blocked. No real user browser profile or model request is used. The script closes its browser and server when done.
