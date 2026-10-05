/* PubChat BYOK extension. No dependencies, telemetry, or secret persistence. */
(function (root) {
  'use strict';
  const STORAGE_KEY = 'pubchat.byok.v1';
  const API_ORIGIN = 'http://localhost:8000';
  const defaults = Object.freeze({enabled:false, protocol:'openai', base_url:'', model_pro:'', model_flash:'', embedding_enabled:true, embedding_protocol:'openai', embedding_base_url:'', embedding_model:'', reuse_key:true});
  const fields = Object.keys(defaults);
  function nonSecret(input) {
    const result = {...defaults};
    for (const name of fields) if (typeof input?.[name] === typeof defaults[name]) result[name] = input[name];
    return result;
  }
  function required(value, label) {
    if (typeof value !== 'string' || !value.trim()) throw new Error(label + ' is required / 必填');
    if (value !== value.trim()) throw new Error(label + ': remove outer whitespace / 请去除首尾空格，输入值不会被自动修改');
    return value;
  }
  function baseURL(value, label) {
    const text = required(value, label);
    let url;
    try { url = new URL(text); } catch { throw new Error(label + ': invalid URL / URL 无效'); }
    if (!['https:','http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error(label + ': use HTTP(S) without credentials, query or fragment / 使用无凭据、查询或片段的 HTTP(S) 地址');
    if (/\/(chat\/completions|messages|embeddings)\/*$/.test(url.pathname) || url.pathname.includes(':generateContent')) throw new Error(label + ': enter the API base, not the complete endpoint / 请填根地址，而非完整调用端点');
    return text;
  }
  // Pure export contract. Caller owns secrets; this function never stores them.
  function exportConfig(config, secrets, original = {}) {
    const c = nonSecret(config);
    if (!['openai','gemini','anthropic'].includes(c.protocol)) throw new Error('Unsupported API protocol');
    const key = required(secrets?.main, 'Main API key');
    const byok = {protocol:c.protocol, base_url:baseURL(c.base_url,'Main base URL'), model_pro:required(c.model_pro,'Pro model ID'), model_flash:required(c.model_flash || c.model_pro,'Flash model ID')};
    if (c.embedding_enabled) {
      if (!['openai','gemini'].includes(c.embedding_protocol)) throw new Error('Unsupported embedding protocol');
      byok.embedding = {enabled:true, protocol:c.embedding_protocol, base_url:baseURL(c.embedding_base_url,'Embedding base URL'), model:required(c.embedding_model,'Embedding model ID'), api:[c.reuse_key ? key : required(secrets?.embedding,'Embedding API key')]};
    } else byok.embedding = {enabled:false};
    return {...original, model:'byok', api:[key], byok};
  }
  const api = {defaults, nonSecret, exportConfig};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (!root?.document) return;
  let config = {...defaults};
  try { config = nonSecret(JSON.parse(root.sessionStorage.getItem(STORAGE_KEY) || '{}')); } catch { /* Storage unavailable: memory only. */ }
  let panel;
  let secrets = {main:'', embedding:''};
  function report(message, error = false) {
    if (!panel?.isConnected) mount();
    const status = panel?.querySelector('[data-status]');
    if (status) { panel.open = true; status.textContent = message; status.dataset.error = String(error); }
  }
  function save() { try { root.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(nonSecret(config))); } catch { /* Memory-only fallback. */ } }
  function validated(original) { return exportConfig(config, secrets, original); }
  root.PubChatBYOK = Object.freeze({...api, isEnabled:() => config.enabled, validate:() => { try { validated(); return true; } catch (error) { report(error.message,true); return false; } }});
  const nativeFetch = root.fetch.bind(root);
  // Match ONLY the original task creation endpoint. All other fetches pass through.
  root.fetch = async function (input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, root.location.href);
    const method = (init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (!config.enabled || method !== 'POST' || url.origin !== API_ORIGIN || url.pathname !== '/api/search/task' || url.search || url.hash) return nativeFetch(input, init);
    let options;
    try {
      const body = init?.body !== undefined ? init.body : await input.clone().text();
      const payload = JSON.parse(body);
      payload.llm_config = validated(payload.llm_config);
      options = {...init, body:JSON.stringify(payload)};
    } catch (error) {
      report('BYOK blocked / 已阻止请求: ' + (error instanceof SyntaxError ? 'Invalid search payload' : error.message), true);
      // Resolve a real local validation failure in the original application's response contract.
      return new Response(JSON.stringify({success:false,message:{en:'BYOK configuration invalid. See the BYOK panel.',zh:'BYOK 配置无效，请检查 BYOK 面板。'}}),{status:400,headers:{'Content-Type':'application/json'}});
    }
    // Cloning a Request preserves headers, credentials, signal, and method.
    return nativeFetch(input instanceof Request ? new Request(input, options) : input, input instanceof Request ? undefined : options);
  };
  function input(name, label, type = 'text', placeholder = '') {
    return `<label class="byok-field">${label}<input data-field="${name}" type="${type}" autocomplete="off" spellcheck="false" placeholder="${placeholder}"></label>`;
  }
  function select(name, label, options) {
    return `<label class="byok-field">${label}<select data-field="${name}">${options.map(x=>`<option value="${x}">${x}</option>`).join('')}</select></label>`;
  }
  function checkbox(name, label) { return `<label class="byok-check"><input data-field="${name}" type="checkbox">${label}</label>`; }
  function refresh() {
    panel.querySelector('[data-main]').disabled = !config.enabled;
    panel.querySelector('[data-embedding]').disabled = !config.embedding_enabled;
    panel.querySelector('[data-field="embedding"]').disabled = config.reuse_key || !config.embedding_enabled;
    panel.querySelector('[data-disable-notice]').hidden = config.embedding_enabled;
    panel.querySelector('[data-mode]').textContent = config.enabled ? 'ACTIVE · 自定义接口' : 'OFF · 使用原有 Gemini 预设';
    panel.querySelector('[data-test]').disabled = !config.enabled;
  }
  async function testConnection() {
    let llm_config;
    try { llm_config = validated(); } catch (error) { report(error.message,true); return; }
    if (!root.confirm('Real connectivity test: sends a minimal model request to your configured provider and may charge. Continue?\n真实连通性测试将发送最小模型请求，可能产生费用。是否继续？')) return;
    const button = panel.querySelector('[data-test]'); button.disabled = true;
    report('Testing / 测试中…');
    const controller = new AbortController();
    const timer = root.setTimeout(() => controller.abort(), 60000);
    try {
      const response = await nativeFetch(API_ORIGIN + '/api/search/byok/test', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({llm_config}),signal:controller.signal});
      const result = await response.json();
      if (typeof result.success !== 'boolean' || typeof result.message?.en !== 'string' || typeof result.message?.zh !== 'string') throw new Error('Unexpected test response / 测试响应格式错误');
      let message = result.message.en + ' / ' + result.message.zh;
      for (const key of Object.values(secrets)) if (key) message = message.split(key).join('[redacted]');
      report(message, !response.ok || !result.success);
    } catch { report('Connectivity test failed, timed out, or endpoint unavailable. / 连通性测试失败、超时或端点不可用。', true); }
    finally { root.clearTimeout(timer); refresh(); }
  }
  function mount() {
    const original = document.querySelector('.ai-api-config-section');
    if (!original || panel?.isConnected) return;
    // Preserve secret input nodes through SPA remounts; never copy keys into React/storage.
    if (!panel) {
      panel = document.createElement('details'); panel.id = 'pubchat-byok'; panel.open = config.enabled;
      panel.innerHTML = `<summary><span class="byok-title">BYOK <small>YOUR MODELS · YOUR ENDPOINT</small></span><span data-mode></span></summary><div class="byok-body"><p>自定义 API / Bring your own API. 原有 Gemini 预设在关闭 BYOK 后仍可使用。PubMed 密钥仍在下方原有设置中填写。</p>${checkbox('enabled','Enable BYOK / 启用自定义接口')}<fieldset data-main><legend>01 / Language model · 语言模型</legend><div class="byok-grid">${select('protocol','API protocol / 协议',['openai','gemini','anthropic'])}${input('base_url','Base URL / 接口根地址','url','https://api.example.com/v1')}${input('model_pro','Pro model ID / 精确模型 ID')}${input('model_flash','Flash model ID / 可选，留空复用 Pro')}${input('main','API key / 仅当前页面内存','password')}</div><p class="byok-hint">OpenAI / Anthropic: typically https://…/v1. Gemini: https://generativelanguage.googleapis.com. 模型 ID 按原样发送；首尾空格会被拒绝。OpenAI 使用 Chat Completions。访问宿主机本地模型请用 http://host.docker.internal:端口/v1；HTTP 明文传输仅适合可信本机或私有网络。</p>${checkbox('embedding_enabled','Enable embeddings / 启用嵌入（默认必配）')}<p data-disable-notice class="byok-warning" hidden>Embeddings explicitly disabled: semantic ranking / retrieval may be unavailable or degraded. / 已主动关闭嵌入：语义检索与排序可能不可用或降级。</p><fieldset data-embedding><legend>02 / Embedding · 嵌入模型</legend><div class="byok-grid">${select('embedding_protocol','Embedding protocol / 协议',['openai','gemini'])}${input('embedding_base_url','Embedding base URL / 根地址','url','https://api.example.com/v1')}${input('embedding_model','Embedding model ID / 精确 ID')}${input('embedding','Embedding API key / 仅当前页面内存','password')}</div>${checkbox('reuse_key','Reuse main API key / 复用主密钥')}</fieldset></fieldset><div class="byok-actions"><button type="button" data-validate>Validate / 验证配置</button><button type="button" data-test>Test connection / 连通性测试</button><button type="button" data-clear>Clear keys / 清除密钥</button></div><p class="byok-hint">Keys never persist and are cleared on reload. Non-secret settings stay in this tab only. Testing sends a minimal real request and may charge. / 密钥不保存；刷新即清空。测试可能产生费用。</p><p data-status role="status" aria-live="polite"></p></div>`;
      for (const node of panel.querySelectorAll('[data-field]')) {
        const name = node.dataset.field;
        if (name in config) { if (node.type === 'checkbox') node.checked = config[name]; else node.value = config[name]; }
      }
      panel.addEventListener('input', event => {
        const node = event.target, name = node.dataset.field;
        if (!name) return;
        if (name in secrets) secrets[name] = node.value;
        else if (name in config) { config[name] = node.type === 'checkbox' ? node.checked : node.value; save(); }
        panel.querySelector('[data-status]').textContent = ''; refresh();
      });
      panel.querySelector('[data-validate]').onclick = () => {
        try { validated(); report('Valid locally. No request sent. / 本地验证通过，尚未发送请求。'); } catch (error) { report(error.message,true); }
      };
      panel.querySelector('[data-test]').onclick = testConnection;
      panel.querySelector('[data-clear]').onclick = () => {
        secrets = {main:'',embedding:''};
        for (const name of ['main','embedding']) panel.querySelector(`[data-field="${name}"]`).value = '';
        report('Keys cleared / 密钥已清空');
      };
    }
    original.before(panel); refresh();
  }
  function start() { mount(); new MutationObserver(mount).observe(document.getElementById('root') || document.body,{childList:true,subtree:true}); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',start,{once:true}); else start();
})(typeof window === 'undefined' ? null : window);
