/* Isolated headless tests. All backend traffic is mocked; NO model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.resolve(__dirname, '../test_tools/node_modules/playwright'));
const dist = path.resolve(__dirname, '../../frontend/dist');
const core = require(path.join(dist, 'byok.js'));
const sentinel = 'local-test-sentinel-not-a-real-key';
(async () => {
  const config = {...core.defaults, enabled:true, base_url:'https://example.invalid/v1', model_pro:'EXACT/Pro:ID', embedding_base_url:'https://example.invalid/v1',embedding_model:'EXACT_EMBED:ID'};
  for (const protocol of ['openai','gemini','anthropic']) {
    const output = core.exportConfig({...config,protocol}, {main:sentinel}, {pubmed_api:['local-pubmed-sentinel'],extra:'kept'});
    assert.equal(output.byok.protocol,protocol); assert.equal(output.byok.model_flash,config.model_pro); assert.equal(output.extra,'kept'); assert.equal(output.byok.embedding.api[0],sentinel);
  }
  assert.throws(()=>core.exportConfig({...config,embedding_model:''},{main:sentinel}));
  assert.equal(core.exportConfig({...config,base_url:'http://host.docker.internal:11434/v1'},{main:sentinel}).byok.base_url,'http://host.docker.internal:11434/v1');
  assert.throws(()=>core.exportConfig({...config,base_url:'https://example.invalid/v1?key=sentinel'},{main:sentinel}));
  assert.throws(()=>core.exportConfig({...config,model_pro:' EXACT/Pro:ID'},{main:sentinel}));
  assert.equal(core.exportConfig({...config,embedding_enabled:false},{main:sentinel}).byok.embedding.enabled,false);
  assert.ok(!JSON.stringify(core.nonSecret({...config,main:sentinel,api:[sentinel]})).includes(sentinel));
  console.log('PASS pure contract: all protocols, exact IDs, flash fallback, required embedding, HTTP(S), secret exclusion');
  const server = http.createServer((req,res)=> {
    let file = path.join(dist,new URL(req.url,'http://localhost').pathname);
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(dist,'index.html');
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'); res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    const windowsChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
    const executablePath = process.env.CHROME_PATH || (process.platform === 'win32' && fs.existsSync(windowsChrome) ? windowsChrome : undefined);
    browser = await chromium.launch({headless:true, executablePath});
    const context = await browser.newContext(); // Fresh context; no user's profile/cookies/storage.
    const page = await context.newPage();
    const traffic = [];
    await page.route('**/*',async route=> {
      const request = route.request(), url = new URL(request.url());
      if (url.origin === 'http://localhost:8000') {
        if (request.method()==='POST') traffic.push({path:url.pathname,body:request.postDataJSON(),headers:request.headers()});
        return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({success:false,message:{zh:'本地模拟响应：未发送真实模型请求',en:'LOCAL MOCK ONLY: no real model request sent'}})});
      }
      if (url.hostname !== '127.0.0.1') return route.abort();
      return route.continue();
    });
    const url = `http://127.0.0.1:${server.address().port}/search/task`;
    await page.goto(url);
    await page.waitForSelector('#pubchat-byok');
    await page.locator('#pubchat-byok summary').click();
    const field = name=>page.locator(`#pubchat-byok [data-field="${name}"]`);
    await field('enabled').check();
    assert.equal(await field('main').getAttribute('type'),'password');
    assert.equal(await field('embedding_enabled').isChecked(),true);
    await page.locator('[data-validate]').click();
    assert.match(await page.locator('[data-status]').textContent(),/required/);
    for (const name of ['base_url','model_pro','embedding_base_url','embedding_model']) await field(name).fill(config[name]);
    await field('main').fill(sentinel);
    await page.locator('[data-validate]').click();
    assert.match(await page.locator('[data-status]').textContent(),/Valid locally/);
    const storage = await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));
    assert.ok(!storage.includes(sentinel));
    const payload = {user_query:'local UI test',search_filters:{author:'Sentinel Author'},journal_filters:{jcr_zone:'Q1-Q4'},llm_config:{model:'google_gemini',api:[],pubmed_api:['local-pubmed-sentinel']}};
    await page.evaluate(async payload=>{await fetch('http://localhost:8000/api/search/task',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Test':'preserved'},body:JSON.stringify(payload)});},payload);
    const captured = traffic.at(-1); assert.equal(captured.body.llm_config.model,'byok'); assert.equal(captured.body.llm_config.api[0],sentinel); assert.deepEqual(captured.body.search_filters,payload.search_filters); assert.deepEqual(captured.body.llm_config.pubmed_api,payload.llm_config.pubmed_api); assert.equal(captured.headers['x-local-test'],'preserved');
    await page.evaluate(async payload=>{await fetch(new Request('http://localhost:8000/api/search/task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));},payload);
    assert.equal(traffic.at(-1).body.llm_config.model,'byok');
    await page.evaluate(async payload=>{await fetch('http://localhost:8000/api/search/task/stop',{method:'POST',body:JSON.stringify(payload)});},payload);
    assert.equal(traffic.at(-1).body.llm_config.model,'google_gemini');
    await field('embedding_enabled').uncheck(); assert.equal(await page.locator('[data-disable-notice]').isVisible(),true);
    await field('embedding_enabled').check(); await field('reuse_key').uncheck(); await field('embedding').fill('local-embedding-sentinel');
    await field('embedding_protocol').selectOption('gemini');
    page.once('dialog',dialog=>dialog.dismiss()); await page.locator('[data-test]').click(); assert.ok(!traffic.some(x=>x.path==='/api/search/byok/test'));
    page.once('dialog',dialog=>dialog.accept()); await page.locator('[data-test]').click(); await page.waitForFunction(()=>document.querySelector('[data-status]').textContent.includes('LOCAL MOCK ONLY'));
    const test = traffic.find(x=>x.path==='/api/search/byok/test'); assert.equal(test.body.llm_config.byok.embedding.api[0],'local-embedding-sentinel');
    // Exercise actual original React search handler with original AI-key field empty.
    await page.locator('.search-box-container textarea').fill('local-test research question');
    await page.locator('.ai-api-config-section input[type=password]').nth(1).fill('local-pubmed-sentinel');
    const before = traffic.filter(x=>x.path==='/api/search/task').length;
    await page.locator('.search-btn').click();
    await page.waitForFunction(()=>document.body.textContent.includes('LOCAL MOCK ONLY'));
    assert.equal(traffic.filter(x=>x.path==='/api/search/task').length,before+1);
    assert.equal(traffic.at(-1).body.llm_config.model,'byok');
    await page.reload(); await page.waitForSelector('#pubchat-byok');
    assert.equal(await field('main').inputValue(),''); assert.equal(await field('embedding').inputValue(),''); assert.equal(await field('enabled').isChecked(),true);
    const count = traffic.length;
    const blocked = await page.evaluate(async payload=>(await fetch('http://localhost:8000/api/search/task',{method:'POST',body:JSON.stringify(payload)})).status,payload);
    assert.equal(blocked,400); assert.equal(traffic.length,count);
    await field('enabled').uncheck();
    await page.evaluate(async payload=>{await fetch('http://localhost:8000/api/search/task',{method:'POST',body:JSON.stringify(payload)});},payload);
    assert.equal(traffic.at(-1).body.llm_config.model,'google_gemini');
    // Both original Gemini choices remain selectable.
    await page.locator('.ai-model-dropdown-trigger').click();
    assert.equal(await page.locator('.ai-model-dropdown-option').count(),2);
    assert.match(await page.locator('.ai-model-dropdown-menu').textContent(),/Google.*OpenRouter/);
    console.log('PASS isolated Chromium UI: mount, masking, visible validation, persistence without secrets, exact fetch interception, Request support, original React handler, embedding notice/separate key, confirmed MOCK test endpoint, reload clears keys/fails closed, Gemini preset options');
    await context.close();
  } finally { if(browser) await browser.close(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error.message);process.exitCode=1;});
