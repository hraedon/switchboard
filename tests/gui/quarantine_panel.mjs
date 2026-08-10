// Drive the dashboard's quarantine panel (Plan 023 WI-4). Run via
// `node tests/gui/quarantine_panel.mjs`, or through pytest —
// tests/test_gui_quarantine_panel.py wraps it and skips when node is absent.
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
let deleteResponse = { ok: true, status: 200, json: async () => ({ released: {} }) };
const els = {};
const document = { getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; } };
const sandbox = {
  document, console,
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    const u = String(url);
    if (u.startsWith('/admin/quarantine/') && opts && opts.method === 'DELETE') {
      return deleteResponse;
    }
    return nextResponse;
  },
  URLSearchParams, confirm: () => true, alert: () => {},
  window: { location: { href: '' } },
};
sandbox.globalThis = sandbox;

// Seed status.json with a quarantined pair: alpha/shared-model, 5 failures,
// last status 500, detail "application/json".
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { alpha: {}, beta: {} },
    route_table: { default: ['alpha', 'beta'] },
    model_map: { 'shared-model': { alpha: 'shared-model', beta: 'shared-model' } },
    quarantine: {
      threshold: 5,
      entries: [
        {
          provider: 'alpha',
          model: 'shared-model',
          failures: 5,
          first_failure_at: 1000.0,
          last_failure_at: 1005.0,
          last_status: 500,
          last_detail: 'application/json',
        },
      ],
      counters: {},
    },
    routing_metrics: { forwarded_per_provider: {} },
  }),
};

vm.createContext(sandbox);
vm.runInContext(script, sandbox);

// --- 1. Quarantine section renders with the entry --------------------------
await sandbox.load();
const appHtml = $('app').innerHTML;
check('quarantine heading renders', /<h2>Quarantine<\/h2>/.test(appHtml));
check('quarantined provider name renders', appHtml.includes('alpha'));
check('quarantined model name renders', appHtml.includes('shared-model'));
check('failure count renders', /5/.test(appHtml));
check('last status renders', appHtml.includes('500'));
check('detail renders', appHtml.includes('application/json'));
check('release button renders', /Release/.test(appHtml));

// --- 2. Release button is wired and calls DELETE ---------------------------
const releaseBtn = $('rel-q-0');
check('release button exists', !!releaseBtn && typeof releaseBtn.onclick === 'function');
fetchCalls = [];
if (releaseBtn) await releaseBtn.onclick();
const delCall = fetchCalls.find(
  c => c.url.startsWith('/admin/quarantine/') && c.opts && c.opts.method === 'DELETE',
);
check('release calls DELETE /admin/quarantine/<provider>/<model>', !!delCall);
check('release URL encodes provider and model',
      delCall && delCall.url === '/admin/quarantine/alpha/shared-model',
      delCall && delCall.url);
check('release sends same-origin credentials',
      delCall && delCall.opts.credentials === 'same-origin');

// --- 3. Release failure surfaces an alert, no crash ------------------------
deleteResponse = { ok: false, status: 404, json: async () => ({ error: 'not quarantined' }) };
fetchCalls = [];
if (releaseBtn) await releaseBtn.onclick();
check('release failure does not crash', true);

// --- 4. No quarantine section when there are no entries --------------------
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { alpha: {}, beta: {} },
    route_table: { default: ['alpha'] },
    model_map: {},
    quarantine: { threshold: 5, entries: [], counters: {} },
    routing_metrics: { forwarded_per_provider: {} },
  }),
};
// Reset DOM elements so the new render is clean.
for (const k of Object.keys(els)) delete els[k];
await sandbox.load();
check('no quarantine heading when entries empty',
      !/<h2>Quarantine<\/h2>/.test($('app').innerHTML));

// --- 5. No quarantine section when quarantine is absent from payload -------
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { alpha: {} },
    route_table: { default: ['alpha'] },
    model_map: {},
    routing_metrics: { forwarded_per_provider: {} },
  }),
};
for (const k of Object.keys(els)) delete els[k];
await sandbox.load();
check('no quarantine heading when payload lacks quarantine',
      !/<h2>Quarantine<\/h2>/.test($('app').innerHTML));

// --- 6. Multiple entries each get their own release button -----------------
nextResponse = {
  ok: true, status: 200,
  json: async () => ({
    providers: { alpha: {}, beta: {} },
    route_table: { default: ['alpha', 'beta'] },
    model_map: {},
    quarantine: {
      threshold: 5,
      entries: [
        { provider: 'alpha', model: 'm1', failures: 5, first_failure_at: 1, last_failure_at: 2, last_status: 500, last_detail: '' },
        { provider: 'beta', model: 'm2', failures: 5, first_failure_at: 1, last_failure_at: 2, last_status: null, last_detail: 'transport: ConnectError' },
      ],
      counters: {},
    },
    routing_metrics: { forwarded_per_provider: {} },
  }),
};
deleteResponse = { ok: true, status: 200, json: async () => ({ released: {} }) };
for (const k of Object.keys(els)) delete els[k];
await sandbox.load();
check('two entries render two release buttons',
      !!$('rel-q-0') && !!$('rel-q-1'));
check('null status renders a dash', $('app').innerHTML.includes('&mdash;'));
check('transport detail renders', $('app').innerHTML.includes('transport: ConnectError'));

// Release the second entry; it must DELETE beta/m2, not alpha/m1.
fetchCalls = [];
await $('rel-q-1').onclick();
const del2 = fetchCalls.find(
  c => c.url.startsWith('/admin/quarantine/') && c.opts && c.opts.method === 'DELETE',
);
check('second release button deletes the correct pair',
      del2 && del2.url === '/admin/quarantine/beta/m2',
      del2 && del2.url);

// --- report ---------------------------------------------------------------
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : '   << ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);