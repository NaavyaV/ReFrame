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

const spacetimedb = schema({ admin, appUser, patient, caretakerNote });
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

export const deleteUser = spacetimedb.reducer(
  { userId: t.string() },
  (ctx, { userId }) => {
    requireAdmin(ctx);
    if (!ctx.db.appUser.userId.delete(userId)) throw new SenderError('not_found');
  }
);

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n) : s);

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
