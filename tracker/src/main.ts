import { DbConnection, tables } from './module_bindings';
import type { AgentAlert, AgentEvent, AgentRun } from './module_bindings/types';

const HOST = 'wss://maincloud.spacetimedb.com';
const DB = 'reframe-g4w65';
const MAX_EVENTS = 80;
const MAX_RUNS = 20;
const ACTIVE = new Set(['thinking', 'removing', 'rebuilding']);
const STAGES = [
  { key: 'thinking', label: 'Reading' },
  { key: 'removing', label: 'Removing' },
  { key: 'rebuilding', label: 'Rebuilding' },
  { key: 'done', label: 'Done' },
];
const KIND_LABEL: Record<string, string> = {
  request: 'Asked',
  scan: 'Read',
  remove: 'Removed',
  change: 'Changed',
  keep: 'Kept',
  reply: 'Replied',
};

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const el = {
  now: $('now'),
  events: $('events'),
  alerts: $('alerts'),
  alertCount: $('alert-count'),
  runs: $('runs'),
  user: $<HTMLSelectElement>('user'),
  link: $('link'),
};

let conn: DbConnection | null = null;
let ready = false;
const fresh = new Set<bigint>();
const params = new URLSearchParams(location.search);
let userFilter = params.get('user') || '';

const ms = (t: { microsSinceUnixEpoch: bigint }) => Number(t.microsSinceUnixEpoch / 1000n);
const clock = (t: { microsSinceUnixEpoch: bigint }) =>
  new Date(ms(t)).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
const pagePath = (url: string) => {
  try {
    const u = new URL(url);
    return u.host + (u.pathname === '/' ? '' : u.pathname);
  } catch {
    return url;
  }
};
const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join('') || '?';

function h(tag: string, attrs: Record<string, string> = {}, ...children: (Node | string | null | false)[]) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const c of children) if (c !== null && c !== false) node.append(c);
  return node;
}

function setLink(state: 'live' | 'connecting' | 'down', text: string) {
  el.link.dataset.state = state;
  el.link.querySelector('span')!.textContent = text;
}

const DEMO_HOSTS = ['reframe-demo.pages.dev', '127.0.0.1:5173', 'localhost:5173'];
const isDemoSite = (host: string) =>
  DEMO_HOSTS.includes(host.toLowerCase()) || host.toLowerCase().endsWith('.reframe-demo.pages.dev');

function data() {
  const db = conn!.db;
  const runs = [...db.agentRun.iter()]
    .filter((r) => isDemoSite(r.site))
    .sort((a, b) => ms(b.startedAt) - ms(a.startedAt));
  const users = new Map<string, string>();
  for (const r of runs) if (!users.has(r.userId)) users.set(r.userId, r.userName);
  const mine = <T extends { userId: string }>(rows: T[]) => (userFilter ? rows.filter((r) => r.userId === userFilter) : rows);
  const runById = new Map(runs.map((r) => [r.runId, r]));
  const onDemo = <T extends { runId: string }>(rows: T[]) => rows.filter((r) => runById.has(r.runId));
  const events = mine(onDemo([...db.agentEvent.iter()])).sort((a, b) => ms(b.at) - ms(a.at) || Number(b.eventId - a.eventId));
  const alerts = mine(onDemo([...db.agentAlert.iter()])).sort((a, b) => ms(b.at) - ms(a.at));
  return { runs: mine(runs), users, runById, events, alerts };
}

function renderUsers(users: Map<string, string>) {
  const current = el.user.value || userFilter;
  el.user.replaceChildren(
    h('option', { value: '' }, 'All users'),
    ...[...users].map(([id, name]) => h('option', { value: id }, name))
  );
  el.user.value = users.has(current) ? current : '';
}

function stageIndex(status: string) {
  if (status === 'done' || status === 'answered') return 3;
  return Math.max(0, STAGES.findIndex((s) => s.key === status));
}

function renderNow(run: AgentRun | undefined) {
  if (!run) {
    el.now.replaceChildren(h('p', { class: 'idle' }, 'Waiting for a request.'));
    el.now.dataset.state = 'idle';
    return;
  }
  const active = ACTIVE.has(run.status);
  const failed = run.status === 'error';
  const at = stageIndex(run.status);
  el.now.dataset.state = active ? 'active' : failed ? 'error' : 'done';
  el.now.replaceChildren(
    h(
      'div',
      { class: 'who-line' },
      h('span', { class: 'avatar', 'aria-hidden': 'true' }, initials(run.userName)),
      h('span', { class: 'name' }, run.userName),
      h(
        'span',
        { class: 'src' },
        run.source === 'auto' ? 'Followed to the next page' : run.source === 'voice' ? 'Spoke a request' : 'Typed a request'
      ),
      h('span', { class: 'page' }, pagePath(run.pageUrl))
    ),
    h('blockquote', { class: 'prompt' }, run.prompt),
    h(
      'ol',
      { class: 'tape', style: `--p:${run.progress}` },
      ...STAGES.map((s, i) =>
        h('li', { class: i < at ? 'past' : i === at ? (active ? 'here' : 'past') : '' }, s.label)
      )
    ),
    h(
      'div',
      { class: 'status-line' },
      h('p', { class: 'step' }, failed ? 'Stopped — page left unchanged' : run.step),
      h(
        'dl',
        { class: 'tally' },
        h('div', { class: 'k-remove' }, h('dt', {}, 'Removed'), h('dd', {}, String(run.removedCount))),
        h('div', { class: 'k-change' }, h('dt', {}, 'Changed'), h('dd', {}, String(run.changedCount))),
        h('div', { class: 'k-keep' }, h('dt', {}, 'Kept'), h('dd', {}, String(run.keptCount)))
      )
    ),
    ...(run.reply && !active ? [h('p', { class: 'reply' }, run.reply)] : [])
  );
}

