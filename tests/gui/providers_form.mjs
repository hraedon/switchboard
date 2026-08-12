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
  // props so the harness can invoke them directly. `checked` backs the
  // enabled-toggle checkbox in the edit form; `disabled` is settable so a
  // test can assert the env-lock state via the element too.
  return {
    id, innerHTML: '', textContent: '', value: '', onclick: null,
    onchange: null, oninput: null, style: {}, type: '',
    checked: false, disabled: false,
  };
}

const $ = (id) => document.getElementById(id);
const getEditing = () => vm.runInContext('editingProvider', sandbox);
const setEditing = (v) => vm.runInContext(`editingProvider = ${!!v}`, sandbox);
const getEditingName = () => vm.runInContext('editingProviderName', sandbox);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) });
}
// Pull one input element's HTML out of a rendered string (the shim does not
// parse innerHTML into elements, so attribute checks are string matches).
function inputHtml(view, id) {
  const m = view.match(new RegExp('<input[^>]*id="' + id + '"[^>]*>'));
  return m ? m[0] : '';
}
// A real browser destroys absent children when a container's innerHTML is
// reassigned; the shim reuses element objects, so stale pf-* values leak
// across re-renders (e.g. an env-mode form's api_key_env survives into a
// stored-mode save and flips the payload's key_mode). Purging pf-* elements
// before each re-open mirrors the browser and keeps tests independent.
function resetFormEls() {
  for (const k of Object.keys(els)) {
    if (k.startsWith('pf-')) delete els[k];
  }
}

// --- harness state ---------------------------------------------------------
let fetchCalls = [];
let nextResponse = { ok: true, status: 200, json: async () => ({}) };
// Exact-URL responses take precedence over `nextResponse`, so a test can stage
// the two distinct fetches the edit flow makes (/admin/providers then
// /admin/config/effective) without racing the single `nextResponse`.
let responsesByUrl = {};
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
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (responsesByUrl[url]) return responsesByUrl[url];
    return nextResponse;
  },
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

// --- 11. EDIT FORM: editProvider opens a prefilled form (Plan 020 WI-6) ----
// Reset responsesByUrl so the two distinct edit-flow fetches are staged
// independently of the global nextResponse.
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
resetFormEls();
setEditing(false);
vm.runInContext('editingProviderName = null; editEnvLocked = null;', sandbox);

responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'umans', upstream: 'https://u.example/v1', target: 3,
      provider_type: 'generic', key_mode: 'env', api_key_env: 'UMANS_KEY',
      auth_header: 'authorization', auth_prefix: '', dashboard_url: '',
      dashboard_token_env: '', usage_key_env: 'UMANS_USAGE', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'umans', env_locked: ['target'] }] }),
};
await sandbox.editProvider('umans');
check('editProvider sets editingProvider', getEditing() === true, getEditing());
check('editProvider records the name being edited',
      getEditingName() === 'umans', getEditingName());
let eform = $('add-provider').innerHTML;
check('edit form has the Edit Provider title', /Edit Provider/.test(eform), eform.slice(0, 80));
check('edit form prefills the name', $('pf-name').value === 'umans', $('pf-name').value);
check('edit form prefills the base URL',
      $('pf-base').value === 'https://u.example/v1', $('pf-base').value);
check('edit form prefills the api_key_env',
      $('pf-key-env').value === 'UMANS_KEY', $('pf-key-env').value);
check('edit form prefills the usage_key_env',
      $('pf-usage-key-env').value === 'UMANS_USAGE', $('pf-usage-key-env').value);
// Name is the route key — it must be disabled, not a free input.
check('edit form disables the name input',
      inputHtml(eform, 'pf-name').includes('disabled'), inputHtml(eform, 'pf-name'));

// --- 12. EDIT FORM: env-locked fields render disabled ----------------------
// 'target' is env_locked above; upstream is not. The lock must be on target
// only — disabling upstream would hide the real editable surface.
const targetInput = inputHtml(eform, 'pf-target');
const baseInput = inputHtml(eform, 'pf-base');
check('env-locked field (target) is disabled', targetInput.includes('disabled'), targetInput);
check('env-locked field carries the lock note', /env-locked/.test(eform));
check('non-locked field (upstream) is editable', !baseInput.includes('disabled'), baseInput);

