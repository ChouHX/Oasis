// Thin fetch wrapper. Every endpoint returns JSON and answers 401 when the
// session cookie is gone, so that is handled in one place: the app drops back
// to the login screen instead of every page handling it.

export class AuthError extends Error {}

async function call(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) throw new AuthError("AUTH_REQUIRED");
  let data = {};
  try {
    data = await r.json();
  } catch (e) {
    data = {};
  }
  if (!r.ok && data.error) throw new Error(data.error);
  return data;
}

export const api = {
  login: (password) => call("/login", { method: "POST", body: { password } }),
  logout: () => call("/logout", { method: "POST" }),
  state: () => call("/api/state"),
  hits: () => call("/api/hits"),
  accounts: (status, page) =>
    call(`/api/accounts?page=${page}${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  registrations: () => call("/api/registrations"),
  log: (since) => call(`/api/log?since=${since}`),
  // 「立即巡检一轮」。间隔/并发/回看天数一并落地，所以改完就生效。
  start: (patch = {}) => call("/api/start", { method: "POST", body: patch }),
  stop: () => call("/api/stop", { method: "POST" }),
  saveConfig: (patch) => call("/api/config", { method: "POST", body: patch }),
  resetAccounts: () => call("/api/accounts/reset", { method: "POST" }),
  deleteAccounts: (status) =>
    call("/api/accounts/delete", { method: "POST", body: { status } }),
  importAccounts: (lines, protocol, opted = true) =>
    call("/api/accounts/import", { method: "POST", body: { lines, protocol, opted } }),
  // 把地址纳进/移出检测范围：给一批 id，或者给 scope（unmarked = 全部还没标记的）。
  optAccounts: (patch) => call("/api/accounts/opt", { method: "POST", body: patch }),
  icloudAliases: () => call("/api/icloud/aliases"),
  icloudImport: (emails) =>
    call("/api/icloud/import", { method: "POST", body: { emails } }),
  vacuum: () => call("/api/vacuum", { method: "POST" }),
  exportUrl: (what) => `/api/export?what=${what}`,
};

// Status keys and their labels/colours, shared by the dashboard, the account
// table and the hit table so they never drift apart.
//
// `hits` is the only conclusive number; `unchecked` / `checked` / `check_errors`
// are progress. The old status words (registered / submitted / failed) are not
// shown: a database from the previous build folds them into hits on open.
// 口径分两层：`total` 是池子里有多少地址，`opted` 是其中真的预约过、因此会被
// 检测的那部分。进度类的三个数只在 opted 里算 —— 没预约过的邮箱不会收到中签信，
// 扫它没有意义，把它算进「还没查过」只会让进度看起来永远落后。
export const STATUS = {
  total: { label: "账号总数", color: "#8c8c8c" },
  opted: { label: "已预约（检测范围）", color: "#1677ff" },
  unmarked: { label: "未标记（不查）", color: "#bfbfbf" },
  hits: { label: "已中签", color: "#52c41a" },
  unchecked: { label: "还没查过", color: "#8c8c8c" },
  checked: { label: "查过未中签", color: "#13c2c2" },
  check_errors: { label: "读信失败", color: "#ff4d4f" },
};

export const STATUS_FILTER = [
  { value: "", label: "全部账号" },
  { value: "opted", label: "已预约（检测范围）" },
  { value: "unmarked", label: "未标记" },
  { value: "hit", label: "已中签" },
  { value: "waiting", label: "未中签" },
  { value: "error", label: "读信失败" },
];

// 中签证据。只剩一个来源：注册截止之后的 Oasis 来信。曾经还有「成功邮件」与
// 「站点已确认」，那两个描述的其实是**预约成功** —— 把它们当中签，会让名单上
// 出现几百个「已中签」而它们一张票都没有。
export const SOURCE = {
  "oasis-mail": { label: "结果信", color: "#52c41a" },
};
