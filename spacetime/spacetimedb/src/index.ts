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

const spacetimedb = schema({ admin, appUser });
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
