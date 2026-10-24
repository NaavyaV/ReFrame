export const DEFAULT_API = 'http://localhost:8000';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function getApiBase() {
  const { apiBase } = await chrome.storage.local.get('apiBase');
  return (apiBase || DEFAULT_API).replace(/\/+$/, '');
}

export async function setApiBase(apiBase) {
  await chrome.storage.local.set({ apiBase });
}

export async function getAuth() {
  const { auth } = await chrome.storage.local.get('auth');
  return auth || null;
}

export async function setAuth(auth) {
  await chrome.storage.local.set({ auth });
}

export async function clearAuth() {
  await chrome.storage.local.remove('auth');
}

export async function apiFetch(path, { method = 'GET', body, token } = {}) {
  const base = await getApiBase();
  let resp;
  try {
    resp = await fetch(base + path, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Can't reach the ReFrame server.", 0);
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    let message = 'Something went wrong.';
    if (typeof data.detail === 'string') message = data.detail;
    else if (resp.status === 422) message = 'Check your details and try again.';
    throw new ApiError(message, resp.status);
  }
  return data;
}
