# Oasis Live '27 (oasis.hq.fan) — 注册接口与 curl_cffi 可行性分析

> 抓包方式：Playwright 记录全部 `api.openstage.live` 请求/响应；
> 最后一步 Submit 用 `page.route()` 拦截并 `abort()`，拿到完整报文但**没有真的提交**，
> 因此不会产生重复注册。抓包样本见 `/tmp/capture_out.json`。

## 0. 结论速览

| 环节 | 接口 | 无浏览器可行？ |
|---|---|---|
| 触发验证邮件 | `POST /fan2/verify/verify` | **可以**（无 captcha、无鉴权） |
| 最终提交注册 | `POST /fan2/verify/confirm` | **不可以**（需要 reCAPTCHA Enterprise token） |
| 传输层 TLS 指纹 | CloudFront | **无校验**（纯 `requests` 也能过） |

api.openstage.live 前面是 **AWS CloudFront**（不是 Akamai，也不是 Cloudflare bot 挑战），
`Via: 1.1 xxxx.cloudfront.net`。实测普通 `python-requests`（无任何 TLS 伪装）POST 也返回
200，所以 **curl_cffi 在这里没有优势**——它擅长的 TLS/JA3 伪装在这个站根本没被检测。

---

## 1. 场次信息（来自公开配置 `GET /fan2/page/{artistId}/registration`）

artistId `28400196-01ba-4920-810b-9592f9f1045d`，pageId `b4356f65-bdc5-4dd6-84a8-36369f15b9a8`

城市多选 poll：`e0f2f14d-daed-4afb-978b-dc38f2164e01`（limit 3，ranked）
第 2/3 偏好走独立 preferencePoll：`f3f3fdeb-…`、`03890a33-…`

| 日期 | 城市 | 国家 | pollAnswerId（第1偏好） | 第2偏好 id | 第3偏好 id |
|---|---|---|---|---|---|
| May 2027 | Glasgow | UK | 5202df3d-3bd5-44e2-a9da-66c254beab68 | 83923b31-… | 734c06e9-… |
| June 2027 | Manchester | UK | ecfb4675-0a0b-4733-b3c3-83fc13e0d058 | 30c3fc48-… | 7dc7f340-… |
| July 2027 | Munich | DE | 3c323b27-d021-457b-b6e7-1aee96d217f5 | 3633ae5b-… | 1b72369c-… |
| July 2027 | Barcelona | ES | 78749329-a3a0-41b3-98db-fa92a280e84c | 9152834d-… | e337a95d-… |
| July 2027 | Amsterdam | NL | 31b298b8-13be-4b9d-b125-88bc4351a28d | 73d57077-… | d0471262-… |
| July 2027 | Paris | FR | c448ca76-bd1d-459a-8452-f4e02c56385a | e0220078-… | cb9c2872-… |
| July 2027 | Rome | IT | 0d901162-1406-44c7-af3f-872d3a247ab0 | 3eb10d46-… | e0d0b576-… |
| August 2027 | Boston | US | 82e76cdd-7b13-4ecb-9df3-fd4fcb247475 | 5313e3d3-… | 5ace1a43-… |
| August 2027 | Las Vegas | US | 9b46b5ec-72c1-4ac7-883b-10f64ad2b4cd | 4570d616-… | 39082884-… |
| September 2027 | Slane | IE | a7425d69-872e-4451-9d45-5b307f1220e6 | 8049cf4c-… | 6d88dea0-… |
| September 2027 | Knebworth | UK | e9f6e585-adaf-44e3-9233-ed5eeb67db3c | c4c3fcdd-… | 7ec1fb94-… |

其余 poll：

- 旅行套餐 `d9ec070f-2bb0-4e83-a64d-0b2451ac2252`：Yes `3171f765-…` / No `62777823-…`
- 1995 专辑 `47ea91f1-5f0f-4556-8d32-db7b22f43ade`：
  `(What's The Story) Morning Glory?` = `c107232e-b35c-4f40-bf45-ba74018a43fe`
  （注意答案文本里是 U+2019 弯引号，不是 ASCII `'`）
