import { schema, table, t, SenderError, type InferSchema, type ReducerCtx } from 'spacetimedb/server';

const admin = table(
  { name: 'admin' },
  {
    identity: t.identity().primaryKey(),
  }
);

const appUser = table(
  { name: 'app_user' },
  {
    userId: t.string().primaryKey(),
    email: t.string().unique(),
    displayName: t.string(),
    passwordHash: t.string(),
    prefsJson: t.string(),
    trackingPaused: t.bool(),
    createdAt: t.timestamp(),
  }
);

// Live agent tracker. Public so the tracker page can subscribe; holds labels only, never field values.
const agentRun = table(
  { name: 'agent_run', public: true },
  {
    runId: t.string().primaryKey(),
    userId: t.string().index('btree'),
    userName: t.string(),
    site: t.string(),
    pageUrl: t.string(),
    prompt: t.string(),
    source: t.string(),
    status: t.string(),
    step: t.string(),
    progress: t.u8(),
    removedCount: t.u32(),
    changedCount: t.u32(),
    keptCount: t.u32(),
    reply: t.string(),
    startedAt: t.timestamp(),
    updatedAt: t.timestamp(),
  }
);

const agentEvent = table(
  { name: 'agent_event', public: true },
  {
    eventId: t.u64().primaryKey().autoInc(),
    runId: t.string().index('btree'),
    userId: t.string(),
    kind: t.string(),
    target: t.string(),
    detail: t.string(),
    at: t.timestamp(),
  }
);

const agentAlert = table(
  { name: 'agent_alert', public: true },
  {
    alertId: t.u64().primaryKey().autoInc(),
    runId: t.string().index('btree'),
    userId: t.string(),
    severity: t.string(),
    title: t.string(),
    detail: t.string(),
    at: t.timestamp(),
  }
);

// Caretaker records. Public so a caretaker deployed under any identity can read them.
const patient = table(
  { name: 'patient', public: true },
  {
    patientId: t.string().primaryKey(),
    fullName: t.string().index('btree'),
    dateOfBirth: t.string(),
    city: t.string(),
    conditions: t.string(),
    medications: t.string(),
    securityQuestion: t.string(),
  }
);

const caretakerNote = table(
  { name: 'caretaker_note', public: true },
  {
    noteId: t.u64().primaryKey().autoInc(),
    patientId: t.string().index('btree'),
    author: t.string(),
    body: t.string(),
    createdAt: t.timestamp(),
  }
);

const spacetimedb = schema({ admin, appUser, agentRun, agentEvent, agentAlert, patient, caretakerNote });
export default spacetimedb;

type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;

function requireAdmin(ctx: Ctx) {
  if (!ctx.db.admin.identity.find(ctx.sender)) {
    throw new SenderError('unauthorized');
  }
}

export const init = spacetimedb.init(ctx => {
  ctx.db.admin.insert({ identity: ctx.sender });
});

export const createUser = spacetimedb.reducer(
  {
    userId: t.string(),
    email: t.string(),
    displayName: t.string(),
    passwordHash: t.string(),
  },
  (ctx, { userId, email, displayName, passwordHash }) => {
    requireAdmin(ctx);
    if (ctx.db.appUser.email.find(email)) {
      throw new SenderError('email_taken');
    }
    ctx.db.appUser.insert({
      userId,
      email,
      displayName,
      passwordHash,
      prefsJson: '{}',
      trackingPaused: false,
      createdAt: ctx.timestamp,
    });
  }
);

export const updatePasswordHash = spacetimedb.reducer(
  { userId: t.string(), passwordHash: t.string() },
  (ctx, { userId, passwordHash }) => {
    requireAdmin(ctx);
    const row = ctx.db.appUser.userId.find(userId);
    if (!row) throw new SenderError('not_found');
    ctx.db.appUser.userId.update({ ...row, passwordHash });
  }
);

export const updatePrefs = spacetimedb.reducer(
  { userId: t.string(), prefsJson: t.string() },
  (ctx, { userId, prefsJson }) => {
    requireAdmin(ctx);
    const row = ctx.db.appUser.userId.find(userId);
    if (!row) throw new SenderError('not_found');
    ctx.db.appUser.userId.update({ ...row, prefsJson });
  }
);

const MAX_RUNS_PER_USER = 40;
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n) : s);

function pruneRuns(ctx: Ctx, userId: string) {
  const runs = [...ctx.db.agentRun.userId.filter(userId)].sort((a, b) =>
    a.startedAt.microsSinceUnixEpoch < b.startedAt.microsSinceUnixEpoch ? -1 : 1
  );
  for (const run of runs.slice(0, Math.max(0, runs.length - MAX_RUNS_PER_USER + 1))) {
    for (const e of [...ctx.db.agentEvent.runId.filter(run.runId)]) ctx.db.agentEvent.eventId.delete(e.eventId);
    for (const a of [...ctx.db.agentAlert.runId.filter(run.runId)]) ctx.db.agentAlert.alertId.delete(a.alertId);
    ctx.db.agentRun.runId.delete(run.runId);
  }
}

function findRun(ctx: Ctx, runId: string) {
  const run = ctx.db.agentRun.runId.find(runId);
  if (!run) throw new SenderError('run_not_found');
  return run;
}

