const $ = (id) => document.getElementById(id);

function mark(el, ok, text) {
  el.textContent = text;
  el.className = ok ? "ok" : "bad";
}

async function refresh() {
  const auth = await chrome.cookies.get({ url: "https://x.com", name: "auth_token" });
  mark($("login"), !!auth, auth ? "已登录" : "未登录");

  let up = false;
  try {
    const r = await fetch("http://127.0.0.1:18631/ping", { signal: AbortSignal.timeout(1500) });
    up = (await r.json()).app === "xsearch";
  } catch {}
  mark($("server"), up, up ? "运行中" : "未运行");

  const { status } = await chrome.storage.local.get("status");
  if (status) {
    $("msg").textContent = `${status.message}（${new Date(status.at).toLocaleTimeString()}）`;
    $("msg").className = status.ok ? "ok" : "bad";
  }
}

$("sync").addEventListener("click", async () => {
  $("sync").disabled = true;
  $("msg").textContent = "同步中…";
  await chrome.runtime.sendMessage("sync-now");
  await refresh();
  $("sync").disabled = false;
});

refresh();