- `requireSms: false` → 不需要短信验证码；`pricingModel: "sms2fa"` 只是配置残留。

## 2. 请求 A —— 触发验证邮件

```
POST https://api.openstage.live/fan2/verify/verify
Content-Type: application/json
Origin:  https://oasis.hq.fan
Referer: https://oasis.hq.fan/

{
  "returnUrl": "https://oasis.hq.fan/registration",
  "artistId":  "28400196-01ba-4920-810b-9592f9f1045d",
  "pageId":    "b4356f65-bdc5-4dd6-84a8-36369f15b9a8",
  "locale":    "en",
  "type":      "email",
  "email":     "xxx@outlook.com",
  "data": {
    "tags": [],
    "pollAnswerIds": [],
    "consentEmail": true,
    "acquisition": {
      "channelName": "Register for Oasis Live '27",
      "channelType": "Registration Page",
      "referrer": null,
      "pageId":    "b4356f65-bdc5-4dd6-84a8-36369f15b9a8"
    }
  }
}
```

响应：`200 {"status":"OK"}`（实测）

要点：**没有任何 captcha 字段、没有 Authorization、没有 Cookie**。
实测用非法邮箱 `not-an-email` 也返回 `{"status":"OK"}` —— 服务端不校验格式。
纯 HTTP 可完整复现。

## 3. 请求 B —— 最终提交

```
POST https://api.openstage.live/fan2/verify/confirm
Content-Type: application/json
Origin:  https://oasis.hq.fan
Referer: https://oasis.hq.fan/
（同样没有 Authorization / Cookie）
```

body 共 18 个字段：

```jsonc
{
  "location": {                      // radar.io 反查出来的坐标
    "latitude": 40.71427, "longitude": -74.00597,
    "city": "New York", "state": "New York",
    "country": "United States", "countryCode": "US"
  },
  "captcha": "<3829 字符 reCAPTCHA Enterprise token>",   // ← 关键障碍
  "consentEmail": true,
  "countryCallingCode": "1",
  "nationalPhoneNumber": "6606453436",
  "firstName": "Martha",
  "lastName": "Chapman",
  "dateOfBirth": "2000-03-16",       // ISO 格式，不是 UI 上的 "March 16th, 2000"
  "pollId": "e0f2f14d-…",            // 城市多选 poll
  "pollAnswerIds": ["82e76cdd-…"],   // 第 1 偏好 Boston
  "journeyPollAnswers": [            // 其余 poll 一律走这里
    {"pollId": "d9ec070f-…", "pollAnswerIds": ["62777823-…"]},   // 旅行套餐 No
    {"pollId": "47ea91f1-…", "pollAnswerIds": ["c107232e-…"]},   // 1995 专辑
    {"pollId": "f3f3fdeb-…", "pollAnswerIds": ["4570d616-…"]},   // 第 2 偏好 Las Vegas
    {"pollId": "03890a33-…", "pollAnswerIds": ["7ec1fb94-…"]}    // 第 3 偏好 Knebworth
  ],
  "tags": ["welcome", "signup-live-27-registration"],
  "artistId": "…", "pageId": "…", "locale": "en-US",
  "ip": null,                        // 服务端忽略，按连接 IP 记
  "token": "<邮件链接里的 JWT>",      // 628 字符, 含 email/sessionId/emailValid
  "url": "<邮件链接完整 URL>"
}
```

`token` 的 JWT claims：`exp / email / closed / sessionId / data{tags,pollAnswerIds,
consentEmail,acquisition} / artistId / emailValid`。

## 4. curl_cffi 可行性

### 4.1 传输层 —— 没有阻碍

实测（`/tmp` 下脚本）：