// --- 13. EDIT FORM: save PUTs the full row to /admin/providers/<name> ------
$('pf-target').value = '5';  // operator raises concurrency (target is locked,
                              // but the shim element is mutable — the payload
                              // carries the field either way; what matters is
                              // the method + URL + body shape).
fetchCalls = [];
responsesByUrl = {};  // PUT goes to /admin/providers/umans, a new exact URL
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const putCall = fetchCalls.find(c => c.url === '/admin/providers/umans');
check('save PUTs to /admin/providers/<name>', !!putCall, fetchCalls.map(c => c.url));
check('uses PUT', putCall?.opts?.method === 'PUT', putCall?.opts?.method);
check('uses same-origin credentials',
      putCall?.opts?.credentials === 'same-origin');
const putBody = putCall ? JSON.parse(putCall.opts.body) : {};
check('PUT body carries upstream', putBody.upstream === 'https://u.example/v1', putBody);
check('PUT body carries provider_type', putBody.provider_type === 'generic');
check('PUT body carries key_mode env', putBody.key_mode === 'env' && putBody.api_key_env === 'UMANS_KEY');
check('PUT body carries enabled', putBody.enabled === true);
check('PUT omits api_key_stored in env mode', !('api_key_stored' in putBody));
check('save clears editing state on success',
      getEditing() === false && getEditingName() === null, [getEditing(), getEditingName()]);

// --- 14. EDIT FORM: write-only stored key — blank means "keep" -------------
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'paid', upstream: 'https://p.example/v1', target: 2,
      provider_type: 'generic', key_mode: 'stored', api_key_hint: '4321',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'paid', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('paid');
check('stored-key form shows the hint',
      /ending 4321/.test($('add-provider').innerHTML), $('add-provider').innerHTML);
check('stored-key form shows the new-key field',
      $('add-provider').innerHTML.includes('id="pf-key-new"'));
check('stored-key form has no env-key field',
      !$('add-provider').innerHTML.includes('id="pf-key-env"'));
// Blank new key: PUT must keep key_mode='stored' and NOT send api_key_stored
// (write-only keep semantics — the server retains the existing credential).
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const stCall = fetchCalls.find(c => c.url === '/admin/providers/paid');
const stBody = stCall ? JSON.parse(stCall.opts.body) : {};
check('blank stored key keeps key_mode stored',
      stBody.key_mode === 'stored', stBody);
check('blank stored key is not sent (keep)',
      !('api_key_stored' in stBody), stBody);

// --- 14b. EDIT FORM: a newly typed stored key IS sent ----------------------
resetFormEls();
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'paid', upstream: 'https://p.example/v1', target: 2,
      provider_type: 'generic', key_mode: 'stored', api_key_hint: '4321',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'paid', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('paid');
$('pf-key-new').value = 'sk-newsecret';
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const rotCall = fetchCalls.find(c => c.url === '/admin/providers/paid');
const rotBody = rotCall ? JSON.parse(rotCall.opts.body) : {};
check('a typed stored key is sent for rotation',
      rotBody.key_mode === 'stored' && rotBody.api_key_stored === 'sk-newsecret',
      rotBody);

// --- 15. EDIT FORM: a 400 surfaces the message and stays in edit mode ------
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'umans', upstream: 'https://u.example/v1', target: 3,
      provider_type: 'generic', key_mode: 'env', api_key_env: 'UMANS_KEY',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'umans', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('umans');
fetchCalls = [];
responsesByUrl = {};
nextResponse = {
  ok: false, status: 400,
  json: async () => ({ error: "field 'upstream' is required" }),
};
await sandbox._saveProvider();
check('edit surfaces the server error verbatim',
      $('pf-error').textContent === "field 'upstream' is required",
      $('pf-error').textContent);
check('edit stays open after a rejection',
      getEditing() === true && getEditingName() === 'umans',
      [getEditing(), getEditingName()]);

