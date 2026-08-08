// Drive the dashboard's default-route editor with a minimal DOM shim
// (Plan 020 WI-8). Run via `node tests/gui/default_route.mjs`, or through
// pytest — tests/test_gui_default_route.py wraps it and skips when node is
// absent.
//
// Why a shim and not a browser: the dashboard's only writing control needs
// its behaviour pinned (does a poll landing mid-edit discard the operator's
// typing? does a 400 surface the provider name?), and a headless browser is
// a heavy dependency for a project that otherwise has none. Only the DOM
// surface this code actually touches is stubbed: getElementById, innerHTML,
// onclick, value, textContent, style, plus fetch and timers. Anything richer
// than that in future GUI work will need a real DOM instead.
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
    style: {},
  };
}

const $ = (id) => document.getElementById(id);
// `let editingDefault` is a lexical binding inside the vm script, NOT a
// property of the sandbox object — it must be read/written by evaluating
// in the context, or assertions against it are vacuous.
const getEditing = () => vm.runInContext('editingDefault', sandbox);
const setEditing = (v) => vm.runInContext(`editingDefault = ${!!v}`, sandbox);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail });
}

// --- harness state ---------------------------------------------------------
let fetchCalls = [];
let nextResponse = { ok: true, status: 200, json: async () => ({}) };
const els = {};

const document = {
  getElementById(id) {
    // Elements the code creates by writing innerHTML are synthesized lazily:
    // the shim cannot parse HTML, so we hand back a stub for any id asked for.
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
};

const sandbox = {
  document,
  console,
  setInterval: () => 0,
  setTimeout: (fn) => { return 0; },   // never auto-fire; assert explicitly
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    return nextResponse;
  },
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;

// `load()` runs at the bottom of the script and would hit /status.json.
// Point the first fetch at a canned payload so evaluation completes.
nextResponse = {
  ok: true, status: 200,
  json: async () => ({ providers: {}, route_table: {} }),
};

vm.createContext(sandbox);
vm.runInContext(script, sandbox);

// --- 1. read-only render ---------------------------------------------------
const data = {
  providers: { umans: {}, ollama: {} },
  route_table: { default: ['umans'], abc123deadbeefcafe: ['umans'] },
};
sandbox.renderDefaultRoute(data);
const view = $('default-route').innerHTML;
check('renders current default', view.includes('umans'), view.slice(0, 120));
check('has an Edit button', view.includes('id="edit-default"'));

// --- 2. entering edit mode -------------------------------------------------
sandbox.renderDefaultRoute(data);           // simulate the 5 s poll
$('edit-default').onclick();
check('edit sets the freeze flag', getEditing() === true, getEditing());
const form = $('default-route').innerHTML;
check('form prefilled with current order', form.includes('value="umans"'), form.match(/value="[^"]*"/)?.[0]);
check('datalist offers configured providers',
      form.includes('<option value="ollama">') && form.includes('<option value="umans">'));

// --- 3. a poll landing mid-edit must NOT clobber the form ------------------
const before = $('default-route').innerHTML;
sandbox.renderDefaultRoute(data);
check('poll does not clobber an open form',
      $('default-route').innerHTML === before);

// --- 4. saving sends the right payload ------------------------------------
fetchCalls = [];
nextResponse = {
  ok: true, status: 200,
  json: async () => ({ default: ['ollama', 'umans'], persisted: true }),
};
$('default-input').value = ' ollama , umans ,, ';
await $('save-default').onclick();
const call = fetchCalls.find(c => c.url === '/admin/routes/default');
check('PUTs to /admin/routes/default', !!call);
check('uses PUT', call?.opts?.method === 'PUT');
check('sends same-origin credentials', call?.opts?.credentials === 'same-origin');
check('sets JSON content-type',
      call?.opts?.headers?.['content-type'] === 'application/json');
check('trims and drops empty entries',
      call && JSON.stringify(JSON.parse(call.opts.body).providers) ===
        JSON.stringify(['ollama', 'umans']),
      call && call.opts.body);

// --- 5. a 400 shows the server's message and stays in edit mode -----------
setEditing(true);
sandbox.renderDefaultRouteForm(['umans'], ['umans', 'ollama']);
nextResponse = {
  ok: false, status: 400,
  json: async () => ({ error: 'unknown provider(s): ghost' }),
};
$('default-input').value = 'ghost';
await $('save-default').onclick();
check('surfaces the server error verbatim',
      $('default-error').textContent === 'unknown provider(s): ghost',
      $('default-error').textContent);
check('stays in edit mode after a rejection',
      getEditing() === true, getEditing());

// --- 6. persisted:false is called out, not silently accepted --------------
setEditing(true);
sandbox.renderDefaultRouteForm(['umans'], ['umans', 'ollama']);
nextResponse = {
  ok: true, status: 200,
  json: async () => ({ default: ['ollama'], persisted: false }),
};
$('default-input').value = 'ollama';
await $('save-default').onclick();
check('warns when the write is not durable',
      /not persisted/i.test($('default-error').textContent),
      $('default-error').textContent);

// --- 7. XSS: a provider name is escaped, not injected ---------------------
setEditing(false);
sandbox.renderDefaultRoute({
  providers: { '<img src=x onerror=alert(1)>': {} },
  route_table: { default: ['<script>bad</script>'] },
});
const dangerous = $('default-route').innerHTML;
check('escapes provider names in the read view',
      !dangerous.includes('<script>bad') && dangerous.includes('&lt;'),
      dangerous.slice(0, 160));

// --- 8. configuration provenance panel (Plan 021 WI-7) -------------------
const cfgHtml = sandbox.renderConfig({
  providers: [
    { name: 'opencode-go', source: 'toml',
      field_sources: { target: 'env', upstream: 'env' } },
    { name: 'ollama-cloud', source: 'store' },
    { name: 'zai', source: 'store', enabled: false },
  ],
  unmatched_env_overrides: ['SWITCHBOARD_PROVIDER_TYPO_UPSTREAM'],
});
check('names env-owned fields', /target/.test(cfgHtml) && /upstream/.test(cfgHtml));
check('shows the owning tier per provider',
      /toml/.test(cfgHtml) && /store/.test(cfgHtml));
check('flags a disabled provider', /disabled/.test(cfgHtml));
check('warns about ignored env overrides',
      cfgHtml.includes('SWITCHBOARD_PROVIDER_TYPO_UPSTREAM')
      && /Ignored environment overrides/.test(cfgHtml));
check('says an env-owned edit would be discarded',
      /discarded at the next restart/.test(cfgHtml), cfgHtml.slice(-300));

// A provider with no env-owned fields must not be shown as locked.
const plainHtml = sandbox.renderConfig({
  providers: [{ name: 'solo', source: 'toml' }],
});
check('unlocked provider shows no field list',
      !/color:var\(--amber\)/.test(plainHtml), plainHtml);

// Absent config (unauthenticated dashboard) renders nothing rather than
// throwing and taking the rest of the page with it.
check('missing config renders empty', sandbox.renderConfig(null) === '');

// Provenance data is attacker-influenced only via config, but escape anyway.
const xssHtml = sandbox.renderConfig({
  providers: [{ name: '<script>bad</script>', source: 'toml' }],
});
check('escapes provider names in the config panel',
      !xssHtml.includes('<script>bad'), xssHtml.slice(0, 160));

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
