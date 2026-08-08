// Drive the dashboard's Add-Provider form with a minimal DOM shim (Plan 021
// WI-5). Run via `node tests/gui/providers_form.mjs`, or through pytest —
// tests/test_gui_providers_form.py wraps it and skips when node is absent.
//
// Pins the load-bearing behaviours: a poll landing mid-edit must not discard
// the form, the registry pick prefills base/auth/name, the save POSTs the
// right payload to /admin/providers, a 400 surfaces the server message, the
// discover probe calls /admin/providers/discover, and provider names are
// escaped rather than injected.
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
  // oninput/onchange are assigned by the form; create them as assignable
  // props so the harness can invoke them directly.
  return {
    id, innerHTML: '', textContent: '', value: '', onclick: null,
    onchange: null, oninput: null, style: {}, type: '',
  };
}

const $ = (id) => document.getElementById(id);
const getEditing = () => vm.runInContext('editingProvider', sandbox);
const setEditing = (v) => vm.runInContext(`editingProvider = ${!!v}`, sandbox);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) });
}

// --- harness state ---------------------------------------------------------
let fetchCalls = [];
let nextResponse = { ok: true, status: 200, json: async () => ({}) };
const els = {};

const document = {
  getElementById(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
};

const sandbox = {
  document,
  console,
  setInterval: () => 0,
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  fetch: async (url, opts) => { fetchCalls.push({ url, opts }); return nextResponse; },
  URLSearchParams,
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;
const timers = [];

// `load()` at the bottom of the script hits /status.json. Point the first
// fetch at a canned payload so evaluation completes without throwing.
nextResponse = {
  ok: true, status: 200,
  json: async () => ({ providers: {}, route_table: {} }),
};

vm.createContext(sandbox);
vm.runInContext(script, sandbox);

// --- 1. the Add Provider button renders -----------------------------------
sandbox.renderAddProvider({ providers: { umans: {} } });
let view = $('add-provider').innerHTML;
check('renders an Add provider button', /Add provider/i.test(view), view.slice(0, 120));

// --- 2. opening the form sets the freeze flag -----------------------------
$('add-provider-btn').onclick();
check('opening the form sets editingProvider', getEditing() === true, getEditing());
const form = $('add-provider').innerHTML;
check('form has a name field', form.includes('id="pf-name"'));
check('form has a base URL field', form.includes('id="pf-base"'));
check('form has a registry picker', form.includes('id="pf-registry"'));
check('form has a live preview region', form.includes('id="pf-preview"'));

// --- 3. a poll landing mid-edit must NOT clobber the form ------------------
const before = $('add-provider').innerHTML;
sandbox.renderAddProvider({ providers: { umans: {} } });
check('poll does not clobber an open form',
      $('add-provider').innerHTML === before);

// --- 4. registry pick prefills base/auth/name -----------------------------
// Seed the registry cache so _loadRegistry resolves without a fetch, then
// re-open the form and apply a pick.
vm.runInContext(
  'registryCache = [{name:"ollama-cloud",default_base_url:"https://ollama.com/v1",auth_header:"authorization"}];',
  sandbox,
);
setEditing(true);
sandbox.renderProviderForm();
// Let the _loadRegistry().then(...) microtask populate the <select>.
await Promise.resolve(); await Promise.resolve();
sandbox._applyRegistryPick('ollama-cloud');
check('registry pick fills the base URL',
      $('pf-base').value === 'https://ollama.com/v1', $('pf-base').value);
check('registry pick fills the auth header',
      $('pf-auth-header').value === 'authorization');
check('registry pick fills the name',
      $('pf-name').value === 'ollama-cloud', $('pf-name').value);

// --- 5. saving sends the right payload ------------------------------------
setEditing(true);
sandbox.renderProviderForm();
$('pf-name').value = 'my-ollama';
$('pf-base').value = 'https://ollama.com/v1';
$('pf-target').value = '3';
$('pf-key-env').value = 'SWITCHBOARD_OLLAMA_KEY';
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const call = fetchCalls.find(c => c.url === '/admin/providers');
check('POSTs to /admin/providers', !!call);
check('uses POST', call?.opts?.method === 'POST');
check('sends same-origin credentials', call?.opts?.credentials === 'same-origin');
check('sets JSON content-type',
      call?.opts?.headers?.['content-type'] === 'application/json');
check('sends the name and upstream',
      call && JSON.parse(call.opts.body).name === 'my-ollama'
          && JSON.parse(call.opts.body).upstream === 'https://ollama.com/v1',
      call && call.opts.body);
check('sends the api_key_env with key_mode env',
      call && JSON.parse(call.opts.body).api_key_env === 'SWITCHBOARD_OLLAMA_KEY'
          && JSON.parse(call.opts.body).key_mode === 'env');
check('save clears editingProvider on success', getEditing() === false, getEditing());

// --- 6. a 400 shows the server's message and stays in edit mode -----------
setEditing(true);
sandbox.renderProviderForm();
$('pf-name').value = 'dup';
$('pf-base').value = 'https://x.example';
nextResponse = {
  ok: false, status: 409,
  json: async () => ({ error: "provider 'dup' already exists" }),
};
await sandbox._saveProvider();
check('surfaces the server error verbatim',
      $('pf-error').textContent === "provider 'dup' already exists",
      $('pf-error').textContent);
check('stays in edit mode after a rejection', getEditing() === true, getEditing());

// --- 7. discover probes /admin/providers/discover and renders results -----
setEditing(true);
sandbox.renderProviderForm();
$('pf-base').value = 'https://api.example.com';
fetchCalls = [];
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    base_url: 'https://api.example.com',
    candidates: [
      { url: 'https://api.example.com/models', status: 404, ok: false, detail: '' },
      { url: 'https://api.example.com/v1/models', status: 200, ok: true, detail: '' },
    ],
  }),
};
await sandbox._runDiscover();
const dcall = fetchCalls.find(c => c.url === '/admin/providers/discover');
check('POSTs to /admin/providers/discover', !!dcall);
check('discover body carries the base URL only (no credential)',
      dcall && JSON.parse(dcall.opts.body).base_url === 'https://api.example.com'
          && !JSON.parse(dcall.opts.body).api_key,
      dcall && dcall.opts.body);