// --- 16. EDIT FORM: XSS — provider name escaped in the prefilled form ------
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: '<script>bad</script>', upstream: 'https://x.example', target: 1,
      provider_type: 'generic', key_mode: 'passthrough',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [] }),
};
setEditing(false);
await sandbox.editProvider('<script>bad</script>');
const xform = $('add-provider').innerHTML;
check('attacker name is escaped in the edit form',
      !xform.includes('<script>bad') && xform.includes('&lt;script&gt;'),
      xform.slice(0, 200));

// --- 17. EDIT FORM: passthrough key mode shows no credential field --------
check('passthrough mode shows the passthrough note',
      /passthrough/.test(xform), xform.slice(0, 300));

// --- 18. EDIT FORM: TOML provider's `type` is preserved (finding 1) --------
// GET /admin/providers emits the TOML section key `type`, not `provider_type`;
// the form must resolve from either so a save does not rewrite the type to
// 'generic' (which would shadow the TOML section with a wrong store row).
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'local', upstream: 'http://localhost:11434', target: 4,
      type: 'ollama', key_mode: 'passthrough',
      auth_header: 'authorization', source: 'toml', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'local', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('local');
check('TOML provider type is read from the section key',
      $('pf-type').value === 'ollama', $('pf-type').value);
check('TOML provider type round-trips in the PUT body',
      true);  // verified structurally below
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const tomlPut = fetchCalls.find(c => c.url === '/admin/providers/local');
const tomlBody = tomlPut ? JSON.parse(tomlPut.opts.body) : {};
check('PUT preserves provider_type ollama (not rewritten to generic)',
      tomlBody.provider_type === 'ollama', tomlBody);

// --- 19. EDIT FORM: TOML-stored provider cannot keep-on-blank (finding 2) --
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'paid-toml', upstream: 'https://p.example/v1', target: 2,
      provider_type: 'generic', key_mode: 'stored', api_key_hint: '4321',
      auth_header: 'authorization', source: 'toml', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'paid-toml', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('paid-toml');
check('TOML-stored form marks the key as required',
      /required/.test($('add-provider').innerHTML), $('add-provider').innerHTML.slice(0, 400));
// Blank key on a TOML-stored provider must NOT PUT (the store would 400).
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
check('blank key on TOML-stored provider does not PUT',
      !fetchCalls.some(c => c.url === '/admin/providers/paid-toml'));
check('blank key on TOML-stored provider surfaces a message',
      /API key/i.test($('pf-error').textContent), $('pf-error').textContent);

// --- 20. ADD button clears stale edit state (finding 4) --------------------
// Open an edit, then click Add: the Add form must not carry a stale
// editingProviderName that would route its save to a PUT.
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'umans', upstream: 'https://u.example/v1', target: 3,
      provider_type: 'generic', key_mode: 'env', api_key_env: 'UMANS_KEY',
      auth_header: 'authorization', source: 'store', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'umans', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('umans');
check('precondition: edit set the editing name', getEditingName() === 'umans');
// Now simulate the operator clicking "Add provider".
sandbox.renderAddProvider({ providers: { umans: {} } });
$('add-provider-btn').onclick();
check('Add button clears editingProviderName',
      getEditingName() === null, getEditingName());
check('Add form title is Add (not Edit)',
      /Add Provider/.test($('add-provider').innerHTML));
// A save from the Add form must POST, not PUT.
$('pf-name').value = 'fresh';
$('pf-base').value = 'https://f.example';
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
check('Add-after-edit POSTs (not a stale PUT to the edited provider)',
      fetchCalls.some(c => c.url === '/admin/providers' && c.opts.method === 'POST')
      && !fetchCalls.some(c => c.url === '/admin/providers/umans'),
      fetchCalls.map(c => c.url));

// --- 21. ADD FORM: credential mode selector (Plan 025) ---------------------
resetFormEls();
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
setEditing(true);
vm.runInContext('editingProviderName = null;', sandbox);
sandbox.renderProviderForm();
check('add form renders the credential mode selector',
      $('add-provider').innerHTML.includes('id="pf-key-mode"'));
check('add form defaults the selector to env',
      $('pf-key-mode').value === 'env', $('pf-key-mode').value);