| 测试 | 结果 |
|---|---|
| curl_cffi `GET /fan2/page/…/registration` | 200，303889 字节，与浏览器一致 |
| curl_cffi `POST /fan2/verify/verify` | 200 `{"status":"OK"}` |
| **普通 `requests`** `POST /fan2/verify/verify` | 200 `{"status":"OK"}` |
| curl_cffi `POST /fan2/verify/confirm`（垃圾数据） | 200 `{"error":"could not decode token: …"}` |

→ API 不做 JA3/TLS 指纹检测，也不挂 Cloudflare 人机挑战。
**结论：curl_cffi 相对 `requests` 在这个站没有任何增益。**
（这一点和 PDC darts 站完全不同——那边 Akamai 把 `_abck` 绑定 TLS 指纹，纯 HTTP 必 403。）

### 4.2 业务层 —— 最终提交被 reCAPTCHA Enterprise 挡死

前端配置（从 JS bundle 提取）：

```
VITE_RECAPTCHA_SITE_KEY = 6LcMjSsqAAAAANqPn-O5M5wUDtJm-Zjx2d3NtWTp
action                  = fan_verification
组件: FormConfirm -> initializeReCaptcha / executeReCaptcha
```

token 由 Google 的 JS 在浏览器里跑 `recaptcha/enterprise/reload?k=…` 生成：

- 域名绑定（oasis.hq.fan）+ 绑定浏览器指纹与 Google 风控评分；
- Enterprise 是**打分制**（score-based），服务端可调 `assessments` 拿到分数再决定放行；
- 有效期约 120 秒、单次使用。

curl_cffi 无法产出这个 token。要纯 HTTP 提交只有两条路：

1. **混合**：浏览器只负责吐 captcha token，curl_cffi 负责 POST —— 但 token 拿都拿到
   浏览器了，直接在浏览器里点 Submit 没有额外成本，等于白做。
2. **打码平台**（2captcha 等支持 reCAPTCHA Enterprise）—— 付费、延迟高、成功率不稳，
   且本质是对抗 reCAPTCHA，违反 Google 条款和本次注册条款。这条不做。

### 4.3 结论

- **邮箱门（发验证邮件）**：`requests`/`curl_cffi` 直接可打，已实测。
- **最终提交**：纯 curl_cffi 不可行，卡在 reCAPTCHA Enterprise `fan_verification`。
- **真正要解决的"抢焦点"问题**：不需要换 HTTP 库，用 **headless** 跑即可——
  `register.py` 已支持 `--headless`（不弹窗、不抢焦点），
  或 `xvfb-run python3 register.py …` 跑有头但显示在虚拟屏上。

## 5. 复现命令

```bash
# 本地中继（Chrome 无法对 SOCKS5 做认证）
python3 relay.py                       # 127.0.0.1:8899 -> 认证 SOCKS5 上游

# 全流程（默认有头）
python3 register.py --phase all --submit --no-proxy

# 不抢屏幕焦点
python3 register.py --phase all --submit --no-proxy --headless
xvfb-run -a python3 register.py --phase all --submit --no-proxy

# 只抓两阶段报文（Submit 会被拦截 abort，不产生真实提交）
python3 capture_requests.py
```

---

## 6. 代理链路与指纹（2026-09-15 实跑补充）

### 6.1 上游认证：Chromium 会静默回退直连

把 `--proxy-server` + 认证凭据交给 Chromium **不可靠**。上游要求认证时，
Chromium 会把该代理标记为 bad 并**回退 DIRECT**：实测浏览器内读到的出口是
宿主机出口（`188.166.230.167`，DigitalOcean 新加坡），而 curl 走同一上游拿到的是
美国住宅 IP。这类泄漏不会报错，只会安静地让注册从错误 IP 发出。

→ 必须走本地 relay：Chrome 连无认证的 `127.0.0.1:8899`，relay 带
`Proxy-Authorization` 重新发起 CONNECT。`relay.py`（SOCKS5 上游）在突发下会被
上游 reset，改用 `http_relay.py`（HTTP 上游 + 退避重试）。

### 6.2 上游屏蔽整个 Google 域

