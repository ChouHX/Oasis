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
  importAccounts: (lines, protocol) =>
    call("/api/accounts/import", { method: "POST", body: { lines, protocol } }),
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
export const STATUS = {
  total: { label: "账号总数", color: "#1677ff" },
  hits: { label: "已中签", color: "#52c41a" },
  unchecked: { label: "还没查过", color: "#8c8c8c" },
  checked: { label: "查过未中签", color: "#13c2c2" },
  check_errors: { label: "读信失败", color: "#ff4d4f" },
  registrations: { label: "旧预约记录", color: "#722ed1" },
};

export const STATUS_FILTER = [
  { value: "", label: "全部账号" },
  { value: "hit", label: "已中签" },
  { value: "pending", label: "未中签" },
];

// 中签证据。后端每个中签行都带 label，这份映射只用于筛选与图例。
export const SOURCE = {
  "success-mail": { label: "成功邮件", color: "#52c41a" },
  "oasis-mail": { label: "Oasis 来信", color: "#13c2c2" },
  "site-ok": { label: "站点已确认（无成功邮件）", color: "#fa8c16" },
};
