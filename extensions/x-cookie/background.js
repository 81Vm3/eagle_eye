// 读取 x.com 的 auth_token / ct0（HttpOnly，只有扩展的 cookies API 能读），
// 在本机 eagle 接收服务（bin/eagle x login）运行时推送过去。
// cookie 只发往 127.0.0.1，扩展本地只存它的 SHA-256，用来判断是否变化。

const SERVER = "http://127.0.0.1:18631";
const X_URL = "https://x.com";
const NAMES = ["auth_token", "ct0", "twid"];

async function readCookies() {
  const out = {};
  for (const name of NAMES) {
    const c = await chrome.cookies.get({ url: X_URL, name });
    if (c) out[name] = c.value;
  }
  return out;
}

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function ping() {
  try {
    const r = await fetch(`${SERVER}/ping`, { signal: AbortSignal.timeout(2000) });
    const j = await r.json();
    return j.app === "xsearch" ? j : null;
  } catch {
    return null;
  }
}

async function setStatus(status) {
  await chrome.storage.local.set({ status: { ...status, at: Date.now() } });
  chrome.action.setBadgeText({ text: status.ok ? "✓" : status.state === "offline" ? "" : "!" });
  chrome.action.setBadgeBackgroundColor({ color: status.ok ? "#1a7f37" : "#cf222e" });
}

// force=true：用户点了「立即同步」，只要服务在就推
async function sync(force = false) {
  const cookies = await readCookies();
  if (!cookies.auth_token || !cookies.ct0) {
    await setStatus({ ok: false, state: "logged_out", message: "未登录 x.com" });
    return;
  }
  const server = await ping();
  if (!server) {
    if (force) await setStatus({ ok: false, state: "offline", message: "eagle 接收服务未运行（bin/eagle x login）" });
    return;
  }
  const hash = await sha256(`${cookies.auth_token};${cookies.ct0}`);
  const { lastHash } = await chrome.storage.local.get("lastHash");
  if (!force && !server.want && hash === lastHash) return;

  try {
    const r = await fetch(`${SERVER}/cookie`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cookies),
      signal: AbortSignal.timeout(15000),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    await chrome.storage.local.set({ lastHash: hash });
    await setStatus({ ok: true, state: "synced", message: `已导入 ${j.account}` });
  } catch (e) {
    await setStatus({ ok: false, state: "error", message: String(e.message || e) });
  }
}

function ensureAlarm() {
  chrome.alarms.create("sync", { periodInMinutes: 0.5 });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  sync();
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  sync();
});
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "sync") sync();
});

let debounce;
chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || !cookie.domain.endsWith("x.com")) return;
  if (cookie.name !== "auth_token" && cookie.name !== "ct0") return;
  clearTimeout(debounce);
  debounce = setTimeout(() => sync(), 3000);
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg === "sync-now") {
    sync(true).then(() => reply(true));
    return true;
  }
});
