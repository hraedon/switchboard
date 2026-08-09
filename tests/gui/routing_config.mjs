// Drive the dashboard's routing-strategy editor with the same minimal DOM
// shim the default-route harness uses (Plan 020 WI-14). Run via
// `node tests/gui/routing_config.mjs`, or through pytest —
// tests/test_gui_routing_config.py wraps it and skips when node is absent.
//
// This control changes how every request is routed, from a browser, live. It
// is the highest-consequence writing control on the dashboard, so the things
// worth pinning are: does a poll landing mid-edit discard the operator's
// typing, does a rejected value surface the server's reason rather than a bare
// status, and does the pace panel only appear when pace is actually selected.
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
// `let editingRouting` is a lexical binding inside the vm script, NOT a
// property of the sandbox object — read it by evaluating in the context or
// the assertion is vacuous.
const getEditing = () => vm.runInContext('editingRouting', sandbox);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail });
}

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
  setTimeout: () => 0,          // never auto-fire; assert explicitly
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    return nextResponse;
  },
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;

nextResponse = {
  ok: true, status: 200,
  json: async () => ({ providers: {}, route_table: {} }),
};

vm.createContext(sandbox);
vm.runInContext(script, sandbox);

const ORDERED = {
  routing_config: {
    strategy: 'ordered', dwell_interval: 30, failback_delay: 0,
    pace_burn_rate_per_day: 0.14, pace_flap_margin: 0.05,
  },
  providers: {},
};

// --- 1. read-only render ---------------------------------------------------
sandbox.renderRoutingConfigEditor(ORDERED);
let view = $('routing-config-editor').innerHTML;
check('renders the current strategy', view.includes('ordered'), view.slice(0, 160));
check('has an Edit button', view.includes('id="edit-routing"'));
check('hides pace knobs when strategy is not pace',
      !view.includes('burn_rate_per_day'), view.slice(0, 300));

// A missing routing_config (unauthenticated dashboard) renders nothing rather
// than throwing and taking the rest of the page down with it.
sandbox.renderRoutingConfigEditor({ providers: {} });
check('missing routing_config renders empty',
      $('routing-config-editor').innerHTML === '');

// --- 2. pace surfaces its knobs and the surplus ranking --------------------
const now = Date.now() / 1000;
const PACE = {
  routing_config: {
    strategy: 'pace', dwell_interval: 30, failback_delay: 0,
    pace_burn_rate_per_day: 0.14, pace_flap_margin: 0.05,
  },
  providers: {
    rich:   { weekly_remaining_fraction: 0.90, weekly_reset_epoch: now + 86400, stale: false },
    poor:   { weekly_remaining_fraction: 0.10, weekly_reset_epoch: now + 86400, stale: false },
    stale:  { weekly_remaining_fraction: 0.99, weekly_reset_epoch: now + 86400, stale: true },
    silent: {},
  },
};
sandbox.renderRoutingConfigEditor(PACE);
view = $('routing-config-editor').innerHTML;
check('shows pace knobs when pace is selected',
      view.includes('burn_rate_per_day') && view.includes('flap_margin'));
check('ranks the higher-surplus provider first',
      view.indexOf('rich') < view.indexOf('poor'), view.slice(0, 400));
// A stale reading must never be scored — that is the routing core's fail-safe,
// and a dashboard that scored it would contradict where traffic actually goes.
check('lists a stale provider as unscored',
      /Unscored/.test(view) && view.indexOf('unscored') > -1);
check('does not score a stale provider',
      view.indexOf('stale') > view.indexOf('Unscored'), view.slice(-400));
check('lists a provider with no weekly signal as unscored',
      view.includes('silent'));

// --- 3. entering edit mode -------------------------------------------------
sandbox.renderRoutingConfigEditor(PACE);
$('edit-routing').onclick();
check('edit sets the freeze flag', getEditing() === true, getEditing());
const form = $('routing-config-editor').innerHTML;
check('strategy select is prefilled with the live value',
      /value="pace" selected/.test(form), form.match(/value="pace"[^>]*/)?.[0]);
check('burn rate prefilled from the live config', form.includes('value="0.14"'));
check('flap margin prefilled from the live config', form.includes('value="0.05"'));
check('offers all three strategies',
      form.includes('value="ordered"') && form.includes('value="headroom"')
      && form.includes('value="pace"'));
check('tells the operator the change persists',
      /survive a restart/.test(form), form.slice(-400));

