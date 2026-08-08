// Drive the dashboard's keyed-route and model-map management (Plan 020
// WI-7/WI-8). Run via `node tests/gui/routes_models.mjs`, or through pytest —
// tests/test_gui_routes_models.py wraps it and skips when node is absent.
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
const getEditingRoute = () => vm.runInContext('editingRoute', sandbox);
const getEditingModel = () => vm.runInContext('editingModel', sandbox);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) });
}

let fetchCalls = [];
let nextResponse = { ok: true, status: 200, json: async () => ({}) };
const els = {};
const document = { getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; } };
const sandbox = {
  document, console,
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  fetch: async (url, opts) => { fetchCalls.push({ url, opts }); return nextResponse; },
  URLSearchParams, confirm: () => true, alert: () => {},
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;

// Seed status.json so the initial load() at script-eval completes and renders
// the tables WITH a keyed route and a mapped model.
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { umans: {}, 'ollama-cloud': {} },
    route_table: { default: ['umans'], deadbeefcafe: ['umans', 'ollama-cloud'] },
    model_map: { 'glm-5.2': { umans: 'umans-glm-5.2', 'ollama-cloud': 'glm-5.2' } },
  }),
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);

// --- 1. Add-route button + freeze -----------------------------------------
sandbox.renderRouteAdd({ providers: { umans: {} } });
check('renders Add keyed route button',
      /Add keyed route/.test($('route-add').innerHTML));
$('add-route-btn').onclick();
check('opening route form sets editingRoute', getEditingRoute() === true);
const rform = $('route-add').innerHTML;
check('route form has key + providers fields',
      rform.includes('id="rf-key"') && rform.includes('id="rf-providers"'));
const before = $('route-add').innerHTML;
sandbox.renderRouteAdd({ providers: { umans: {} } });
check('poll does not clobber an open route form', $('route-add').innerHTML === before);

// --- 2. saving a route POSTs the right payload ----------------------------
sandbox.renderRouteAddForm();
$('rf-key').value = 'sk-secret-1234';
$('rf-providers').value = ' umans , ollama-cloud ,, ';
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await $('rf-save').onclick();
const rcall = fetchCalls.find(c => c.url === '/admin/routes');
check('POSTs to /admin/routes', !!rcall);
check('route save sends same-origin credentials', rcall?.opts?.credentials === 'same-origin');
check('route save trims/drops empty providers',
      rcall && JSON.stringify(JSON.parse(rcall.opts.body).providers) ===
        JSON.stringify(['umans', 'ollama-cloud']),
      rcall && rcall.opts.body);
check('route save clears editingRoute on success', getEditingRoute() === false);

// --- 3. a 400 surfaces the message and stays in edit ----------------------
vm.runInContext('editingRoute = true', sandbox);
sandbox.renderRouteAddForm();
$('rf-key').value = 'sk-x';
$('rf-providers').value = 'ghost';
nextResponse = { ok: false, status: 400, json: async () => ({ error: 'unknown provider(s): ghost' }) };
await $('rf-save').onclick();
check('route save surfaces server error',
      $('rf-error').textContent === 'unknown provider(s): ghost');
check('route save stays in edit on rejection', getEditingRoute() === true);

// --- 4. deleting a keyed route --------------------------------------------
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({ removed: true }) };
await sandbox.deleteRoute('deadbeefcafe');
const dcall = fetchCalls.find(c => c.url === '/admin/routes/deadbeefcafe');
check('DELETEs /admin/routes/<key>', !!dcall && dcall.opts.method === 'DELETE');

// --- 5. Add-model button + per-provider alias form ------------------------
sandbox.renderModelAdd({ providers: { umans: {}, 'ollama-cloud': {} } });
check('renders Add model mapping button',
      /Add model mapping/.test($('model-add').innerHTML));
$('add-model-btn').onclick();
check('opening model form sets editingModel', getEditingModel() === true);
const mform = $('model-add').innerHTML;
check('model form has a model-name field', mform.includes('id="mf-model"'));
check('model form renders an alias input per provider',
      mform.includes('id="mf-alias-umans"') && mform.includes('id="mf-alias-ollama-cloud"'));

// --- 6. saving a model POSTs aliases, blank aliases dropped ---------------
$('mf-model').value = 'kimi-k2.7';
$('mf-alias-umans').value = 'umans-kimi';
// ollama-cloud left blank → dropped.
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await $('mf-save').onclick();
const mcall = fetchCalls.find(c => c.url === '/admin/model-map');
check('POSTs to /admin/model-map', !!mcall);
check('model save sends only filled aliases',
      mcall && JSON.stringify(JSON.parse(mcall.opts.body).aliases) ===
        JSON.stringify({ umans: 'umans-kimi' }),
      mcall && mcall.opts.body);
check('model save clears editingModel on success', getEditingModel() === false);

// --- 7. model save validates ----------------------------------------------
vm.runInContext('editingModel = true', sandbox);
sandbox.renderModelAddForm(['umans']);
$('mf-model').value = 'x';
$('mf-alias-umans').value = '';  // no aliases
fetchCalls = [];
await $('mf-save').onclick();
check('model save with no aliases does not POST',
      !fetchCalls.some(c => c.url === '/admin/model-map'));
check('model save with no aliases shows a message',
      /alias/i.test($('mf-error').textContent));

// --- 8. deleting a model ---------------------------------------------------
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({ removed: true }) };
await sandbox.deleteModel('glm-5.2');
const mdcall = fetchCalls.find(c => c.url === '/admin/model-map/glm-5.2');
check('DELETEs /admin/model-map/<model>', !!mdcall && mdcall.opts.method === 'DELETE');

// --- 9. tables render per-row delete buttons (post-render wiring) ---------
// Re-run load() with the seeded data so the #app tables + delete buttons are
// built and their handlers assigned.
fetchCalls = [];
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { umans: {} },
    route_table: { default: ['umans'], k1: ['umans'] },
    model_map: { 'glm-5.2': { umans: 'umans-glm-5.2' } },
  }),
};
await sandbox.load();
check('keyed-route table has a delete button', !!$('del-route-0'));
check('model-map table has a delete button', !!$('del-model-0'));
// The handler was wired post-render and points at the right key.
nextResponse = { ok: true, status: 200, json: async () => ({}) };
if ($('del-route-0') && $('del-route-0').onclick) await $('del-route-0').onclick();
check('row delete button calls deleteRoute for its key',
      fetchCalls.some(c => c.url === '/admin/routes/k1'));

// --- 10. XSS: route/model content is escaped in the tables ----------------
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: {},
    route_table: { default: [], '<script>bad</script>': ['p'] },
    model_map: { '<img src=x onerror=alert(1)>': {} },
  }),
};
await sandbox.load();
const appHtml = $('app').innerHTML;
check('escapes attacker-controlled route keys',
      !appHtml.includes('<script>bad') && appHtml.includes('&lt;'));
check('escapes attacker-controlled model names',
      !appHtml.includes('<img src=x onerror'));

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