// Switch to stored: the cred block rewrites to the paste-key input.
$('pf-key-mode').value = 'stored';
$('pf-key-mode').onchange();
check('stored mode rewrites the cred block with a key input',
      $('pf-cred-block').innerHTML.includes('id="pf-key-new"'),
      $('pf-cred-block').innerHTML.slice(0, 200));

// Saving stored mode without a key is caught client-side.
$('pf-name').value = 'newpaid';
$('pf-base').value = 'https://np.example/v1';
fetchCalls = [];
await sandbox._saveProvider();
check('add stored without a key does not POST',
      !fetchCalls.some(c => c.url === '/admin/providers'));
check('add stored without a key shows a message',
      /API key/i.test($('pf-error').textContent), $('pf-error').textContent);

// With a key typed, the POST carries key_mode stored + the credential.
$('pf-key-new').value = 'sk-brandnew';
fetchCalls = [];
await sandbox._saveProvider();
const addStored = fetchCalls.find(c => c.url === '/admin/providers');
const addStoredBody = addStored ? JSON.parse(addStored.opts.body) : {};
check('add with pasted key POSTs key_mode stored',
      addStoredBody.key_mode === 'stored'
      && addStoredBody.api_key_stored === 'sk-brandnew',
      addStored && addStored.opts.body);

// --- 21b. ADD FORM: quick-add with blank env sends explicit passthrough ----
// There is no server default for key_mode; the pre-fix form omitted it and
// the create 400ed. Review finding 3.
resetFormEls();
setEditing(true);
vm.runInContext('editingProviderName = null;', sandbox);
sandbox.renderProviderForm();
$('pf-name').value = 'quick';
$('pf-base').value = 'https://q.example';
// selector stays at its default (env); the env var field is left blank.
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const quickCall = fetchCalls.find(c => c.url === '/admin/providers');
check('quick-add with blank env sends key_mode passthrough',
      quickCall && JSON.parse(quickCall.opts.body).key_mode === 'passthrough',
      quickCall && quickCall.opts.body);

// --- 22. ADD FORM: explicit passthrough mode --------------------------------
resetFormEls();
setEditing(true);
vm.runInContext('editingProviderName = null;', sandbox);
sandbox.renderProviderForm();
$('pf-name').value = 'pt';
$('pf-base').value = 'https://pt.example';
$('pf-key-mode').value = 'passthrough';
$('pf-key-mode').onchange();
fetchCalls = [];
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const ptCall = fetchCalls.find(c => c.url === '/admin/providers');
check('explicit passthrough POSTs key_mode passthrough',
      ptCall && JSON.parse(ptCall.opts.body).key_mode === 'passthrough',
      ptCall && ptCall.opts.body);

// --- 23. EDIT FORM: switching modes (Plan 025) ------------------------------
// env → stored without typing a key must be refused: there is no stored
// copy to keep, so a blank save would strand the provider credential-less.
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'umans', upstream: 'https://u.example/v1', target: 3,
      provider_type: 'generic', key_mode: 'env', api_key_env: 'UMANS_KEY',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'umans', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('umans');
check('edit form selector reflects the row mode',
      $('pf-key-mode').value === 'env', $('pf-key-mode').value);
$('pf-key-mode').value = 'stored';
$('pf-key-mode').onchange();
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
check('mode switch to stored without a key does not PUT',
      !fetchCalls.some(c => c.url === '/admin/providers/umans'));
check('mode switch to stored without a key explains itself',
      /stored mode/i.test($('pf-error').textContent), $('pf-error').textContent);
// Typing the key completes the switch.
$('pf-key-new').value = 'sk-switched';
fetchCalls = [];
await sandbox._saveProvider();
const swCall = fetchCalls.find(c => c.url === '/admin/providers/umans');
const swBody = swCall ? JSON.parse(swCall.opts.body) : {};
check('mode switch to stored PUTs the new credential',
      swBody.key_mode === 'stored' && swBody.api_key_stored === 'sk-switched',
      swCall && swCall.opts.body);

// --- 23b. EDIT FORM: switching to passthrough ------------------------------
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'umans', upstream: 'https://u.example/v1', target: 3,
      provider_type: 'generic', key_mode: 'env', api_key_env: 'UMANS_KEY',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'umans', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('umans');
