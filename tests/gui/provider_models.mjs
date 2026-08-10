// Drive the dashboard's provider-model enumeration and auto-match scan
// (Plan 024 WI-2/WI-3). Run via `node tests/gui/provider_models.mjs`, or
// through pytest — tests/test_gui_provider_models.py wraps it and skips when
// node is absent.
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import vm from 'node:vm';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const html = fs.readFileSync(
  path.join(here, '..', '..', 'src', 'switchboard', 'static', 'dashboard.html'),
  'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', onclick: null,
    onchange: null, oninput: null, style: {}, type: '',
  };
}

const $ = (id) => document.getElementById(id);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) });
}

let fetchCalls = [];
let nextResponse = { ok: true, status: 200, json: async () => ({}) };
// fetch routes by URL prefix: status.json is the default; /admin/providers/.../models
// and /admin/model-map get explicit overrides set per-test.
let modelsResponse = null;
let modelMapResponse = null;
const els = {};
const document = { getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; } };
const sandbox = {
  document, console,
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    const u = String(url);
    if (u.endsWith('/models')) return modelsResponse || nextResponse;
    if (u === '/admin/model-map') return modelMapResponse || nextResponse;
    return nextResponse;
  },
  URLSearchParams, confirm: () => true, alert: () => {},
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;

// Seed status.json so the initial load() renders the Model Map section with
// a defined model and two providers.
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { umans: {}, 'ollama-cloud': {} },
    route_table: { default: ['umans'] },
    model_map: { 'glm-5.2': { umans: 'umans-glm-5.2' } },
  }),
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);

// --- 1. Scan button renders in the Model Map section ----------------------
sandbox.renderModelAdd({
  providers: { umans: {}, 'ollama-cloud': {} },
  model_map: { 'glm-5.2': { umans: 'umans-glm-5.2' } },
});
check('renders Scan for exact-match button',
      /Scan for exact-match/.test($('model-add').innerHTML));

// --- 2. "show models" populates a per-provider datalist -------------------
sandbox.renderModelAddForm(['umans', 'ollama-cloud']);
check('alias row has a show-models button',
      !!$('mf-umans-show') && !!$('mf-ollama-cloud-show'));
check('alias input has a datalist',
      !!$('mf-umans-models') && !!$('mf-ollama-cloud-models'));

modelsResponse = {
  ok: true, status: 200,
  json: async () => ({ ok: true, models: ['glm-5.2', 'kimi-k3'], detail: '' }),
};
// Reset the in-form cache so the fetch actually fires.
vm.runInContext('modelEnums = {}', sandbox);
fetchCalls = [];
await $('mf-umans-show').onclick();
const umansCall = fetchCalls.find(c => c.url === '/admin/providers/umans/models');
check('show-models fetches /admin/providers/<name>/models', !!umansCall);
check('show-models fetches with same-origin credentials',
      umansCall?.opts?.credentials === 'same-origin');
check('datalist is populated with the model IDs',
      $('mf-umans-models').innerHTML.includes('glm-5.2') &&
      $('mf-umans-models').innerHTML.includes('kimi-k3'));
check('status shows model count',
      /2 model/.test($('mf-umans-status').textContent));

// --- 3. show-models surfaces a parse failure, does not crash -------------
modelsResponse = {
  ok: true, status: 200,
  json: async () => ({ ok: false, models: [], detail: 'no models in response' }),
};
vm.runInContext('modelEnums = {}', sandbox);
fetchCalls = [];
await $('mf-ollama-cloud-show').onclick();
check('parse failure shows detail in status',
      $('mf-ollama-cloud-status').textContent.includes('no models'));
check('failed datalist stays empty',
      $('mf-ollama-cloud-models').innerHTML === '');

// --- 3b. Finding #1: double-click on "show models" does not throw ----------
// The bug: _fetchProviderModels stored null as an in-flight marker, the
// guard was `!== undefined`, so a concurrent caller got null back and
// `res.models` threw a TypeError. The fix stores the promise itself, so
// the second caller awaits it and gets the resolved result.
// Simulate a slow upstream with a deferred json() so two concurrent calls
// race before the first resolves.
let _resolveBody;
const _bodyPromise = new Promise(r => { _resolveBody = r; });
modelsResponse = {
  ok: true, status: 200,
  json: async () => _bodyPromise,
};
vm.runInContext('modelEnums = {}', sandbox);
fetchCalls = [];
// Fire two concurrent calls WITHOUT awaiting the first.
const p1 = sandbox._fetchProviderModels('umans');
const p2 = sandbox._fetchProviderModels('umans');
try {
  _resolveBody({ ok: true, models: ['glm-5.2'], detail: '' });
  const [r1, r2] = await Promise.all([p1, p2]);
  check('finding #1: double-click does not throw (both resolved)',
        r1 && r2 && Array.isArray(r1.models) && Array.isArray(r2.models),
        `${r1} / ${r2}`);
  check('finding #1: both calls get the same result',
        JSON.stringify(r1) === JSON.stringify(r2),
        `${JSON.stringify(r1)} vs ${JSON.stringify(r2)}`);
} catch (e) {
  check('finding #1: double-click does not throw', false, String(e));
}
check('finding #1: only one upstream call for concurrent fetches',
      fetchCalls.filter(c => c.url === '/admin/providers/umans/models').length === 1,
      `${fetchCalls.filter(c => c.url === '/admin/providers/umans/models').length} calls`);