export const startRun = spacetimedb.reducer(
  {
    runId: t.string(),
    userId: t.string(),
    userName: t.string(),
    site: t.string(),
    pageUrl: t.string(),
    prompt: t.string(),
    source: t.string(),
  },
  (ctx, { runId, userId, userName, site, pageUrl, prompt, source }) => {
    requireAdmin(ctx);
    pruneRuns(ctx, userId);
    ctx.db.agentRun.insert({
      runId,
      userId,
      userName: clip(userName, 80),
      site: clip(site, 120),
      pageUrl: clip(pageUrl, 300),
      prompt: clip(prompt, 500),
      source,
      status: 'thinking',
      step: 'Reading the page',
      progress: 0,
      removedCount: 0,
      changedCount: 0,
      keptCount: 0,
      reply: '',
      startedAt: ctx.timestamp,
      updatedAt: ctx.timestamp,
    });
    ctx.db.agentEvent.insert({ eventId: 0n, runId, userId, kind: 'request', target: clip(prompt, 200), detail: source, at: ctx.timestamp });
  }
);

export const logEvent = spacetimedb.reducer(
  { runId: t.string(), kind: t.string(), target: t.string(), detail: t.string(), step: t.string(), progress: t.u8() },
  (ctx, { runId, kind, target, detail, step, progress }) => {
    requireAdmin(ctx);
    const run = findRun(ctx, runId);
    ctx.db.agentEvent.insert({
      eventId: 0n,
      runId,
      userId: run.userId,
      kind,
      target: clip(target, 200),
      detail: clip(detail, 300),
      at: ctx.timestamp,
    });
    ctx.db.agentRun.runId.update({
      ...run,
      status: kind === 'remove' ? 'removing' : kind === 'change' || kind === 'keep' ? 'rebuilding' : run.status,
      step: step || run.step,
      progress: Math.max(run.progress, Math.min(progress, 100)),
      removedCount: run.removedCount + (kind === 'remove' ? 1 : 0),
      changedCount: run.changedCount + (kind === 'change' ? 1 : 0),
      keptCount: run.keptCount + (kind === 'keep' ? 1 : 0),
      updatedAt: ctx.timestamp,
    });
  }
);

export const raiseAlert = spacetimedb.reducer(
  { runId: t.string(), severity: t.string(), title: t.string(), detail: t.string() },
  (ctx, { runId, severity, title, detail }) => {
    requireAdmin(ctx);
    const run = findRun(ctx, runId);
    ctx.db.agentAlert.insert({
      alertId: 0n,
      runId,
      userId: run.userId,
      severity,
      title: clip(title, 120),
      detail: clip(detail, 300),
      at: ctx.timestamp,
    });
  }
);

export const finishRun = spacetimedb.reducer(
  { runId: t.string(), status: t.string(), reply: t.string() },
  (ctx, { runId, status, reply }) => {
    requireAdmin(ctx);
    const run = findRun(ctx, runId);
    ctx.db.agentRun.runId.update({
      ...run,
      status,
      step: status === 'done' ? 'Done' : 'Stopped',
      progress: status === 'done' ? 100 : run.progress,
      reply: clip(reply, 500),
      updatedAt: ctx.timestamp,
    });
    ctx.db.agentEvent.insert({ eventId: 0n, runId, userId: run.userId, kind: 'reply', target: clip(reply, 200), detail: status, at: ctx.timestamp });
  }
);

export const clearRuns = spacetimedb.reducer({ userId: t.string() }, (ctx, { userId }) => {
  requireAdmin(ctx);
  for (const run of [...ctx.db.agentRun.userId.filter(userId)]) {
    for (const e of [...ctx.db.agentEvent.runId.filter(run.runId)]) ctx.db.agentEvent.eventId.delete(e.eventId);
    for (const a of [...ctx.db.agentAlert.runId.filter(run.runId)]) ctx.db.agentAlert.alertId.delete(a.alertId);
    ctx.db.agentRun.runId.delete(run.runId);
  }
});

export const deleteUser = spacetimedb.reducer(
  { userId: t.string() },
  (ctx, { userId }) => {
    requireAdmin(ctx);
    if (!ctx.db.appUser.userId.delete(userId)) throw new SenderError('not_found');
  }
);

export const seedPatient = spacetimedb.reducer(
  {
    patientId: t.string(),
    fullName: t.string(),
    dateOfBirth: t.string(),
    city: t.string(),
    conditions: t.string(),
    medications: t.string(),
    securityQuestion: t.string(),
  },
  (ctx, { patientId, fullName, dateOfBirth, city, conditions, medications, securityQuestion }) => {
    requireAdmin(ctx);
    if (ctx.db.patient.patientId.find(patientId)) return;
    ctx.db.patient.insert({
      patientId,
      fullName: clip(fullName, 120),
      dateOfBirth: clip(dateOfBirth, 40),
      city: clip(city, 80),
      conditions: clip(conditions, 500),
      medications: clip(medications, 500),
      securityQuestion: clip(securityQuestion, 300),
    });
  }
);

export const addCaretakerNote = spacetimedb.reducer(
  { patientId: t.string(), author: t.string(), body: t.string() },
  (ctx, { patientId, author, body }) => {
    if (!ctx.db.patient.patientId.find(patientId)) throw new SenderError('not_found');
    const text = body.trim();
    if (!text) throw new SenderError('empty');
    ctx.db.caretakerNote.insert({
      noteId: 0n,
      patientId,
      author: clip(author, 80),
      body: clip(text, 2000),
      createdAt: ctx.timestamp,
    });
  }
);