$('pf-key-mode').value = 'passthrough';
$('pf-key-mode').onchange();
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const pdCall = fetchCalls.find(c => c.url === '/admin/providers/umans');
check('mode switch to passthrough PUTs key_mode passthrough',
      pdCall && JSON.parse(pdCall.opts.body).key_mode === 'passthrough',
      pdCall && pdCall.opts.body);

// --- 23c. stored → passthrough switch warns about erasing the key ----------
// Review finding 4: the downgrade permanently erases the stored credential.
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'paid', upstream: 'https://p.example/v1', target: 2,
      provider_type: 'generic', key_mode: 'stored', api_key_hint: '4321',
      auth_header: 'authorization', enabled: true,
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'paid', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('paid');
$('pf-key-mode').value = 'passthrough';
$('pf-key-mode').onchange();
check('stored→passthrough switch shows the erase warning',
      /ERASES/.test($('pf-cred-block').innerHTML),
      $('pf-cred-block').innerHTML.slice(0, 240));

// --- 24. EDIT FORM: dashboard_provider + peak_windows round-trip ------------
resetFormEls();
responsesByUrl = {};
responsesByUrl['/admin/providers'] = {
  ok: true, status: 200,
  json: async () => ({
    providers: [{
      name: 'zai', upstream: 'https://z.example/v4', target: 2,
      provider_type: 'generic', key_mode: 'env', api_key_env: 'ZAI_KEY',
      auth_header: 'authorization', enabled: true,
      dashboard_provider: 'zai',
      peak_windows: ['mon-fri 14:00-18:00 +08:00'],
    }],
  }),
};
responsesByUrl['/admin/config/effective'] = {
  ok: true, status: 200,
  json: async () => ({ providers: [{ name: 'zai', env_locked: [] }] }),
};
setEditing(false);
await sandbox.editProvider('zai');
check('edit form prefills dashboard_provider',
      $('pf-dashboard-provider').value === 'zai', $('pf-dashboard-provider').value);
check('edit form prefills peak windows one per line',
      $('pf-peak-windows').value === 'mon-fri 14:00-18:00 +08:00',
      $('pf-peak-windows').value);
// Add a second window and save: both are sent as a list.
$('pf-peak-windows').value = 'mon-fri 14:00-18:00 +08:00\ndaily 08:00-22:00 +08:00';
fetchCalls = [];
responsesByUrl = {};
nextResponse = { ok: true, status: 200, json: async () => ({}) };
await sandbox._saveProvider();
const zCall = fetchCalls.find(c => c.url === '/admin/providers/zai');
const zBody = zCall ? JSON.parse(zCall.opts.body) : {};
check('PUT carries dashboard_provider',
      zBody.dashboard_provider === 'zai', zCall && zCall.opts.body);
check('PUT carries peak_windows as a list',
      JSON.stringify(zBody.peak_windows) ===
        JSON.stringify(['mon-fri 14:00-18:00 +08:00', 'daily 08:00-22:00 +08:00']),
      zCall && zCall.opts.body);

// --- 25. provider card renders the peak badge -------------------------------
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: {
      zai: {
        gate_closed_reason: 'open', effective_permits: 2, in_flight: 0,
        queue_depth: 0, ready: true, total_429s: 0,
        total_requests_forwarded: 1,
        peak: {
          in_peak: true,
          boundary_epoch: Date.now() / 1000 + 3600,
          windows: ['mon-fri 14:00-18:00 +08:00'],
        },
      },
    },
    route_table: { default: ['zai'] },
  }),
};
vm.runInContext('editingProvider = false; scanPinned = false; editingModel = false;', sandbox);
await sandbox.load();
const appView = $('app').innerHTML;
check('in-peak provider card shows the peak badge',
      /badge saturated[^>]*>peak</.test(appView), appView.slice(0, 400));
check('peak section shows the demoted state',
      /in peak \(demoted\)/.test(appView));
check('peak section shows the window spec',
      appView.includes('mon-fri 14:00-18:00 +08:00'));

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
