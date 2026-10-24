import { ApiError, apiFetch, clearAuth, getAuth, setAuth } from './shared/api.js';

const CONTENT_FILES = ['content/snapshot.js', 'content/apply.js', 'content/widget.js', 'content/main.js'];
const MAX_CHAT = 60;
const MAX_CACHE = 60;

// ---------- per-tab session state ----------

const keys = (tabId) => ({ chat: `chat:${tabId}`, goal: `goal:${tabId}`, busy: `busy:${tabId}`, open: `open:${tabId}` });
const busyTabs = new Set();
const pendingRebuild = new Set();
const debounceTimers = new Map();
const renderCache = new Map();

async function getSession(key) {
  return (await chrome.storage.session.get(key))[key];
}

async function tabState(tabId) {
  const k = keys(tabId);
  const data = await chrome.storage.session.get([k.chat, k.busy, k.open]);
  const auth = await getAuth();
  return { chat: data[k.chat] || [], busy: Boolean(data[k.busy]), open: Boolean(data[k.open]), user: auth?.user || null };
}

async function broadcast(tabId) {
  const state = await tabState(tabId);
  chrome.tabs.sendMessage(tabId, { type: 'rf:state', state }).catch(() => {});
}

async function pushChat(tabId, entry) {
  const key = keys(tabId).chat;
  const list = (await getSession(key)) || [];
  list.push({ ...entry, ts: Date.now() });
  await chrome.storage.session.set({ [key]: list.slice(-MAX_CHAT) });
  await broadcast(tabId);
}

async function chatHistory(tabId) {
  const list = (await getSession(keys(tabId).chat)) || [];
  return list
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .slice(-6)
    .map(({ role, content }) => ({ role, content: String(content).slice(0, 2000) }));
}

async function setBusy(tabId, busy) {
  if (busy) busyTabs.add(tabId);
  else busyTabs.delete(tabId);
  await chrome.storage.session.set({ [keys(tabId).busy]: busy });
  await broadcast(tabId);
}

// ---------- content script bridge ----------

async function sendToTab(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await chrome.scripting.executeScript({ target: { tabId }, files: CONTENT_FILES });
    return await chrome.tabs.sendMessage(tabId, message);
  }
}

class PageError extends Error {}

async function snapshotTab(tabId) {
  try {
    const res = await chrome.tabs.sendMessage(tabId, { type: 'rf:snapshot' });
    if (!res?.snapshot) throw new Error('empty');
    return res;
  } catch {
    throw new PageError("Can't change this page.");
  }
}

// ---------- reframe flow ----------

function signature(snapshot) {
  let h = 5381;
  const s = snapshot.url + snapshot.elements.map((e) => `${e.ref}${e.tag}${e.text.slice(0, 30)}${e.href || ''}`).join('|');
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return h.toString(36);
}

function cachePut(key, value) {
  renderCache.delete(key);
  renderCache.set(key, value);
  if (renderCache.size > MAX_CACHE) renderCache.delete(renderCache.keys().next().value);
}

function mergePageStyle(base, ops) {
  const merged = { ...(base || {}) };
  for (const op of ops) {
    if (op.op !== 'page_style') continue;
    for (const [k, v] of Object.entries(op)) {
      if (k === 'op' || v === null || v === undefined) continue;
      if (v === false || (k === 'font_scale' && v === 1)) delete merged[k];
      else merged[k] = v;
    }
  }
  return merged;
}

async function updateGoal(tabId, text, ops, continuation) {
  const key = keys(tabId).goal;
  if (ops.some((op) => op.op === 'restore')) {
    await chrome.storage.session.remove(key);
    return;
  }
  if (!ops.length) return;
  const goal = (await getSession(key)) || {};
  const structural = ops.some((op) => op.op !== 'page_style');
  await chrome.storage.session.set({
    [key]: {
      message: structural && !continuation ? text : goal.message || null,
      pageStyle: mergePageStyle(goal.pageStyle, ops),
    },
  });
}

async function runReframe(tabId, text, { continuation = false } = {}) {
  const auth = await getAuth();
  if (!auth?.token) throw new ApiError('Signed out.', 401);

  const history = await chatHistory(tabId);
  if (!continuation) await pushChat(tabId, { role: 'user', content: text });

  const { snapshot, applied } = await snapshotTab(tabId);
  const cacheKey = `${text}|${signature(snapshot)}`;
  let res = continuation ? renderCache.get(cacheKey) : null;
  if (!res) {
    res = await apiFetch('/reframe', {
      method: 'POST',
      token: auth.token,
      body: { message: text, snapshot, history, applied, continuation },
    });
    if (res.operations?.length) cachePut(cacheKey, res);
  }
  const ops = res.operations || [];
  await chrome.tabs.sendMessage(tabId, { type: 'rf:apply', ops });
  await updateGoal(tabId, text, ops, continuation);
  await pushChat(tabId, { role: 'assistant', content: res.reply });
  return { ok: true };
}