// --- 4. auto-match scan finds an exact match for a defined model ---------
// ollama-cloud offers 'glm-5.2' (exact match) and is NOT already wired.
modelsResponse = {
  ok: true, status: 200,
  json: async () => ({ ok: true, models: ['glm-5.2', 'deepseek-v4-flash'], detail: '' }),
};
vm.runInContext('modelEnums = {}', sandbox);
fetchCalls = [];
const mm = { 'glm-5.2': { umans: 'umans-glm-5.2' } };
await sandbox.runAutoMatchScan(['umans', 'ollama-cloud'], ['glm-5.2'], mm);
// Both providers were probed.
check('scan probes every provider',
      fetchCalls.some(c => c.url === '/admin/providers/umans/models') &&
      fetchCalls.some(c => c.url === '/admin/providers/ollama-cloud/models'));
const scanOut = $('scan-match-results').innerHTML;
check('scan renders an Add offer for the exact match',
      /Add/.test(scanOut) && scanOut.includes('ollama-cloud') && scanOut.includes('glm-5.2'));
check('scan does not offer an already-wired provider',
      !/<button[^>]*>Add<\/button>.*umans/.test(scanOut));

// --- 5. clicking an offer POSTs a MERGED alias set ------------------------
// The model map POST replaces the whole alias set, so the offer must merge
// the existing umans alias with the new ollama-cloud one. Finding #2 fix:
// applyAutoMatch re-reads /admin/model-map (GET) before merging, so the
// merge sees concurrent alias adds the stale snapshot would drop.
// Here: GET returns umans already wired; the offer adds ollama-cloud.
modelMapResponse = {
  ok: true, status: 200,
  json: async () => ({ models: [{ model: 'glm-5.2', aliases: { umans: 'umans-glm-5.2' } }] }),
};
fetchCalls = [];
const offerBtn = $('mf-am-0');
check('offer button exists', !!offerBtn && typeof offerBtn.onclick === 'function');
if (offerBtn) await offerBtn.onclick();
// First fetch is the GET re-read; the POST is the second /admin/model-map.
const mmPost = fetchCalls.find(c => c.url === '/admin/model-map' && c.opts && c.opts.method === 'POST');
check('offer POSTs to /admin/model-map', !!mmPost);
const sentBody = mmPost ? JSON.parse(mmPost.opts.body) : {};
check('offer merges existing aliases (re-read), not replaces',
      sentBody.aliases && sentBody.aliases.umans === 'umans-glm-5.2' &&
      sentBody.aliases['ollama-cloud'] === 'glm-5.2',
      mmPost && mmPost.opts.body);

// --- 5b. Finding #2: a concurrent alias add is not dropped ----------------
// The snapshot (currentMap) has umans only — this is the state at the last
// load(). Between load and click, a concurrent add wired ollama-cloud (which
// only the re-read sees). The POST must keep umans AND the concurrent
// ollama-cloud AND add the new zai alias. Without the re-read, the stale
// snapshot would POST umans+zai only, silently dropping ollama-cloud.
const staleSnapshot = { 'glm-5.2': { umans: 'umans-glm-5.2' } };
const freshGetModels = {
  models: [
    {
      model: 'glm-5.2',
      aliases: {
        umans: 'umans-glm-5.2',
        'ollama-cloud': 'ollama-glm-5.2',
      },
    },
  ],
};
modelMapResponse = { ok: true, status: 200, json: async () => freshGetModels };
fetchCalls = [];
const offer3 = { model: 'glm-5.2', provider: 'zai', alias: 'zai-glm-5.2' };
await sandbox.applyAutoMatch(offer3, staleSnapshot);
const post3 = fetchCalls.find(
  c => c.url === '/admin/model-map' && c.opts && c.opts.method === 'POST',
);
const body3 = post3 ? JSON.parse(post3.opts.body) : {};
check('finding #2: concurrent alias add survives the merge',
      body3.aliases &&
      body3.aliases.umans === 'umans-glm-5.2' &&
      body3.aliases['ollama-cloud'] === 'ollama-glm-5.2' &&
      body3.aliases.zai === 'zai-glm-5.2',
      post3 && post3.opts.body);

// --- 6. a provider that fails the scan is listed, not aborting it --------
modelsResponse = {
  ok: true, status: 200,
  json: async () => ({ ok: false, models: [], detail: 'timeout' }),
};
vm.runInContext('modelEnums = {}', sandbox);
fetchCalls = [];
await sandbox.runAutoMatchScan(['umans', 'ollama-cloud'], ['glm-5.2'], mm);
const failOut = $('scan-match-results').innerHTML;
check('failed provider is named in the scan output',
      failOut.includes('umans') && failOut.includes('timeout'));
check('status counts unavailable providers',
      /unavailable/.test($('scan-match-status').textContent));

// --- 7. scan with no defined models is a no-op message -------------------
vm.runInContext('modelEnums = {}', sandbox);
fetchCalls = [];
await sandbox.runAutoMatchScan(['umans'], [], {});
check('scan with no models does not probe',
      !fetchCalls.some(c => c.url.endsWith('/models')));
check('scan with no models shows a message',
      /add a model mapping first/.test($('scan-match-results').innerHTML));

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);