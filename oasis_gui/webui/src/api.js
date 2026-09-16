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
  accounts: (status, page) =>
    call(`/api/accounts?page=${page}${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  registrations: () => call("/api/registrations"),
  proxies: () => call("/api/proxies"),
  saveProxies: (lines) => call("/api/proxies", { method: "POST", body: { lines } }),
  log: (since) => call(`/api/log?since=${since}`),
  start: (threads, mode) => call("/api/start", { method: "POST", body: { threads, mode } }),
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
// table and the registrations table so they never drift apart.
export const STATUS = {
  total: { label: "账号总数", color: "#1677ff" },
  pending: { label: "待注册", color: "#8c8c8c" },
  running: { label: "进行中", color: "#fa8c16" },
  registered: { label: "邮件确认注册", color: "#52c41a" },
  submitted: { label: "页面确认提交", color: "#13c2c2" },
  failed: { label: "失败", color: "#ff4d4f" },
  registrations: { label: "预约记录", color: "#722ed1" },
};

export const STATUS_FILTER = [
  { value: "", label: "全部状态" },
  { value: "pending", label: "待注册" },
  { value: "running", label: "进行中" },
  { value: "registered", label: "邮件确认" },
  { value: "submitted", label: "页面确认" },
  { value: "failed", label: "失败" },
];