async function withBusy(tabId, fn) {
  await setBusy(tabId, true);
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) await clearAuth();
    chrome.tabs.sendMessage(tabId, { type: 'rf:pending', on: false }).catch(() => {});
    const message = err instanceof ApiError || err instanceof PageError ? err.message : 'Something went wrong.';
    await pushChat(tabId, { role: 'error', content: message });
    return { error: message };
  } finally {
    await setBusy(tabId, false);
    if (pendingRebuild.delete(tabId)) schedulePageChange(tabId, 0);
  }
}

async function handleChat(tabId, text) {
  if (busyTabs.has(tabId)) return { error: 'busy' };
  return withBusy(tabId, () => runReframe(tabId, text));
}

// ---------- following the user across pages ----------

async function continueGoal(tabId) {
  const goal = await getSession(keys(tabId).goal);
  if (!goal?.message) return;
  if (busyTabs.has(tabId)) {
    pendingRebuild.add(tabId);
    return;
  }
  chrome.tabs.sendMessage(tabId, { type: 'rf:pending', on: true }).catch(() => {});
  await withBusy(tabId, () => runReframe(tabId, goal.message, { continuation: true }));
}

function schedulePageChange(tabId, delay = 150) {
  clearTimeout(debounceTimers.get(tabId));
  debounceTimers.set(
    tabId,
    setTimeout(() => {
      debounceTimers.delete(tabId);
      continueGoal(tabId);
    }, delay)
  );
}

async function onHello(tabId) {
  const goal = await getSession(keys(tabId).goal);
  const auth = await getAuth();
  const pending = Boolean(goal?.message && auth?.token);
  if (pending) schedulePageChange(tabId, 0);
  return { state: await tabState(tabId), pageStyle: goal?.pageStyle || null, pending };
}

// ---------- auth ----------

async function authenticate(path, body) {
  try {
    const res = await apiFetch(path, { method: 'POST', body });
    await setAuth({ token: res.token, user: res.user });
    return { ok: true };
  } catch (err) {
    return { error: err instanceof ApiError ? err.message : 'Something went wrong.' };
  }
}

chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area !== 'local' || !('auth' in changes)) return;
  const tabs = await chrome.tabs.query({ url: ['http://*/*', 'https://*/*'] });
  for (const tab of tabs) broadcast(tab.id);
});

// ---------- toolbar icon and shortcut ----------

async function toggleWidget(tab) {
  if (!tab?.id || !/^https?:/.test(tab.url || '')) return;
  try {
    await sendToTab(tab.id, { type: 'rf:toggle' });
  } catch {}
}

chrome.action.onClicked.addListener(toggleWidget);
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'toggle-chat') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  toggleWidget(tab);
});

chrome.tabs.onCreated.addListener(async (tab) => {
  if (tab.openerTabId === undefined) return;
  const goal = await getSession(keys(tab.openerTabId).goal);
  if (goal) await chrome.storage.session.set({ [keys(tab.id).goal]: goal });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  const k = keys(tabId);
  chrome.storage.session.remove([k.chat, k.goal, k.busy, k.open]);
  busyTabs.delete(tabId);
  pendingRebuild.delete(tabId);
});

// ---------- messages from content scripts ----------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab?.id;
  if (tabId === undefined) return false;
  switch (msg?.type) {
    case 'rf:hello':
      onHello(tabId).then(sendResponse);
      return true;
    case 'rf:chat':
      handleChat(tabId, String(msg.text || '').slice(0, 1000)).then(sendResponse);
      return true;
    case 'rf:login':
      authenticate('/auth/login', { email: msg.email, password: msg.password }).then(sendResponse);
      return true;
    case 'rf:register':
      authenticate('/auth/register', { email: msg.email, password: msg.password, display_name: msg.display_name }).then(sendResponse);
      return true;
    case 'rf:signout':
      clearAuth().then(() => sendResponse({ ok: true }));
      return true;
    case 'rf:set-open':
      chrome.storage.session.set({ [keys(tabId).open]: Boolean(msg.open) }).then(() => sendResponse({ ok: true }));
      return true;
    case 'rf:page-changed':
      schedulePageChange(tabId);
      return false;
    case 'rf:restored':
      chrome.storage.session.remove(keys(tabId).goal).then(() => sendResponse({ ok: true }));
      return true;
    default:
      return false;
  }
});