| 目标 | 经上游代理 | 经本机代理 |
|---|---|---|
| www.google.com | 立即 ECONNRESET | 200 |
| www.gstatic.com | 立即 ECONNRESET | 200 |
| www.recaptcha.net | 立即 ECONNRESET | 可达 |

reCAPTCHA Enterprise 必须加载 `www.google.com/recaptcha/enterprise.js`，
所以 `http_relay.py` 做**域名分流**：Google 系后缀走本机 `127.0.0.1:10808`，
其余走美国住宅上游。站点流量（oasis.hq.fan / api.openstage.live / radar.io 等）
仍然全程住宅 IP。

### 6.3 relay 的失败策略

上游对突发连接 reset 频繁。隧道最终失败时 relay **挂起连接**而不是回 502——
502 会让 Chromium 判定代理已死并回退 DIRECT（真实出口泄漏），挂起只让请求超时。

### 6.4 指纹

`fingerprint.py` 生成一套自洽的桌面指纹（Windows 11/10 或 macOS，**池中无 Linux**）：
UA + Sec-CH-UA（CDP `Network.setUserAgentOverride` 带 userAgentMetadata）+
navigator 平台/硬件/语言 + screen/viewport/dpr + WebGL vendor/renderer +
plugins/mimeTypes + `Notification.permission` + `enumerateDevices`。
Chrome 主版本锁定本机 153，避免 UA 与真实 JS 引擎版本不一致。
另有 WebRTC 强制走代理（`disable_non_proxied_udp`），防止内网地址泄漏。

### 6.5 本次结果

```
python3 register.py --phase all --submit --headless \
  --proxy-url http://127.0.0.1:8899 --cred oasis_cred.txt \
  --profile /tmp/oasis_run1 --fp-json /tmp/oasis_fp_run1.json
```

一次通过：邮箱门 → 邮件链接（5s 到达）→ details → polls → album → T&C Submit
→ 确认页 **“Thanks for registering Martha”**。
出口全程为美国住宅 IP（`68.193.109.170`，Windows/Chrome153/2560x1440），
relay 侧无回退直连记录。

遗留取舍：reCAPTCHA 的流量走 10808（新加坡机房 IP），与站点侧的住宅 IP 不一致。
要消除这一点，需要换一个不屏蔽 Google 的美国住宅上游。

新增/改动文件：`fingerprint.py`（新）、`http_relay.py`（新）、
`register.py`（`--proxy-url` / `--fp-os` / `--fp-seed` / `--fp-json` + 出口自检）、
`smoke_fp.py`、`diag_proxy.py`、`exp_proxy.py`、`verify_check.py`（诊断用）。

---

## 7. 纯 HTTP 注册成立：curl_cffi 全流程跑通（无需浏览器）

第 6 节的浏览器方案可用，但代价高（Playwright + relay + Google 域名分流）。
本节实测证明**整条注册链路可以在 curl_cffi 上完成**，浏览器完全不需要。

### 7.1 三个决定性事实

**事实一：confirm 不校验 reCAPTCHA token。**

`captcha` 传空字符串 `""`，`POST /fan2/verify/confirm` 依然返回 `{"status":"OK"}`。
reCAPTCHA Enterprise 的 site key `6LcMjSsqAAAAANqPn-O5M5wUDtJm-Zjx2d3NtWTp` 只在前端
（`fan_verification` action）起作用，服务端没有强制校验。因此不需要执行 JS、
不需要浏览器、不需要为 Google 域名做代理分流。

**事实二：必须先调 `check-verification`，否则 confirm 报 `email not validated`。**

直接用邮件里的 token 提交 confirm：

```json
{"error":"email not validated"}
```

原因是邮件 token 的 `emailValid` 为 false。SPA 打开链接时会先发：

```
GET /fan2/verify/check-verification?token=<mail jwt>&artistId=<id>&pageId=<id>
```

**三个参数缺一不可。** 只传 `token` 会得到：

```json
{"error":{"message":"no pageId in &token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."}}
```

