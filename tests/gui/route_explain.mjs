// Drive the dashboard's Routing Explain card (Plan 026 W1.6). Run via
// `node tests/gui/route_explain.mjs`, or through pytest —
// tests/test_gui_route_explain.py wraps it and skips when node is absent.
//
// This card exists to answer "who would serve model X right now, and why", so
// the behaviours worth pinning are the ones that would make it lie or vanish:
// the 5 s poll wiping an answer the operator is reading, a server-derived
// provider name rendering as markup, the selected candidate not being
// distinguishable from the rest, and a failed fetch leaving a dead card.
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
// `explainPinned` is a lexical binding inside the vm script, not a sandbox
// property — read it by evaluating in the context or the assertion is vacuous.
const getPinned = () => vm.runInContext('explainPinned', sandbox);
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) });
}

let fetchCalls = [];
let statusResponse = { ok: true, status: 200, json: async () => ({}) };
let planResponse = { ok: true, status: 200, json: async () => ({}) };
const els = {};
const document = { getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; } };
const sandbox = {
  document, console,
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  fetch: async (u, opts) => {
    fetchCalls.push({ url: String(u), opts });
    return String(u).startsWith('/admin/route-plan') ? planResponse : statusResponse;
  },
  URLSearchParams, confirm: () => true, alert: () => {},
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(script, sandbox);

const STATUS = {
  providers: { alpha: {}, beta: {} },
  route_table: { default: ['alpha', 'beta'] },
  model_map: {
    'glm-5.2': { alpha: 'glm-5.2', beta: 'glm-4.6' },
    'kimi-k2': { alpha: 'kimi-k2' },
  },
};

const PLAN = {
  model: 'glm-5.2',
  keyed_route: false,
  strategy: 'pace',
  candidates: ['alpha', 'beta', 'gamma'],
  quarantined: ['delta'],
  reason: 'pace_failover',
  immediate: ['beta', 'alpha'],
  queue_candidate: 'alpha',
  terminal_fallback: 'alpha',
  assessments: [
    { provider: 'beta', tier: 'immediate', signals: [], score: 0.812,
      rank: 0, availability: 'available', freshness: 'fresh' },
    { provider: 'alpha', tier: 'immediate', signals: [], score: -0.1,
      rank: 1, availability: 'available', freshness: 'fresh' },
    { provider: 'busybee', tier: 'queue', signals: ['busy', 'in_peak'],
      score: null, rank: 0, availability: 'busy', freshness: 'fresh' },
    { provider: 'stale', tier: 'backstop', signals: ['degraded'],
      score: null, rank: 0, availability: 'available', freshness: 'degraded' },
  ],
  excluded: [{ provider: 'gamma', why: 'closed' }],
};

// --- 1. the card renders, with a datalist from the model map ---------------
sandbox.renderRoutingExplain(STATUS);
let view = $('routing-explain').innerHTML;
check('renders the card', view.includes('Routing Explain'), view.slice(0, 120));
check('has a model input', view.includes('id="explain-model"'));
check('has an Explain button', view.includes('id="explain-btn"'));
check('feeds the datalist from the model map',
      view.includes('value="glm-5.2"') && view.includes('value="kimi-k2"'),
      view.slice(0, 400));
check('says it is read-only', /never\s+moves a pin/.test(view), view.slice(0, 400));

// A dashboard with no model map (unauthenticated, or none configured) renders
// the card anyway — the input is free text, the datalist is just a convenience.
sandbox.renderRoutingExplain({ providers: {} });
check('renders without a model map',
      $('routing-explain').innerHTML.includes('id="explain-model"'));

// --- 2. Explain fetches the endpoint with the typed model -----------------
sandbox.renderRoutingExplain(STATUS);
fetchCalls = [];
planResponse = { ok: true, status: 200, json: async () => PLAN };
$('explain-model').value = ' glm-5.2 ';
await $('explain-btn').onclick();
const call = fetchCalls.find(c => c.url.startsWith('/admin/route-plan'));
check('GETs /admin/route-plan', !!call, JSON.stringify(fetchCalls));
check('passes the trimmed model', call?.url === '/admin/route-plan?model=glm-5.2',
      call?.url);
check('sends same-origin credentials', call?.opts?.credentials === 'same-origin');
check('does not send a raw API key', !/[?&]key=/.test(call?.url || ''), call?.url);

// --- 3. the assessment table ---------------------------------------------
let out = $('explain-results').innerHTML;
check('renders the reason', out.includes('pace_failover'), out.slice(0, 200));
check('renders the immediate order', out.includes('beta → alpha'), out.slice(0, 400));
check('renders the queue candidate and terminal fallback',
      out.includes('queue candidate') && out.includes('terminal fallback'));
check('has a table header with the five columns',
      out.includes('<th>provider</th>') && out.includes('<th>tier</th>')
      && out.includes('<th>signals</th>') && out.includes('<th>score</th>')
      && out.includes('<th>order</th>'));
check('rows are in tier order (immediate, queue, backstop)',
      out.indexOf('beta') < out.indexOf('busybee')
      && out.indexOf('busybee') < out.indexOf('stale'), out.slice(0, 600));
check('highlights the selected first immediate',
      /<tr style="font-weight:600"><td>beta &larr; selected/.test(out),
      out.slice(out.indexOf('<tr'), out.indexOf('<tr') + 200));
check('does not mark a non-selected candidate',
      !out.includes('alpha &larr; selected'));
check('renders each fired signal', out.includes('<code>busy</code>')
      && out.includes('<code>in_peak</code>') && out.includes('<code>degraded</code>'));
check('renders the score to three places', out.includes('0.812'), out.slice(0, 800));
check('renders a null score as a dash rather than 0',
      !/>0\.000</.test(out), out.slice(0, 800));
check('shows the 1-based order column',
      /<td>2<\/td>/.test(out), out.slice(0, 800));
check('tiers carry a badge class',
      out.includes('badge open') && out.includes('badge saturated')
      && out.includes('badge unknown'), out.slice(0, 800));
check('reports filter-stage exclusions with the reason',
      out.includes('gamma') && out.includes('closed'), out.slice(-500));
check('warns about quarantined pairs',
      /quarantined for this model: delta/.test(out), out.slice(-300));
check('omits the model-preference row when the model has none',
      !out.includes('model preference'), out.slice(0, 400));

// --- 3b. a per-model preference is shown when set (Plan 026 W3) -----------
planResponse = {
  ok: true, status: 200,
  json: async () => ({ ...PLAN, preference: ['beta', 'alpha'] }),
};
vm.runInContext('explainPinned = false', sandbox);
sandbox.renderRoutingExplain(STATUS);
$('explain-model').value = 'glm-5.2';
await $('explain-btn').onclick();
const prefOut = $('explain-results').innerHTML;
check('renders the model preference order',
      /model preference/.test(prefOut) && prefOut.includes('beta → alpha'),
      prefOut.slice(0, 600));
// Restore the unpreferred plan for the freeze-flag checks below.
planResponse = { ok: true, status: 200, json: async () => PLAN };
vm.runInContext('explainPinned = false', sandbox);
sandbox.renderRoutingExplain(STATUS);
$('explain-model').value = ' glm-5.2 ';
await $('explain-btn').onclick();
out = $('explain-results').innerHTML;

// --- 4. a poll landing while results are shown must not wipe them ---------
check('sets the freeze flag', getPinned() === true, getPinned());
const before = $('routing-explain').innerHTML;
sandbox.renderRoutingExplain(STATUS);
check('poll does not clobber the results',
      $('routing-explain').innerHTML === before);

// Dismiss releases the freeze and reloads.
$('explain-dismiss').onclick();
check('dismiss clears the freeze flag', getPinned() === false);

// --- 5. blank model explains the default route unfiltered ----------------
sandbox.renderRoutingExplain(STATUS);
fetchCalls = [];
$('explain-model').value = '';
await $('explain-btn').onclick();
const bare = fetchCalls.find(c => c.url.startsWith('/admin/route-plan'));
check('omits the model param when blank', bare?.url === '/admin/route-plan', bare?.url);

// --- 6. server-derived strings are escaped -------------------------------
$('explain-dismiss').onclick();
sandbox.renderRoutingExplain(STATUS);
planResponse = {
  ok: true, status: 200,
  json: async () => ({
    reason: '<img src=x onerror=1>',
    strategy: 'ordered',
    immediate: ['<script>bad</script>'],
    queue_candidate: null,
    terminal_fallback: '<b>t</b>',
    assessments: [{
      provider: '<script>bad</script>', tier: '<i>immediate</i>',
      signals: ['<svg onload=1>'], score: null, rank: 0,
    }],
    excluded: [{ provider: '<u>g</u>', why: '<em>closed</em>' }],
    quarantined: ['<hr>'],
  }),
};
await $('explain-btn').onclick();
out = $('explain-results').innerHTML;
check('escapes provider names', !out.includes('<script>bad'), out.slice(0, 300));
check('escapes the reason', !out.includes('<img src=x'), out.slice(0, 300));
check('escapes signal names', !out.includes('<svg onload'), out.slice(0, 400));
check('escapes the exclusion rows', !out.includes('<u>g</u>'), out.slice(-300));
check('escapes the quarantine list', !out.includes('<hr>'), out.slice(-300));
check('escaped output still shows the text', out.includes('&lt;'), out.slice(0, 200));

// A card with nothing to show says so rather than rendering an empty table.
$('explain-dismiss').onclick();
sandbox.renderRoutingExplain(STATUS);
planResponse = {
  ok: true, status: 200,
  json: async () => ({
    reason: 'model_unservable', strategy: 'ordered', immediate: [],
    queue_candidate: null, terminal_fallback: 'alpha', assessments: [],
    excluded: [], quarantined: [],
  }),
};
await $('explain-btn').onclick();
out = $('explain-results').innerHTML;
check('explains an empty plan in words',
      /every one was filtered out/.test(out), out.slice(0, 400));
check('still reports the reason on an empty plan',
      out.includes('model_unservable'), out.slice(0, 200));

// --- 7. failures surface, and stay dismissible ---------------------------
$('explain-dismiss').onclick();
sandbox.renderRoutingExplain(STATUS);
planResponse = { ok: false, status: 401, json: async () => ({ error: 'unauthorized' }) };
await $('explain-btn').onclick();
out = $('explain-results').innerHTML;
check('shows the server error text', out.includes('unauthorized'), out.slice(0, 200));
check('an error is still dismissible', out.includes('explain-dismiss'), out.slice(-200));

$('explain-dismiss').onclick();
sandbox.renderRoutingExplain(STATUS);
planResponse = { ok: false, status: 500, json: async () => { throw new Error('no body'); } };
await $('explain-btn').onclick();
check('falls back to the status code with no error body',
      $('explain-results').innerHTML.includes('500'),
      $('explain-results').innerHTML.slice(0, 200));

$('explain-dismiss').onclick();
sandbox.renderRoutingExplain(STATUS);
const goodFetch = sandbox.fetch;
sandbox.fetch = async () => { throw new Error('network down'); };
await $('explain-btn').onclick();
check('surfaces a transport failure',
      $('explain-results').innerHTML.includes('network down'),
      $('explain-results').innerHTML.slice(0, 200));
sandbox.fetch = goodFetch;

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