const discoverOut = $('pf-discover').innerHTML;
check('reports how many answered', /1 answered/.test(discoverOut), discoverOut);
check('marks the ok composition', discoverOut.includes('https://api.example.com/v1/models'));

// --- 8. missing base URL is caught before any fetch -----------------------
setEditing(true);
sandbox.renderProviderForm();
$('pf-base').value = '';
fetchCalls = [];
await sandbox._runDiscover();
check('discover without a base URL does not fetch',
      !fetchCalls.some(c => c.url === '/admin/providers/discover'));
check('discover without a base URL shows a message',
      /base URL/i.test($('pf-error').textContent));

// --- 9. save validates required fields ------------------------------------
setEditing(true);
sandbox.renderProviderForm();
$('pf-name').value = '';
$('pf-base').value = 'https://x.example';
fetchCalls = [];
await sandbox._saveProvider();
check('save without a name does not POST',
      !fetchCalls.some(c => c.url === '/admin/providers'));
check('save without a name shows a message',
      /name/i.test($('pf-error').textContent));

// --- 10. XSS: discover output escapes candidate URLs ----------------------
// The candidate URLs come from the server's composition of an operator-typed
// base, and render into the page — the real injection surface for this form.
setEditing(true);
sandbox.renderProviderForm();
$('pf-base').value = 'https://x.example';
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    base_url: 'https://x.example',
    candidates: [
      { url: '<script>bad</script>/models', status: 200, ok: true, detail: '' },
    ],
  }),
};
await sandbox._runDiscover();
const dangerous = $('pf-discover').innerHTML;
check('escapes attacker-influenced URLs in the discover output',
      !dangerous.includes('<script>bad') && dangerous.includes('&lt;'),
      dangerous.slice(0, 160));

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