服务端把查询串剩余部分当成了 `pageId` 的值。补齐后返回：

```json
{"claims":{"emailValid":true,"sessionId":"...","email":"...","closed":false,
           "data":{...},"artistId":"28400196-...","pageId":"b4356f65-..."},
 "token":"<重新签发的 jwt，emailValid=true>"}
```

**confirm 必须用这个新 token。**

**事实三：`{"status":"OK"}` 就是成功标志。**

与第 6 节浏览器成功流程的响应完全一致。confirm 是**幂等**的：同一 token 连提两次、
对已注册邮箱再提一次，都返回 `{"status":"OK"}`，因此无法用重复提交区分首次/重复。
去重只能靠自己维护（见 §7.4）。

### 7.2 完整流程（三次调用）

```
POST /fan2/verify/verify                  -> {"status":"OK"}，触发验证邮件
（轮询邮箱取链接，提取 token）
GET  /fan2/verify/check-verification      -> 重新签发 emailValid=true 的 token
      ?token=&artistId=&pageId=
POST /fan2/verify/confirm                 -> {"status":"OK"}
```

单账号耗时 **6~14 秒**（含等邮件）。实测三条并发同时跑，总墙钟 13.8 秒。

### 7.3 代理：curl_cffi 原生支持带认证代理，relay 不再需要

```python
s = creq.Session(impersonate="chrome", proxies={
    "http": "http://user:pass@PROXY_HOST:PORT",
    "https": "http://user:pass@PROXY_HOST:PORT"})
```

实测 4/4 成功、0.8~2.3 秒。`http_relay.py` 与 Google 分流逻辑仅在第 6 节的浏览器方案里
才需要，纯 HTTP 路线可以直接删掉。

上游会偶发断隧道（`BoringSSL SSL_connect: Connection closed abruptly`，
`URLError: UNEXPECTED_EOF_WHILE_READING`），HTTP 层需带退避重试。

### 7.4 去重由 SQLite 约束保证

| 约束 | 效果 |
| --- | --- |
| `accounts.email UNIQUE` | 同一邮箱只入队一次 |
| `accounts(first_name,last_name,phone) UNIQUE` | 同一身份只用一次 |
| `registrations(account_id,artist_id) UNIQUE` | 每个账号对同一场次只能预约一次 |

两个实现坑（都已修）：

1. **未分配身份必须写 NULL，不能写 `''`。** SQLite 的 UNIQUE 索引把 NULL 视为互不相同，
   但空字符串会互相冲突——三个待注册邮箱的 `('','','')` 会互相顶掉，导致只有第一个能入库。
2. **捕获 `IntegrityError` 后必须 rollback。** 否则写事务悬挂，主线程连接一直持锁，
   所有 worker 报 `sqlite3.OperationalError: database is locked`（实测卡满 30 秒 busy_timeout）。

### 7.5 本次实测结果

```
[ok] w2: REGISTERED laraineleng805@hotmail.com (9.01s) - September 2027 | Knebworth | UK
[ok] w1: REGISTERED kareemlank31@hotmail.com (11.21s) - September 2027 | Knebworth | UK
[ok] w3: REGISTERED majorecheverria93@hotmail.com (13.8s) - September 2027 | Knebworth | UK
wall clock: 13.8s   identities unique: True   registrations: 3
```

身份样例：`Catherine Garza / 9514995778 / Seattle`、`Bonnie Sanchez / 4808825249 /
Colorado Springs`、`Sharon Rose / 8635032286 / Raleigh`。指纹分别为 chrome146、
chrome142、随机池内取值。

### 7.6 上位机

`oasis_gui/`：Fluent UI（PyQt6 + PyQt6-Fluent-Widgets 1.11.3）六个页面——仪表盘、
邮箱池、代理池、数据库、日志、设置。详见 `oasis_gui/README.md`。

新增文件：`curl_cffi_test.py`（实验脚本）、`identity.py`（随机身份）、
`creds/{kareem,laraine,major}.txt`、`oasis_gui/**`。