// --- 4. a poll landing mid-edit must NOT clobber the form ------------------
const before = $('routing-config-editor').innerHTML;
sandbox.renderRoutingConfigEditor(ORDERED);
check('poll does not clobber an open form',
      $('routing-config-editor').innerHTML === before);

// --- 5. saving sends the right payload ------------------------------------
fetchCalls = [];
nextResponse = {
  ok: true, status: 200,
  json: async () => ({ strategy: 'headroom', persisted: true }),
};
$('rc-strategy').value = 'headroom';
$('rc-burn-rate').value = '0.2';
$('rc-flap-margin').value = '0.03';
$('rc-dwell').value = '45';
$('rc-failback').value = '10';
await $('save-routing').onclick();
const call = fetchCalls.find(c => c.url === '/admin/config/routing');
check('PUTs to /admin/config/routing', !!call);
check('uses PUT', call?.opts?.method === 'PUT');
check('sends same-origin credentials', call?.opts?.credentials === 'same-origin');
check('sets JSON content-type',
      call?.opts?.headers?.['content-type'] === 'application/json');
const sent = call ? JSON.parse(call.opts.body) : {};
check('sends the selected strategy', sent.strategy === 'headroom', JSON.stringify(sent));
check('sends numbers, not strings',
      typeof sent.pace_burn_rate_per_day === 'number'
      && typeof sent.dwell_interval === 'number', JSON.stringify(sent));
check('sends the edited values',
      sent.pace_burn_rate_per_day === 0.2 && sent.pace_flap_margin === 0.03
      && sent.dwell_interval === 45 && sent.failback_delay === 10,
      JSON.stringify(sent));
check('clears the freeze flag after a successful save', getEditing() === false);

// A blank numeric field must be omitted rather than sent as NaN — JSON.stringify
// turns NaN into null, which the server rejects as "must be a number", so the
// operator would see an error for a field they deliberately left alone.
sandbox.renderRoutingConfigEditor(PACE);
$('edit-routing').onclick();
fetchCalls = [];
$('rc-burn-rate').value = '';
await $('save-routing').onclick();
const blankCall = fetchCalls.find(c => c.url === '/admin/config/routing');
check('omits a blank numeric field',
      blankCall && !('pace_burn_rate_per_day' in JSON.parse(blankCall.opts.body)),
      blankCall?.opts?.body);

// --- 6. a rejected value surfaces the server's reason ----------------------
sandbox.renderRoutingConfigEditor(PACE);
$('edit-routing').onclick();
fetchCalls = [];
nextResponse = {
  ok: false, status: 400,
  json: async () => ({ error: 'pace_flap_margin must be in [0.0, 1.0)' }),
};
$('rc-flap-margin').value = '1.5';
await $('save-routing').onclick();
check('shows the server error text',
      $('rc-error').textContent.includes('pace_flap_margin'),
      $('rc-error').textContent);
check('keeps the form open after a rejection', getEditing() === true);

// A failure with no JSON body must still say something useful.
nextResponse = { ok: false, status: 500, json: async () => { throw new Error('no body'); } };
await $('save-routing').onclick();
check('falls back to the status code when there is no error body',
      $('rc-error').textContent.includes('500'), $('rc-error').textContent);

// A transport failure must not leave the operator staring at a dead button.
sandbox.fetch = async () => { throw new Error('network down'); };
await $('save-routing').onclick();
check('surfaces a transport failure',
      $('rc-error').textContent.includes('network down'),
      $('rc-error').textContent);
sandbox.fetch = async (url, opts) => { fetchCalls.push({ url, opts }); return nextResponse; };

// --- 7. cancel abandons the edit ------------------------------------------
sandbox.renderRoutingConfigEditor(PACE);
$('edit-routing').onclick();
$('cancel-routing').onclick();
check('cancel clears the freeze flag', getEditing() === false);

// --- 8. provider names are escaped ----------------------------------------
sandbox.renderRoutingConfigEditor({
  routing_config: { strategy: 'pace', pace_burn_rate_per_day: 0.14 },
  providers: {
    '<script>bad</script>': {
      weekly_remaining_fraction: 0.5, weekly_reset_epoch: now + 86400, stale: false,
    },
  },
});
const xss = $('routing-config-editor').innerHTML;
check('escapes provider names in the surplus ranking',
      !xss.includes('<script>bad') && xss.includes('&lt;'), xss.slice(0, 200));

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