function renderEvents(events: AgentEvent[], runById: Map<string, AgentRun>) {
  el.events.replaceChildren(
    ...events.slice(0, MAX_EVENTS).map((e) => {
      const run = runById.get(e.runId);
      const target = e.kind === 'remove' ? h('s', {}, e.target) : h('span', {}, e.target);
      return h(
        'li',
        { class: `ev k-${e.kind}${fresh.has(e.eventId) ? ' fresh' : ''}` },
        h('time', {}, clock(e.at)),
        h('b', { class: 'kind' }, KIND_LABEL[e.kind] || e.kind),
        h('p', { class: 'what' }, target, e.detail && e.kind !== 'request' && e.kind !== 'reply' ? h('small', {}, e.detail) : null),
        run && !userFilter ? h('span', { class: 'by' }, run.userName) : null
      );
    })
  );
  fresh.clear();
}

function renderAlerts(alerts: AgentAlert[], runById: Map<string, AgentRun>) {
  el.alertCount.textContent = String(alerts.length);
  el.alertCount.dataset.hot = String(alerts.some((a) => a.severity !== 'info'));
  if (!alerts.length) {
    el.alerts.replaceChildren(h('li', { class: 'empty' }, 'No alerts.'));
    return;
  }
  el.alerts.replaceChildren(
    ...alerts.slice(0, 30).map((a) => {
      const run = runById.get(a.runId);
      return h(
        'li',
        { class: `alert s-${a.severity}${freshAlerts.has(a.alertId) ? ' fresh' : ''}` },
        h('p', { class: 'title' }, a.title),
        a.detail ? h('p', { class: 'detail' }, a.detail) : null,
        h('p', { class: 'meta' }, `${clock(a.at)}${run ? ` · ${run.userName} · ${pagePath(run.pageUrl)}` : ''}`)
      );
    })
  );
  freshAlerts.clear();
}

function renderRuns(runs: AgentRun[]) {
  el.runs.replaceChildren(
    ...runs.slice(0, MAX_RUNS).map((r) =>
      h(
        'li',
        { class: `run st-${r.status}` },
        h('span', { class: 'dot', 'aria-hidden': 'true' }),
        h('p', { class: 'p' }, r.prompt),
        h('p', { class: 'meta' }, `${clock(r.startedAt)} · ${r.userName} · ${pagePath(r.pageUrl)}`),
        h(
          'p',
          { class: 'n', 'aria-label': `${r.removedCount} removed, ${r.changedCount} changed, ${r.keptCount} kept` },
          h('span', { class: 'k-remove' }, String(r.removedCount)),
          h('span', { class: 'k-change' }, String(r.changedCount)),
          h('span', { class: 'k-keep' }, String(r.keptCount))
        )
      )
    )
  );
}

const freshAlerts = new Set<bigint>();
let queued = false;
function render() {
  if (queued || !conn) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    const d = data();
    renderUsers(d.users);
    renderNow(d.runs.find((r) => ACTIVE.has(r.status)) || d.runs[0]);
    renderEvents(d.events, d.runById);
    renderAlerts(d.alerts, d.runById);
    renderRuns(d.runs);
  });
}

el.user.addEventListener('change', () => {
  userFilter = el.user.value;
  const url = new URL(location.href);
  if (userFilter) url.searchParams.set('user', userFilter);
  else url.searchParams.delete('user');
  history.replaceState(null, '', url);
  render();
});

conn = DbConnection.builder()
  .withUri(HOST)
  .withDatabaseName(DB)
  .withAutomaticReconnect()
  .onConnect((c) => {
    setLink('connecting', 'Syncing');
    c.subscriptionBuilder()
      .onApplied(() => {
        ready = true;
        setLink('live', 'Live');
        render();
      })
      .subscribe([tables.agentRun, tables.agentEvent, tables.agentAlert]);
  })
  .onAutomaticReconnect(() => setLink('live', 'Live'))
  .onDisconnect((_ctx, _err, attempt) => setLink('down', attempt === undefined ? 'Offline' : 'Reconnecting'))
  .onConnectError(() => setLink('down', "Can't reach SpacetimeDB"))
  .build();

conn.db.agentRun.onInsert(render);
conn.db.agentRun.onUpdate(render);
conn.db.agentRun.onDelete(render);
conn.db.agentEvent.onInsert((_ctx, row) => {
  if (ready) fresh.add(row.eventId);
  render();
});
conn.db.agentEvent.onDelete(render);
conn.db.agentAlert.onInsert((_ctx, row) => {
  if (ready) freshAlerts.add(row.alertId);
  render();
});
conn.db.agentAlert.onDelete(render);
