# Oasis Live '27 注册控制台

基于 **curl_cffi 纯 HTTP** 的注册上位机（Fluent UI 桌面端），带代理池、线程池、实时日志与 SQLite 去重落库。

## 注册页完整流程（用 CDP 实驱逆向出来，与官方 SPA 一致）

用 CDP 连到浏览器、开一个**无痕 context**，把整个表单走通，可以确认官方的真实步骤：

| 步 | 内容 | 关键元素 |
| --- | --- | --- |
| 1 | 资料 | `#soundcheckConfirmFirstName` `#soundcheckConfirmLastName` `#birthDate` `input#soundcheckConfirmLocation` `#soundcheckConfirmPhoneNumber` |
| 2 | 场次偏好 + 是否要交通 | `[data-testid=multiselect]` 里的 `[role=combobox]`（"First preference*"，最多 3 个），另一个 combobox 是 Yes/No |
| 3 | 「Please answer the question below」专辑题 | 一个 `[role=combobox]` |
| 4 | Terms & Conditions | **必须先点 `Scroll to bottom`，再点 `Submit`** |

第 4 步是最大的坑：不滚到底，`Submit` 点了没反应，**而且不报错**。全部走完页面会显示 "Thanks for registering XXX"。

**但「Thanks」不代表注册成功** —— 实测页面显示 Thanks、confirm 返回 200，邮箱里仍然只有验证邮件、没有 `Registration Complete`。**唯一可信的成功信号始终是那封邮件。**

### 各字段的填写要点（踩过的坑）

- **电话**：`fill()` 不触发组件的输入事件，必须 `type(delay=100+)` 再 `Tab` 失焦，否则显示 "Please enter a valid phone number"
- **生日**：点击后弹日历，`#year` / `#month` 是自定义 select，要逐级点；不能直接输入
- **地址**：combobox 的 `div` 和 `input` **共用同一个 id**，`#soundcheckConfirmLocation` 会匹配到两个元素；要用 `input#soundcheckConfirmLocation`；必须逐字输入让 radar.io 补全返回选项后点选

### 官方请求体与我们实现的唯一实质差异

抓到 SPA 自己发出的 confirm（**带真 captcha，页面显示 Thanks**）：

```json
{
  "location": {"latitude":32.78306, "longitude":-96.80667, "city":"Dallas",
               "county":"Dallas County",          ← 我们缺这个字段
               "country":"United States", "countryCode":"US", "state":"Texas"},
  "captcha": "0cAFcWeA...",
  "ip": "73.134.115.213",                        ← 真实出口 IP
  "locale": "en-US", "consentEmail": true, ...
}
```

`journeyPollAnswers`、`pollAnswerIds`、`tags`、`pollId`、`artistId`、`pageId` 与我们**完全一致**（逐项比对过）。差异只有 `county` 与 `ip`。

`county` 来自 radar.io 的补全结果；我们的城市数据是静态表，**没有 county**，目前留空而不是编造。

## ⚠️ 携带真 captcha token 会让注册静默失败（唯一干净的隔离实验）

**同一代理、同一信箱、同一分钟，只改 captcha**：

| captcha | confirm 响应 | Registration Complete |
| --- | --- | --- |
| **空** | `{"status":"OK"}` | ✅ **收到** |
| 真 token（2382 字符） | `{"status":"OK"}` | ❌ **永远收不到** |

这是最可靠的一组证据——**做在站点开始限流之前**。

### ⚠️ 测试时间线：10:40 UTC 之后的结果全部不可信

整理所有成功记录后发现一条清晰的分界：

```
所有产出 Registration Complete 的注册  →  全部发生在 UTC 10:40 之前
UTC 10:40 之后的所有尝试（含 SPA 自驱）  →  全部只有验证邮件，没有成功邮件
```

10:40 之后连**验证邮件都会随机不投递**，说明站点对我的测试流量状态已经变了（限流或临时封禁）。**那之后的对照实验都不作为结论依据**，包括我一度以为是「`ip` 字段填了反而更糟」的推断——那也很可能只是限流的表现。

**结论：等站点状态恢复后需要重测。**

### 怎么重测（下次直接照做）

站点恢复后（先确认验证邮件能正常到达），要一次性把下面几件事跑清楚，**别像我这次一样边测边改**，否则混淆变量太多：

1. **同一信箱的多个 `+tag` 别名、同一代理、同一分钟**，只改 captcha 有无 —— 这是唯一能定性的对照
2. **SPA 自驱一次**（`/tmp/spa6.py` 那套流程），抓它自己的 confirm，看它能不能拿到成功邮件
3. 若能，**逐字段补差**：先加 `county`，再加 `ip`，每次只加一个

**每次改动单独验证，不要合并。**我这轮最大的教训就是同时改了两处（`ip` + `consentMessaging`），结果分不清是哪个导致的失败。

### 我排除掉的假设（都实测过）

想找出「同一个真实浏览器能过、我们过不了」的差异，逐项试了：

| 假设 | 做法 | 结果 |
| --- | --- | --- |
| token 跨客户端回放无效 | 用浏览器模式：token 与提交**同一个 Chromium 页面**（同会话、同 IP、同 TLS） | ❌ 仍失败 |
| 无头被识别 | 用 xvfb 跑**有头** Chromium | ❌ 仍失败 |
| 缺 `ip` 字段 | 按 SPA 的做法从 Cloudflare trace 取真实出口 IP 填入 | ❌ 仍失败，**且把原本能成功的空 captcha 路径也弄坏了** |
| 缺 `consentMessaging` | 补上 `true`（真实浏览器里有） | 未单独验证，一并退回 |
| 出口不一致 | 换成**粘性会话**代理，实测连续 5 次同一出口 | ❌ 仍失败 |

`ip` 那条尤其值得记：**填了反而更糟**。真实浏览器填是因为它的所有请求都出自同一条浏览器连接；我们填的是一个可能对不上的值，比留空更危险。所以现在**保持 `null`**。

### 剩余最可能的解释

reCAPTCHA Enterprise 是**评分**机制，不是通过/拒绝二值。空 captcha 服务端直接跳过评分；非空 token 进入评分，而 Playwright 驱动的 Chromium 拿到的是低分——即使我给无头补了 UA、plugins、`window.chrome`、WebGL、时区。你手动跑的是真实有头 Windows Chrome，评分天然更高。

### 现阶段的取舍

**要「带 captcha」就注册不出来；要注册出来就不能带 captcha。** 二选一，没有第三条路（我已尽力找过）。

代码里留了 `send_captcha` 开关（默认关）。打开它只有在一种情况下有意义：你确认站点开始**强制**校验 captcha——那时不接受 captcha 的路径本来也会被拒，打开至少不亏。

### 关于「后续被清算」的风险

你的担心无法用实验证伪，我不假装能。但可以给出两条实测事实供权衡：

- 站点当前对**空 captcha** 的请求直接放行，说明它没有把 `captcha` 当作必填
- **带真 captcha 是 100% 立即失败**，不是概率问题

### 只保留浏览器模式

`hybrid`（浏览器签 captcha + curl_cffi 提交）已移除：实测 curl 提交会被站点的风控
识别。留着它只会让人误选然后白丢账号，所以配置、界面、环境变量都清掉了，旧配置
里的 `hybrid` 会被自动纠正为 `browser`。

### 性能：实测 90~150 秒 → 24 秒

单账号稳定态耗时，每个数字都是实测：

| 改动 | 单账号 | 说明 |
| --- | --- | --- |
| 起点（每账号新起 chromium） | 90~150s | |
| 共享浏览器 + 条件等待 + 页面优先判定 | 73s | 开着 verify_success |
| 同上，关掉 verify_success | 44s | |
| **curl 发 verify** + 去掉页面预热 | 33s | |
| **邮件预取** | **24s** | 现在的默认路径 |

#### verify 由 curl 发，提交仍由浏览器发

这是关键的一条界线，实测划出来的：

| captcha | 提交方式 | 结果 |
| --- | --- | --- |
| 空 | curl | 成功过 6 次 |
| 浏览器自签 | **curl** | **从未成功** |
| 外部（打码平台） | 浏览器 | 成功 |
| 页面自签 | 浏览器 | 成功 |

**唯一失败的组合是「真 captcha + curl 提交」。** 所以提交必须走浏览器；而 verify
是另一回事——旧的 hybrid 流程里 verify 本来就是 curl 发的，邮件每次都正常到达。

结论：verify 交给 curl，`/registration` 那次预热页面就不需要了（它原本只是给
verify 的 fetch 提供一个 origin），省掉 6 秒。

#### 邮件预取

验证邮件要 ~10 秒才到，而这 10 秒原本是 worker 在干等。现在有个后台线程提前给
接下来的几个账号触发验证邮件，worker 走到那个账号时邮件已经在收件箱里了，直接
打开就行。

深度刻意保持小（默认等于线程数）：请求本身不贵但不是免费，而且**验证链接会过期**，
一次要二十个账号的邮件只会得到一堆失效链接。超过 `PREFETCH_MAX_AGE`（10 分钟）
的条目会被丢弃，worker 发现没有新鲜条目时自己发一次。

#### 前面的三个改动



| 改动 | 效果 |
| --- | --- |
| 浏览器模式改用**共享 chromium**（CDP 连接，每账号只开一个 context） | 每账号省 2~4 秒启动 + 约 220MB |
| 表单驱动的**固定等待改成条件等待** | 21.45s → 5.45s |
| 判定顺序反过来：**先看页面，邮件只做加分项** | 每账号省最多 150 秒 |

第三条最要紧：原来先等满 `success_timeout` 找成功邮件，而浏览器模式实测**根本不发
这封邮件**，等于每个账号白等一分钟以上。现在页面确认后最多再看 30 秒
（`MAIL_GRACE_AFTER_PAGE`），只有**页面没确认**时才等满预算——那时邮件确实是唯一的
证据。

实测单账号从 90~150 秒降到 **73~105 秒**，表单部分从 54 秒降到 **17 秒**。

固定等待为什么该删：SPA 自己的 DOM 就绪只要 **1.04 秒**（实测），那些 4.2 秒、1.8 秒
是「不知道它什么时候好就干等」的猜测。改成等下一个元素出现，快机器不用为慢机器
的估值买单。

### 报错：区分「站点拒绝」和「没连上」

代理挂掉不该把账号标记成 failed。现在按异常内容分类：

```
传输层失败（ERR_CONNECTION_CLOSED / ERR_TUNNEL_CONNECTION_FAILED /
            upstream unavailable / ConnectionResetError …）
  → 同 worker 内重试 1 次；仍失败则**退回队列**，账号未消耗
站点拒绝（页面没确认、邮箱校验失败 …）
  → 记为 failed，不重试
```

退回队列有个坑：账号回到 `pending` 后同一个 worker 立刻又会领回来，死代理下会在
**一轮内无限循环**。所以每轮有传输失败预算（`TRANSIENT_PER_ROUND`），超了就提前结束
本轮，交给外层的 60 秒退避。实测：死代理下 7 次失败后停下，**4 个账号全部仍在队列**。

### 浏览器模式 = 驱动官方表单，不是自己拼请求

原来浏览器模式是「打开邮件链接 → 用自己的 body 发 fetch」。**那样页面永远停在资料步骤，不会出现成功页**，而且请求体缺 `location.county`、`ip` 等字段。

现在改成**填表 → 逐页点到底 → 让 SPA 自己提交**，与真人完全一致：

| 步 | 操作 | 坑 |
| --- | --- | --- |
| 1 资料 | 姓名 / 生日 / 地址 / 电话 → Continue | 电话要 `type()` 不能 `fill()`；生日是自定义日历，要逐级点，填不上要**重开重试**；地址要用 `input#id` |
| 2 场次 | multiselect 选最多 3 个 + 交通选项 | 按城市名匹配选项文本 |
| 3 专辑题 | 一个 combobox | — |
| 4 T&C | **先点 `Scroll to bottom` 再点 `Submit`** | 不滚到底，`Submit` 点了**完全没反应且不报错** |

**又一个坑**：`"Terms & Conditions"` 出现在**每一页的页脚**，用它判断「是否到了 T&C 页」会误判。要用 T&C 页独有的句子 `"click the submit button below"`。

实测跑通：

```
[info] details: name='Mark' phone='8303661324' dob='September 28th, 1994' loc='Raleigh...'
[info] 3 venue(s) selected
[info] step 1: Please answer the question below   → album options: 7 → picking 'Be Here Now'
[info] step 2: Terms & Conditions
[info] page confirms the registration
[ok  ] SUBMITTED fallacy-longing-9p@icloud.com (273.16s, browser)  [页面确认，无成功邮件]
```

注意浏览器模式**需要 Google 可达**才签得出 captcha——没配 `google_proxy` 时会卡在 `wait_for_function` 超时。

## 两种成功状态：`registered` 与 `submitted`

实测确认：**浏览器模式就是不发送「Registration Complete」邮件**，页面显示 "Thanks for registering XXX" 就是成功。所以库里用**两个状态**区分证据强度：

| 状态 | 含义 | 怎么判定 |
| --- | --- | --- |
| `registered` | **邮件确认**注册成功 | 信箱里收到 `Registration Complete` |
| `submitted` | **页面确认**提交成功 | 提交后页面出现 "Thanks for registering"（浏览器模式不发邮件） |

仪表盘上分成两张卡（「邮件确认注册」/「页面确认提交」），邮箱池的清理按钮也各有一个，可以分别清理。

`submitted` 的账号在 `error` 字段里写了说明，方便回查：

```
页面确认注册成功（浏览器模式不发成功邮件）
```

**浏览器模式的判定逻辑**（`BrowserRegistrar.register`）：

```
提交后：
  收到成功邮件     -> evidence = "mail"  -> registered
  没邮件但页面确认  -> evidence = "page"  -> submitted
  两者都没有        -> 判失败
```

hybrid 模式不走这条路径——它不打开页面，看不到页面状态，所以仍然只认邮件。

## 判定注册是否成功：只看邮箱，别信响应### 只保留浏览器模式

`hybrid`（浏览器签 captcha + curl_cffi 提交）已移除：实测 curl 提交会被站点的风控
识别。留着它只会让人误选然后白丢账号，所以配置、界面、环境变量都清掉了，旧配置
里的 `hybrid` 会被自动纠正为 `browser`。

### 性能：实测 90~150 秒 → 24 秒

单账号稳定态耗时，每个数字都是实测：

| 改动 | 单账号 | 说明 |
| --- | --- | --- |
| 起点（每账号新起 chromium） | 90~150s | |
| 共享浏览器 + 条件等待 + 页面优先判定 | 73s | 开着 verify_success |
| 同上，关掉 verify_success | 44s | |
| **curl 发 verify** + 去掉页面预热 | 33s | |
| **邮件预取** | **24s** | 现在的默认路径 |

#### verify 由 curl 发，提交仍由浏览器发

这是关键的一条界线，实测划出来的：

| captcha | 提交方式 | 结果 |
| --- | --- | --- |
| 空 | curl | 成功过 6 次 |
| 浏览器自签 | **curl** | **从未成功** |
| 外部（打码平台） | 浏览器 | 成功 |
| 页面自签 | 浏览器 | 成功 |

**唯一失败的组合是「真 captcha + curl 提交」。** 所以提交必须走浏览器；而 verify
是另一回事——旧的 hybrid 流程里 verify 本来就是 curl 发的，邮件每次都正常到达。

结论：verify 交给 curl，`/registration` 那次预热页面就不需要了（它原本只是给
verify 的 fetch 提供一个 origin），省掉 6 秒。

#### 邮件预取

验证邮件要 ~10 秒才到，而这 10 秒原本是 worker 在干等。现在有个后台线程提前给
接下来的几个账号触发验证邮件，worker 走到那个账号时邮件已经在收件箱里了，直接
打开就行。

深度刻意保持小（默认等于线程数）：请求本身不贵但不是免费，而且**验证链接会过期**，
一次要二十个账号的邮件只会得到一堆失效链接。超过 `PREFETCH_MAX_AGE`（10 分钟）
的条目会被丢弃，worker 发现没有新鲜条目时自己发一次。

#### 前面的三个改动



| 改动 | 效果 |
| --- | --- |
| 浏览器模式改用**共享 chromium**（CDP 连接，每账号只开一个 context） | 每账号省 2~4 秒启动 + 约 220MB |
| 表单驱动的**固定等待改成条件等待** | 21.45s → 5.45s |
| 判定顺序反过来：**先看页面，邮件只做加分项** | 每账号省最多 150 秒 |

第三条最要紧：原来先等满 `success_timeout` 找成功邮件，而浏览器模式实测**根本不发
这封邮件**，等于每个账号白等一分钟以上。现在页面确认后最多再看 30 秒
（`MAIL_GRACE_AFTER_PAGE`），只有**页面没确认**时才等满预算——那时邮件确实是唯一的
证据。

实测单账号从 90~150 秒降到 **73~105 秒**，表单部分从 54 秒降到 **17 秒**。

固定等待为什么该删：SPA 自己的 DOM 就绪只要 **1.04 秒**（实测），那些 4.2 秒、1.8 秒
是「不知道它什么时候好就干等」的猜测。改成等下一个元素出现，快机器不用为慢机器
的估值买单。

### 报错：区分「站点拒绝」和「没连上」

代理挂掉不该把账号标记成 failed。现在按异常内容分类：

```
传输层失败（ERR_CONNECTION_CLOSED / ERR_TUNNEL_CONNECTION_FAILED /
            upstream unavailable / ConnectionResetError …）
  → 同 worker 内重试 1 次；仍失败则**退回队列**，账号未消耗
站点拒绝（页面没确认、邮箱校验失败 …）
  → 记为 failed，不重试
```

退回队列有个坑：账号回到 `pending` 后同一个 worker 立刻又会领回来，死代理下会在
**一轮内无限循环**。所以每轮有传输失败预算（`TRANSIENT_PER_ROUND`），超了就提前结束
本轮，交给外层的 60 秒退避。实测：死代理下 7 次失败后停下，**4 个账号全部仍在队列**。

### 浏览器模式 = 驱动官方表单，不是自己拼请求

原来浏览器模式是「打开邮件链接 → 用自己的 body 发 fetch」。**那样页面永远停在资料步骤，不会出现成功页**，而且请求体缺 `location.county`、`ip` 等字段。

现在改成**填表 → 逐页点到底 → 让 SPA 自己提交**，与真人完全一致：

| 步 | 操作 | 坑 |
| --- | --- | --- |
| 1 资料 | 姓名 / 生日 / 地址 / 电话 → Continue | 电话要 `type()` 不能 `fill()`；生日是自定义日历，要逐级点，填不上要**重开重试**；地址要用 `input#id` |
| 2 场次 | multiselect 选最多 3 个 + 交通选项 | 按城市名匹配选项文本 |
| 3 专辑题 | 一个 combobox | — |
| 4 T&C | **先点 `Scroll to bottom` 再点 `Submit`** | 不滚到底，`Submit` 点了**完全没反应且不报错** |

**又一个坑**：`"Terms & Conditions"` 出现在**每一页的页脚**，用它判断「是否到了 T&C 页」会误判。要用 T&C 页独有的句子 `"click the submit button below"`。

实测跑通：

```
[info] details: name='Mark' phone='8303661324' dob='September 28th, 1994' loc='Raleigh...'
[info] 3 venue(s) selected
[info] step 1: Please answer the question below   → album options: 7 → picking 'Be Here Now'
[info] step 2: Terms & Conditions
[info] page confirms the registration
[ok  ] SUBMITTED fallacy-longing-9p@icloud.com (273.16s, browser)  [页面确认，无成功邮件]
```

注意浏览器模式**需要 Google 可达**才签得出 captcha——没配 `google_proxy` 时会卡在 `wait_for_function` 超时。

## 两种成功状态：`registered` 与 `submitted`

实测确认：**浏览器模式就是不发送「Registration Complete」邮件**，页面显示 "Thanks for registering XXX" 就是成功。所以库里用**两个状态**区分证据强度：

| 状态 | 含义 | 怎么判定 |
| --- | --- | --- |
| `registered` | **邮件确认**注册成功 | 信箱里收到 `Registration Complete` |
| `submitted` | **页面确认**提交成功 | 提交后页面出现 "Thanks for registering"（浏览器模式不发邮件） |

仪表盘上分成两张卡（「邮件确认注册」/「页面确认提交」），邮箱池的清理按钮也各有一个，可以分别清理。

`submitted` 的账号在 `error` 字段里写了说明，方便回查：

```
页面确认注册成功（浏览器模式不发成功邮件）
```

**浏览器模式的判定逻辑**（`BrowserRegistrar.register`）：

```
提交后：
  收到成功邮件     -> evidence = "mail"  -> registered
  没邮件但页面确认  -> evidence = "page"  -> submitted
  两者都没有        -> 判失败
```

hybrid 模式不走这条路径——它不打开页面，看不到页面状态，所以仍然只认邮件。

## 判定注册是否成功：只看邮箱，别信响应

注册成功会收到**两封**邮件，第二封才是标志：

```
Verify Your Email
Oasis Live '27 Registration Complete
```

**`confirm` 返回 `{"status":"OK"}` 毫无判别力**——账号被拒、重复注册、captcha 无效时它都返回 OK。实测 `robertlaro84@gmail.com` 连发 6 次验证邮件、confirm 每次都 OK，**一封成功邮件都没有**，那是被站点标记的地址。

### 开启「成功后校验邮件」让程序自己判断

设置页有个开关（配置项 `verify_success`，**默认关闭**）。开启后 confirm 之后会等成功邮件，等不到就把该账号判为失败：

```
[info] confirm -> {"status":"OK"} (14.7s)
[info] waiting for success mail: 62s/120s
[info] no success mail within 120s - confirm answered OK but the registration did not complete
[error] FAILED robertlaro84@gmail.com - ... the address was refused
```

**为什么默认关闭**：要等，实测成功邮件在验证邮件之后 48 秒 ~ 2.5 分钟到达，所以每个账号会多花最多 180 秒（`success_timeout`）。

**建议**：先开一轮小批量摸清你的账号池里有多少是被拒的，之后关掉跑量。

判别的依据是**正文里的 `successfully registered`**，不是标题——原因见下。

### 匹配标题时注意 MIME 编码（踩过的坑）

站点标题头是编码过的：

```
Subject: =?UTF-8?Q?Oasis_Live_=E2=80=9927_Registration_Complete?=
```

**空格变成了下划线。** 直接对原始头做 `"Registration Complete" in subject` 永远匹配不上，会得到假阴性。必须先解码：

```python
from email.header import decode_header
subj = "".join(t.decode(c or "utf-8", "ignore") if isinstance(t, bytes) else t
               for t, c in decode_header(raw_subject))
```

我就因为这个错判过一次——见下面。

### `+tag` 别名可用，而且能救「被标记的地址」

实测同一个信箱的两个地址：

| 使用的地址 | 验证邮件 | 成功邮件 | 结果 |
| --- | --- | --- | --- |
| `robertlaro84@gmail.com` | 6 封 | **0 封** | ❌ 地址被标记 |
| `robertlaro84+new1@gmail.com` | 1 封 | **1 封** | ✅ 注册成功 |

**结论**：站点标记的是**具体地址**，不是整个信箱。底座地址被标记后，同一信箱的 `+tag` 别名仍能正常注册——这是个可用的绕行手段。

> 早前本文件写过「站点不接受 `+tag` 别名」，那是**错的**。当时的判定脚本用原始标题头做子串匹配，被上面的 MIME 编码坑了；别名其实在 09:08:15 就注册成功了。

## 多线程共用一个浏览器进程（不是每线程一个）

早先的实现是**每个线程起一个独立浏览器**。实测下来这是错的——代价高一倍，还换不到任何隔离。

### 独立进程根本不提供指纹多样性

同一台机器上开 3 个独立 Chromium，逐个取指纹：

```
browser1: canvas=d8PqXnME/g0AAP//5MhiMwAA
browser2: canvas=d8PqXnME/g0AAP//5MhiMwAA
browser3: canvas=d8PqXnME/g0AAP//5MhiMwAA
-> 唯一 canvas 指纹数: 1/3
```

**三个完全一样。** 指纹来自 Chromium 构建 + 机器，不是进程。开 3 个进程和开 3 个 context，指纹一模一样，所以"多开进程更安全"这个前提不成立。

真正起隔离作用的是 **context**：cookie、storage、cache 各自独立。这正是我们需要的。

### 内存

| 方案 | 每 3 个工作页 |
| --- | --- |
| 3 个独立浏览器 | +1269 MB（423 MB/线程） |
| 1 个浏览器 + 3 个 context | +598 MB（199 MB/线程） |

省一半。8 线程的话是 3.4GB vs 1.6GB。

### 怎么做到多线程共用一个浏览器

唯一的障碍是 **Playwright 同步 API 绑定线程**——在 A 线程启动的 browser 不能从 B 线程调用。解法是自己带 `--remote-debugging-port` 启动 Chromium，然后每个线程 `connect_over_cdp()` 连上去：

```python
shared browser on :49511 (pid 1097552, ...)      ← 一个进程
warm context ready on http://127.0.0.1:49511     ← 线程1 的 context
warm context ready on http://127.0.0.1:49511     ← 线程2 的 context
warm context ready on http://127.0.0.1:49511     ← 线程3 的 context
```

每个线程有自己的 `sync_playwright()` 实例和 CDP 连接，浏览器在独立进程里，不违反线程亲和性。`b.close()` 只断这条连接，不会关掉浏览器。

**踩过的坑**：`_shared_browser` 里判断"存活"时，刚创建还没启动的实例 `proc is None` 会被误判成已死——三个线程几乎同时到达就各起了一个浏览器。现在把启动放进锁里，并且锁改成了 `RLock`。

## 为什么 curl 取不到 captcha token

抓包录制了 reCAPTCHA Enterprise 的完整链路：

```
GET  google.com/recaptcha/enterprise.js?render=SITEKEY
GET  gstatic.com/recaptcha/releases/<ver>/recaptcha__en.js
GET  google.com/recaptcha/enterprise/anchor?ar=1&k=SITEKEY&co=<base64 origin>&...
GET  google.com/recaptcha/enterprise/webworker.js
POST google.com/recaptcha/enterprise/reload?k=SITEKEY     ← token 从这里出来
POST google.com/recaptcha/enterprise/clr?k=SITEKEY
```

问题在 `reload` 的请求体：

```
content-type: application/x-protobuffer
body 长度: 10574 字节
```

里面嵌着加密信号块（`03AFcWeA7Dlm...`），是 Google 的 JS 用**浏览器实时指纹**——canvas、WebGL、字体、时序、交互遥测——算出来并加密的。这个 10KB protobuf 在 Python 里重算不出来；能做的只有开真浏览器，或者买打码服务。

**所以 curl 路线到此为止。**

## 但 captcha 不必每号开一次页面（提速 ~60 倍）

关键事实：**captcha 不依赖注册 token**。reCAPTCHA 只需要页面 origin（`oasis.hq.fan`）和 site key，跟邮件链接没关系。

所以浏览器**只预热一次、常驻复用**即可：

```
第一次（含浏览器预热）: 15.5s
之后每一次:             0.55 ~ 0.61s
```

对比旧做法（每个号都开一次邮件链接页面取 token）的约 40 秒——**每个账号省下近 40 秒**。混合模式的流程也简化了：token 直接用 curl 的 `check-verification` 刷新，浏览器只负责签 captcha。

## 拟人化停顿（防「太快」被识别）

真人收到邮件、打开链接、填完表提交，中间必然有间隔。提交紧跟验证是后端最容易测的自动化特征。

设置页新增「提交前停顿(秒)」，默认 **45**，实际在 **0.6~1.4 倍**之间随机（即 27~63 秒）。填 0 关闭。

要不要开取决于你的取舍：开了更接近真人，但每个号多花约 45 秒。

## 注册方式只有两种

`MODES = ("hybrid", "browser")`，默认 **hybrid**。

**纯 HTTP 模式（curl_cffi 直发、captcha 留空）已移除。** 移除的不是流程本身——hybrid 用的还是同一套 curl_cffi 调用——而是「可以不带 captcha 提交」这条 UI 路径。现在 captcha 是否携带由 `send_captcha` 开关决定，见上一节。

| 方式 | 说明 | 实测耗时 |
| --- | --- | --- |
| **hybrid**（默认） | 常驻共享浏览器按需签 captcha，提交走 curl_cffi | 29 秒（不签 captcha） |
| browser | 真实 Chromium 全流程，页面内 fetch 提交 | 60~120 秒 |

## 链式代理（短效代理走本地前置）

短效住宅代理在国内常常**拨不通**——TCP 连得上、但发什么都不回，看起来像代理已失效。实际是它只能从境外访问。设置里填「前置代理(链路)」即可：

```
前置代理(链路)   socks5://127.0.0.1:10808
代理池           socks5://用户:密码@PROXY_HOST:PORT
```

引擎会自动把两者串起来：**curl_cffi → 本地 relay → 前置代理 → 上游代理 → 目标站**。

```
[info] chained via socks5://127.0.0.1:10808; curl_cffi -> http://127.0.0.1:39231
[info] exit 128.177.7.113 -> US (identities for this proxy will use US addresses)
[ok  ] REGISTERED ... (14.36s, http)
```

**为什么必须走本地 relay**：curl_cffi 自己不会链式代理，浏览器也需要一个 HTTP 代理入口。relay 同时支持两种上游协议：

| 上游协议 | relay 行为 |
| --- | --- |
| `http(s)://` | 向它发 HTTP CONNECT，注入 `Proxy-Authorization` |
| `socks5://` | 直接做 SOCKS5 握手（含用户名/密码认证） |

短效代理多数是 **SOCKS5**，所以早期版本对它发 HTTP CONNECT 会一直等到超时，误判成"代理死了"。

前置代理也支持写进代理行本身，用 `|` 分隔（设置里的那一栏留空即可）：

```
socks5://127.0.0.1:10808|socks5://用户:密码@PROXY_HOST:PORT
```

**实测收益**：链路代理的出口是**粘性**的（连续 5 次都是同一个 IP），这正好治好了下面那个混合模式静默失败的毛病。同一个出口跑 http 模式，注册耗时从 25~51 秒降到 **14 秒**。

**注意**：短效代理通常 3 小时过期，过期后表现为 `upstream unavailable ... TimeoutError`，换新地址即可。

## 关键结论（实测得出，非推测）

1. **不需要浏览器。** `/fan2/verify/confirm` 服务端**不校验** reCAPTCHA Enterprise token，`captcha` 传空字符串即可通过。因此无需 Playwright、无需 relay、无需 Google 域名分流。
2. **必须调 `check-verification`。** 直接用邮件里的 token 提交 confirm 会得到 `{"error":"email not validated"}`。必须先用
   `GET /fan2/verify/check-verification?token=&artistId=&pageId=`（**三个参数缺一不可**，缺 pageId 会报 `no pageId in &token=...`），
   它返回一个 `emailValid:true` 的新 token，用这个 token 才能提交成功。
3. **响应即成功标志。** 成功后返回 `{"status":"OK"}`，与浏览器成功流程的响应一致。confirm 是幂等的，重复提交同样返回 OK。
4. **代理可直连。** curl_cffi 原生支持带认证代理，`http://user:pass@host:port` 直接可用，实测 4/4 成功，无需本地中转。

单账号全流程约 **6~14 秒**。

## 场次偏好

注册表单共 **11 个场次**，全部可选（三个下拉每个都列出全部 11 个），下方用紧凑标签网格展示全部场次并标出当前偏好序号 ①②③。

```
2027-05  Glasgow   UK      2027-08  Boston     US
2027-06  Manchester UK     2027-08  Las Vegas  US
2027-07  Munich     DE     2027-09  Slane      IE
2027-07  Barcelona  ES     2027-09  Knebworth  UK
2027-07  Amsterdam  NL
2027-07  Paris      FR
2027-07  Rome       IT
```

默认顺序 **Knebworth > Slane Castle > Glasgow**，三者必须互不重复。改选后立即写入配置。

## 布局说明（踩过的坑）

`qfluentwidgets` 的 `HeaderCardWidget.viewLayout` 是 **QHBoxLayout**，不是 QVBoxLayout。把多个控件直接 `addLayout` 进去会全部横向并排，把卡片最小宽度撑到 1781px（超过视口 1271px），表现为横向滚动条 + 内容显示不全。本项目统一用 `make_card()` 在卡片内嵌一个纵向 body 容器来规避。

表格：列宽**不硬编码**。`FlexTable` 每次填数据（以及窗口缩放时、用户还没手动拖过时）按内容 `resizeColumnsToContents()`，并把每列下限抬到"表头文字宽度 + 22px"保证表头永不被省略号截断；余量不强行摊派（摊派不是饿死表头就是撑宽数值列），留给操作者自己拖。所有列都是 `Interactive`，分隔线随时可拖。

控件尺寸：qfluentwidgets 默认 `PushButton` 是 120×34、`SpinBox` 的 `SpinButton` 固定 31×23 且两个并排——宽度 78px 时数字输入区可用宽度实测为 **0**。所以按钮统一压到 28px 高、9pt 字；数字输入改用 `CompactSpinBox` / `CompactDoubleSpinBox`（宽 84 时输入区 46px）。侧边栏展开宽度从默认 322px 收到 168px。

下拉：`ComboBox` 的弹出菜单默认带高度展开动画，动画期间行还用普通 delegate 绘制，看起来就是"先闪一下纯文本再变成选项"。`Combo` 子类改成 `MenuAnimationType.NONE` 一次性铺开。

线程安全：所有 worker 线程里碰控件的地方都必须经 `Bridge` 信号回主线程。早期版本在代理测试的子线程里直接 `InfoBar.success(...)`，产生的 toast 是无人拥有的顶层窗口，点不掉也关不掉。

当前指标（1320×840 窗口）：仪表盘内层宽度 = 视口宽度，内容高 676 < 视口高 791，**一屏放下不需要滚动**；1080×680 / 1200×720 / 1440×900 三种尺寸下六个页面均零溢出、无横向滚动条。

## 注册方式（三种，仪表盘上切换）

| 方式 | 做什么 | 单账号耗时 | captcha |
| --- | --- | --- | --- |
| **纯 HTTP** | curl_cffi 直发三个接口，无浏览器 | 6~15 秒 | 空字符串 |
| **混合** | 浏览器只打开一次页面产真 captcha，提交仍走 curl_cffi | 60~90 秒 | **浏览器产出的真 token** |
| **浏览器** | 真实 Chromium 全流程，页面内 `fetch` 提交 | 60~120 秒 | **浏览器产出的真 token** |

### captcha 到底校不校验（实测）

**抓包确认浏览器提交确实带 token**：拦截 confirm 请求，body 4685 字节，`captcha` 字段 2361 字符，前缀与 `grecaptcha.enterprise.execute` 的返回值逐字一致。

**然后做了三方对比**（各自独立的新会话 token）：

| 提交的 captcha | 服务端响应 |
| --- | --- |
| 真 token（浏览器产出，2425 字符） | `{"status":"OK"}` |
| 垃圾串（`"A"` × 2361） | `{"status":"OK"}` |
| 空字符串 | `{"status":"OK"}` |

**结论：提交时服务端对 captcha 不做任何校验**——连垃圾串都照收。所以：

- 空 captcha 不会在这个接口上被拒。ban 的风险从这个响应看不出来，也无法在这里证伪。
- 但如果服务端存在**异步打分**（站点有 `queue.openstage.live/fan1/telemetry` 上报），真 token 严格优于垃圾/空。混和模式就是为这个不确定性准备的：成本比全浏览器低，captcha 又是真的。
- curl_cffi 带真 token 是可行的（上表第一行就是 curl_cffi 提交的），token 不绑提交方的 TLS/IP。

### 浏览器方式的实现要点

- reCAPTCHA **只在表单渲染后才初始化**。直接打开 `/registration`（不带 token）时 `window.grecaptcha` 是 undefined，所以必须先把邮件里的链接打开，才取得到 token。
- transport 是本地 relay（`core/relay.py`）。**默认不分流**：Google 与注册流量走同一个上游。只有当上游屏蔽 Google、reCAPTCHA 加载不了时，才在「设置 → Google 分流代理」填一个能访问 Google 的代理。
  > 早期版本把分流目标硬编码成 `127.0.0.1:10808`（我开发机的地址），换台机器就是 `ConnectionRefusedError`，reCAPTCHA 永远加载不了。现在留空即不分流；填了但连不通会在 3 次失败后自动停用并提示，不会刷屏。
- 引擎按 `Playwright chromium（查盘）→ 系统 Chrome → 一次性下载` 的顺序挑选。**按文件系统探测而不是试启动**——裸启动会因为容器沙箱等原因失败，曾经因此误判"没浏览器"白下 150MB。
- 下载的浏览器落在 exe 同目录的 `browsers/`（`PLAYWRIGHT_BROWSERS_PATH`），整个文件夹可整体拷贝。
- 只随机 viewport / timezone / locale，**不伪造 UA**——浏览器是真的，改 UA 反而和实际引擎对不上。

### 并发与 relay

同一个上游代理只起**一个** relay，所有 worker 共用（`RelayPool` 按上游去重）。默认并发 32 条隧道、失败挂起 15 秒（不是 60 秒）。relay 的上游错误日志做了限流：前 3 次逐条打印，之后每 25 次汇总一次，不会把整轮日志淹没。

## 邮箱提供方：**Gmail 能用，Outlook/Hotmail 目前收不到信**

2026-09-16 实测，同一天同一代理同一时段：

| 邮箱 | verify/verify | 邮件到达 |
| --- | --- | --- |
| `KareemLank31@hotmail.com` | 200 OK | **9 分钟无** |
| `garyrangel2591@outlook.com` 等 3 个全新号 | 200 OK | **100 秒无** |
| `Paulineaude611@gmail.com` | 200 OK | **34 秒到** |
| `denisevach58@gmail.com` | 200 OK | **秒到，全程注册 25.4 秒** |

排除了这些可能：接口变了（用 Playwright 驱动真实表单抓包，请求体与我们发的**完全一致，且不带 captcha**）、IP 被限流（新加坡机房和美国住宅两个出口都不行）、代码问题（浏览器原生流程同样收不到）。

**结论：站点侧对微软域名不投递或严重积压**（页面自己也提示 "allow up to 1 hour"）。换 Gmail 即可。

> 排查期间我一度根据前端 bundle 里 `Iy({...captcha...})` 判断"verify 现在要 captcha"，**那是错的**——那段代码属于 landing2/closed 注册入口，不是 `/registration` 的邮箱步骤。以真实抓包为准。

## Web 管理界面

服务端带一个网页管理界面，**React 18 + Ant Design 5**，六个页面与桌面端一一对应
（仪表盘 / 邮箱池 / 代理池 / 预约记录 / 数据库 / 设置）。

```
oasis_gui/webui/            React 源码
  src/App.jsx               外壳：左侧菜单 + 顶栏 + 页面切换
  src/pages/*.jsx           六个页面
  src/api.js                接口封装 + 状态色板
  dist/                     构建产物（CI 产出，不入库）
```

### 为什么需要构建，以及部署时不需要

React 要 Node 才能编译，而**运行镜像里没有 Node** —— 这不是问题，因为构建发生在
CI 的多阶段构建里：

```
FROM node:22-alpine AS frontend   ← npm ci && npm run build
FROM playwright/python:...        ← 只 COPY --from=frontend 的 dist/
```

所以你服务器上 `docker compose pull` 拿到的镜像里已经有前端产物，
**不需要装 Node，也不需要编译**。

本机从源码跑（没有产物时）会看到一页说明，告诉你执行：

```bash
cd oasis_gui/webui && npm install && npm run build
```

改 UI 时用 `npm run dev`（Vite 起在 5174，把 `/api`、`/login` 代理到 8080），
cookie 和会话跟生产环境一样，改完即时热更新。

### 静态资源在认证之前放行

`/assets/*.js|css` 必须能匿名访问——**登录页自己就要加载这个 bundle**。
把它们放到认证之后，浏览器会拿 `index.html` 顶替 JS，登录页直接白屏
（这个 bug 我写出来过，测试才抓到）。

API 仍然全部受保护，实测未登录时 `/api/*` 一律 401。

资源路径做了穿越防护：`..`、`%2e%2e`、`..%2f` 等六种写法全部拒绝，
返回 SPA 外壳而不是上层的数据库文件。

### 配置只有一个来源：管理界面

界面里能配的东西**全部落盘到 `<数据库同目录>/oasis_config.json`**，重启不丢。
`.env` 只留两项：数据库路径和管理密码（密码不能在网页上改，鸡生蛋）。

代理池、线程数、场次偏好、各个超时、Google 出口、iCloud 服务地址与密码、调试
开关——都在网页上。`OASIS_*` 环境变量仍然可用，但只在配置文件里还没有值时作为
**首次启动的种子**写入，之后以文件为准，不会每次重启覆盖你在网页上改的东西。

桌面端和网页端**共用同一组配置键、同一套标签**，顺序也一致（桌面端多一个
`log_file`，那是它自己写日志用的；服务端日志走 stdout 交给 docker 收集）。
两边的设置页：

```
等邮件超时 · 成功后校验邮件 · 成功邮件超时 · 账号间停顿
取件代理 · iCloud 服务 · iCloud 密码
前置代理 · Google 分流代理 · 数据库路径 · 调试堆栈
```

线程数和场次偏好在两边的**仪表盘**上，代理池和账号各有自己的页面。

顺带清掉了三个已经没人读的键：`strict_egress`（原为 hybrid 比较 captcha 与提交
出口用，浏览器模式下两者天然同一个浏览器，不可能不一致）、`http_timeout`
（只服务 curl_cffi 会话）、`think_time` / `send_captcha`（随 hybrid 一起移除）。
`_page_client_ip` 也是定义了没人调用的死方法，一并删掉。

### 认证是强制的

界面能**启停引擎、看到账号池、导出凭据**，所以 `OASIS_WEB_PASSWORD` 没设时
服务会**直接拒绝启动**，而不是"开着但没锁"：

```
[error] OASIS_WEB_PASSWORD is not set - the admin UI exposes the account pool
        and can start/stop the engine, so it will not be served without a
        password. Set it in .env.
```

实现细节：

- 密码用 `hmac.compare_digest` **常量时间比较**，不留时序侧信道
- 登录成功发 `oasis_session` Cookie（`HttpOnly` + `SameSite=Lax`，12 小时）
- **未认证时所有 `/api/*` 一律 401**，`/` 只返回登录页——包括 `POST /api/start`
  和 `/api/export`，实测六个接口全部挡住
- 登录失败限流：同一实例 15 分钟内 6 次即 429

### 双重防护

`docker-compose.yml` 里端口已经绑在 `127.0.0.1`，公网访问不到：

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

要远程访问就用 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 user@服务器
# 然后本地开 http://127.0.0.1:8080
```

**不要把 8080 直接暴露到公网**——密码只是一层，界面上还有导出全部凭据的按钮。

### 运行时可调项会落盘

网页上改的线程数、模式、场次、超时会写进 `oasis_config.json`（默认在数据库
同目录，跟着 volume 持久化），**下一轮就生效，不用重启**。

优先级：`.env` 里**实际设置了**的变量在启动时写入文件；没设的沿用文件里的值。
也就是 `.env` 描述部署，网页描述这次运行。

### 接口

| 路径 | 说明 |
| --- | --- |
| `GET /` | 管理页面（未登录时是登录页） |
| `POST /login`、`/logout` | 会话 |
| `GET /api/state` | 统计 + 本机信息 + 配置 + 运行状态 |
| `GET /api/accounts?status=&page=` | 账号列表（每页 50） |
| `GET /api/registrations` | 预约记录 |
| `GET /api/proxies` | 代理池 + 健康度（`user:pass` **服务端遮罩**） |
| `POST /api/proxies` | 整体替换代理池，落盘并立即生效 |
| `POST /api/accounts/import` | 粘贴凭据行导入账号 |
| `GET /api/icloud/aliases` | 拉取 iCloud 别名列表，标记哪些已导入 |
| `POST /api/icloud/import` | 只导入勾选的别名 |
| `GET /api/log?since=` | 增量日志（环形缓冲 2000 行） |
| `POST /api/start`、`/api/stop` | 启停 |
| `POST /api/config` | 改配置并落盘 |
| `POST /api/accounts/reset`、`/api/accounts/delete` | 重置 / 清理 |
| `POST /api/vacuum` | SQLite 压缩 |
| `GET /api/export?what=creds\|accounts` | 导出 |
| `GET /health`、`/stats` | 给监控用的极简探针 |

### 代理池和邮箱都在网页上配

不需要改 `.env` 重启。代理池存进 `oasis_config.json`（跟桌面端一样的地方），
邮箱直接进数据库：

- **代理池页**：一个多行输入框，粘贴后「保存并生效」——`pool.load()` 原地替换，
  引擎持有的引用不用动，**下一轮就生效**
- **邮箱池页**：粘贴凭据行导入，支持 Outlook / Gmail / iCloud 三种格式，
  自动去重（重复的行计入 `duplicate`）

`OASIS_PROXIES` 只在配置文件里还没有代理时做**种子**。一旦你在网页上改过，
就以文件为准——否则每次重启都会把你改的覆盖回环境变量。

### 预约记录里的场次是翻译过的

`registrations.poll_answer_ids` 存的是场次的 **poll uuid**，对人没有意义。服务端
用 `registrar.SHOWS` 翻译成场馆名再返回：

```
"shows": "2027-05 | Glasgow | UK > 2027-06 | Manchester | UK > 2027-07 | Paris | FR"
```

翻译放服务端而不是前端，是为了避免在页面里硬编码 uuid——那迟早会和
`registrar.SHOWS` 对不上。

### iCloud 别名要手动勾选，不全导

iCloud 建别名有配额（约每小时 10 个），全量导入会把已经用掉的别名一起塞进队列。
所以邮箱池页有独立的 iCloud 面板：

1. 「拉取别名列表」→ 调 `hme_catalog()`，列出服务上所有别名
2. 每个别名显示**启用/停用**、所属账号、标签，以及**是否已在池中**
3. 已导入的置灰不可选，另有「只选未导入的」一键勾选
4. 「导入勾选项」→ 只为选中的那些生成 `别名----acc_xxx----hme` 并入库

导入时**服务端重新拉一次目录**，用服务返回的 `account_id` 构造凭据行，而不是
信任页面传来的 id——页面只能影响"导哪些"，改不了"导成什么"。

服务连不上或密码不对时，错误直接回显到界面上，并且区分了三种情况：

```
iCloud 服务密码未配置（设置页的 iCloud 密码，或环境变量 ICLOUD_HME_ADMIN_PASSWORD）
连不上 iCloud 服务 http://127.0.0.1:8081（URLError）——容器里要用 host.docker.internal，不是 127.0.0.1
iCloud 服务登录失败（HTTP 401）
```

### 停止是服务级状态，不只是停引擎

`engine.stop()` 只让 worker 跑完当前账号就退出。但服务的外层循环发现队列里还有
待注册账号，**会立刻开新一轮**——表现就是"点了停止但没停"。

所以停止做成两层：`PAUSED` 事件挡住外层循环，再调 `engine.stop()` 结束当前轮。
点「启动」才清除它。

实测：点停止后 `rounds` 停在 1，等 40 秒也没有自开新一轮；点启动后回到 `running`
并真的开下一轮。状态依次是 `running`（worker 正在收尾）→ `paused`。

### 失败退避

一轮里**没有任何注册成功、却有账号失败**，基本是传输层问题（代理死了 / 出口不通 /
被站点拦），不关账号的事。原来循环会一路空转，几秒钟就能把整个队列刷成 failed。

现在会在这种情况下暂停 60 秒再试，日志里给出提示。点停止时不进这个等待，
免得界面卡在 `idle` 一分钟才显示 `paused`。

### 没有代理也能启动

代理池既然能在网页上配，服务就不再因为 `OASIS_PROXIES` 为空而拒绝启动，
只记一条警告。但**队列里有活而代理池为空时会等待而不是开跑**——否则会把待注册
账号全部标成失败，而原因跟账号毫无关系。

### 代理凭据不会出现在网页上

页面拿到的地址是 `socks5://***:***@host:port`，真实密码只在服务端。这里有个
容易踩的坑：**如果用户不动输入框直接保存，密码会被 `***` 覆盖掉**。所以保存时
服务端会把遮罩形式还原回原行：

```python
known = {mask_url(e["url"]): e["url"] for e in self.pool.snapshot()}
resolved = [known.get(mask_url(ln), ln) for ln in lines]
```

实测：原样保存后配置文件里仍是 `socks5://realu:realp@10.0.0.1:1080`；改一行、
加一行时，没动的那行凭据也保住了。

遮罩是**服务端做**的，不是前端——否则密码会随每个页面加载走一遍网络，
也会进访问日志。`/api/state` 里的 `config.proxies` 只返回数量，不返回内容。

## iCloud 隐藏邮箱（HME）接入

对接本地部署的 iCloud Hide-My-Email 服务（默认 `http://127.0.0.1:8081`）。

**凭据格式（三段）：**

```
别名@icloud.com----acc_b865bbe0----hme
```

- 第 1 段是**别名地址**（站点看到的收件人）
- 第 2 段是 HME 的 `account_id`（`GET /api/accounts` 拿）
- 第 3 段固定 `hme`，用来和微软/Google 的格式区分

导入时协议选 `imap` / `graph` 都无所谓——`@icloud.com` 会被自动路由到 `HmeMailbox`。

**配置**（环境变量，或写进凭据）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ICLOUD_HME_BASE` | `http://127.0.0.1:8081` | 服务地址 |
| `ICLOUD_HME_ADMIN_PASSWORD` | 空 | 服务启动时设的 `ICLOUD_HME_ADMIN_PASSWORD` |

### ⚠️ 这个邮件必须读 `body_html`，不能读 `body`

Oasis 的验证邮件把链接放在**按钮的 href** 里，纯文本版只有 "click the button above"：

```
body     长度 236   -> 没有链接
body_html 长度 10479 -> href 里是完整链接
```

`HmeMailbox` 因此对每封邮件都会拉一次详情拿 `body_html`——只信 `preview` 或 `body` 的话会一直等到超时。

### 与服务交互的几个要点

- **读接口不需要 CSRF**，只要会话 Cookie；登录一次覆盖整个 TTL，401 时才重登
- 会话在服务端内存里，**服务重启就要重新登录**（`HmeMailbox` 会自动重登一次）
- 服务不可达时抛 `MailAuthError: HME service unreachable`，不会把异常漏到引擎外
- `access_token()` 直接返回 `None`——这里没有 bearer token，Cookie 就是凭据

## Gmail 支持（OAuth2 + IMAP）

Gmail 没有 Graph 通道，凭据自带 `client_secret`，所以**导入格式多一段**：

```
微软 4 段：user@outlook.com----password----client_id----refresh_token
Gmail 5 段：user@gmail.com----password----client_id----client_secret----refresh_token
```

按**段数**识别，混批导入也没问题。4 段的 Gmail 行会被明确拒绝（Google 不给刷新，缺 secret 只会拿到 401，不如当场报错）：

```
x@gmail.com: Gmail needs 5 fields (email----password----client_id----client_secret----refresh_token)
```

流程与微软侧同构，只是换了端点和作用域：

| | 微软 | Google |
| --- | --- | --- |
| token 端点 | `login.microsoftonline.com/consumers/oauth2/v2.0/token` | `oauth2.googleapis.com/token` |
| 作用域 | `https://outlook.office.com/IMAP.AccessAsUser.All` | `https://mail.google.com/` |
| IMAP | `outlook.office365.com:993` | `imap.gmail.com:993` |
| 垃圾夹 | `Junk`（`\Junk` 属性发现） | `[Gmail]/Spam`（`\Spam` 属性发现，同一套逻辑） |

`client_secret` 存在 `accounts.client_secret`（旧库自动迁移）。协议下拉对 Gmail 不生效——它没有 Graph，选什么都走 Gmail 读取器。

**注意取件网络**：沙箱里 Gmail IMAP 直连不通，需要设「取件代理」。你那边如果 Gmail IMAP 也走不通（Gmail 在国内常被阻断），同样要在设置里填一个能访问 Google 的代理，例如 `socks5://127.0.0.1:1080`。

## 取件协议（导入时按批次指定）

**两种方式的前提完全相同：先用 refresh_token 换 access_token，再拿 access_token 取件。** 换取逻辑只有一份——`BaseMailbox.access_token()`，两个子类只覆盖 `scope`：

```
refresh_token ──(token endpoint + scope)──> access_token ──> 取件
```

| 协议 | scope | 取件方式 |
| --- | --- | --- |
| **Graph API** | `https://graph.microsoft.com/.default` | `GET graph.microsoft.com/v1.0/me/messages` |
| **IMAP** | `https://outlook.office.com/IMAP.AccessAsUser.All` | `outlook.office365.com:993` + `AUTH XOAUTH2` |
| **自动**（默认） | 先 Graph，被拒或取不到就换 IMAP | 前一半预算给 Graph，后一半给 IMAP，轮换后的 refresh_token 随切换一起带过去 |

**重要：client_id 决定 Graph 能不能用。** 实测两个 client_id：

```
client 2cee05de-…  graph token 兑换 OK  →  graph /me  401 UnknownError   （无 Graph 邮件权限）
client 9e5f94bc-…  graph token 兑换 OK  →  graph /me  OK
```

`2cee05de` 只能走 IMAP。**导入时选错协议 = 每个账号白等满 300 秒超时**。所以：

- 默认协议改成 **自动**，不再默认 Graph。
- Graph 收到 401/403 会**立即**上抛（`MailAuthError`），不再轮询到超时；自动模式下随即切 IMAP。实测同一账号：修复前 300s 超时失败，修复后 **10.8s 成功取到链接**。

因为凭据完全一样，同一个 refresh_token 两种受众都能兑，所以某个账号在一条通道上取不到时可以换另一条。access_token 不能跨协议复用（scope 不同），所以缓存按实例分开。

IMAP 实测要点（都是踩过的）：

- scope 必须写 **outlook.office.com**；`outlook.office365.com/IMAP.AccessAsUser.All` 会被拒（`AADSTS70011 invalid_scope`）。MSA 传统 scope `wl.imap` 同样被拒。
- 拿到的 access token 是**不透明字符串**（不是 JWT），这是消费级账号的正常表现。
- **必须搜 Junk 夹。** Outlook 会把相当一部分验证邮件直接投进 Junk，只搜 INBOX 会静默漏掉，最后报成"邮件没到"。文件夹名是本地化的（`Junk` / `垃圾邮件`），所以从 IMAP `LIST` 的 `\Junk` 属性里发现，不硬编码。实测同一账号：只搜 INBOX 3 封、搜 INBOX+Junk 6 封，链接就在 Junk 里。
- **取件默认直连，不走代理。** 代理是为了让注册流量看起来像美国家庭宽带，跟微软通信不需要。走代理既慢又多一个故障源——"verification mail never arrived" 常常就是这么来的。要强制走代理就在「设置 → 取件代理」填一行；留空即直连。实测直连 IMAP 建连 0.9 秒、走 SOCKS 2.3 秒。
- 取件失败**不再静默**：连接/认证错误会打进日志（`mail fetch failed: ...`），超时也会附上最后一次错误，不会再拿"邮件没到"糊弄。
- Outlook 会间歇性对完全正常的 token 回 `User is authenticated but not connected`，连接握手已带 4 次退避重试并强制刷新 token。
- 解析正文时 `msg.walk()` 是**生成器**，必须先 `list()`；否则 text/plain 那一轮就把它耗尽，所有 multipart 邮件都会解成空串。
- Graph 对部分 `outlook.com` 账号会返回 **401 `UnknownError`**（token 兑换成功但 Graph 拒绝该 token）。这批账号用 IMAP 或「自动」协议即可；YM 的库里全部是 imap，不受影响。

## 每次运行随机化的内容

- 身份：名字、姓氏、生日（18~58 岁）
- **地区：美国 / 德国 / 法国**（`identity.SUPPORTED_COUNTRIES`）
- **地址跟随代理出口国家**：每次拿到代理后先查它的出口 IP 归属国（`registrar.exit_geo()`，通过该代理请求 `ip-api.com`，所以看到的是出口地址本身），然后用对应国家的城市生成身份。出口不是这三个国家时退回随机，并在日志里说明。
- 电话按国家格式生成：US 用真实 NANP 区号（排除 555/8xx/9xx）；DE 用 015x/016x/017x 移动号段（10 位）；FR 用 06/07 移动号段（9 位）。都按 E.164 国家号码返回（无前导 0），配对应的 `countryCallingCode`。
- 传输指纹：每次会话从 chrome131/136/142/145/146/150、edge101、firefox135/144、safari180/184/260 中随机取一个
- 代理：从代理池按使用量最少优先分配

出口国家查询结果按代理缓存（`engine._geo_cache`），一个出口只查一次，不会每个账号都请求 ip-api。

```
[info] exit 99.13.71.88 -> US (identities for this proxy will use US addresses)
[info] w1 identity Kayla Miller / 2096514529 / Pittsburgh
```

## 去重（由 SQLite 约束保证，不靠 Python 侧检查）

| 约束 | 效果 |
| --- | --- |
| `accounts.email UNIQUE` | 同一邮箱只入队一次 |
| `accounts(first_name,last_name,phone) UNIQUE` | 同一身份只用一次（身份列为 NULL 时互不冲突） |
| `registrations(account_id,artist_id) UNIQUE` | 每个账号对同一场次只能预约一次 |

## 安装

```bash
sudo apt-get install -y python3-pyqt6.qtsvg        # Debian 把 QtSvg 拆包，缺它 qfluentwidgets 起不来
pip install -r requirements.txt --user --break-system-packages
```

## 运行

```bash
./run.sh
# 或
python3 app.py
```

## exe 分发：浏览器**不需要**用户装，但首次运行要下载

**exe 自带 Playwright 驱动，不自带浏览器本体。**

| 组件 | 是否打进 exe | 说明 |
| --- | --- | --- |
| Python 运行时 + 全部依赖 | ✅ 自带 | 单文件，约 87MB |
| Playwright Python 包 | ✅ 自带 | `collect_all("playwright")` |
| **Playwright 驱动**（`node.exe` + `cli.js`） | ✅ 自带 | TOC 里 949 个条目，实测可跑 |
| **Chromium 浏览器本体** | ❌ **不带** | 首次用到时下载 |

所以用户端**不需要预装 Playwright**，也不需要 Node。但**首次进入混合/浏览器模式时会自动下载浏览器**，实测在 Wine 里下完是 **432MB**（chromium + ffmpeg）：

```
no browser found - downloading Playwright chromium (one time, ~150 MB) into ...
browser engine: Playwright chromium (...chrome-win64\chrome.exe)
```

### 下载落在哪里

- **打包后的 exe**：`<exe 所在目录>\browsers\`（便携，用户看得见）
- **源码运行**：`%LOCALAPPDATA%\ms-playwright`（Windows 平台默认位置）
- 用户若自己设了 `PLAYWRIGHT_BROWSERS_PATH`，以用户的为准

**这里修过一个 bug**：旧代码只在「下载子进程」的环境里设了 `PLAYWRIGHT_BROWSERS_PATH`，主进程的查找函数看不到，于是**每次启动都会重新下载一遍**。现在路径决策集中在一处，查找和下载必然一致，并且把环境变量导出给 Playwright 自己的 `launch()`（否则它也会去别处找）。

### 想免掉首次下载？

把浏览器一起打进 exe 也可以，代价是体积从 87MB 涨到约 **300MB+**。做法是在打包前把 `browsers/` 目录作为 datas 收进去，并确保 `PLAYWRIGHT_BROWSERS_PATH` 指向解包后的临时目录。目前没这么做——一次性的 400MB 下载对绝大多数用户是可接受的。

## 打包成 Windows 单文件 exe

已构建产物：`dist/OasisConsole.exe`（PE32+ x86-64 GUI，约 87 MB，单文件，含两种注册方式）。

**文件全部落在 exe 所在目录**（`oasis_config.json` / `oasis.db` / `oasis_run.log` / 导出的 CSV，浏览器方式还会在 `browsers/` 下放 chromium）。这点必须靠代码保证：PyInstaller onefile 模式下 `__file__` 指向临时解压目录（退出即删），所以 `app.app_dir()` 在 frozen 时改用 `sys.executable` 的目录。

两种构建方式：

```bat
REM Windows 本机（需 Python 3.12 64 位）
build_windows.bat
```

```bash
# Linux 交叉构建（Wine + Windows Python，脚本自动装好前缀与依赖）
./build_windows_wine.sh
```

构建要点（都是踩过的）：

- **PyQt6 锁 6.6.1**。PyInstaller 打包时必须真的导入每个模块，而 Qt 6.11 的 DLL 在 Wine 9 下加载不了（`Qt6Core.dll` 直接报缺依赖，wine 缺较新的 Windows API）；6.6.x 正常，在真实 Windows 上同样是稳定版。
- `qfluentwidgets` 与 `curl_cffi` 必须 `collect_all`，否则运行时报缺 qss/svg 资源或缺 libcurl。`playwright` 也要 `collect_all`——它带的是 node driver（`driver/node.exe` + `driver/package/cli.js`），漏了浏览器方式就没法启动，而浏览器二进制是首次使用时才下载的。
- `socks` 是 `mailbox.open_tunnel()` 里的**惰性导入**，静态分析看不到，得写进 `hiddenimports`。
- 排除列表**不能排** `QtXml` / `QtMultimedia` / `QtMultimediaWidgets` —— qfluentwidgets 实际用到了（`common/icon.py` 就 import 了 QtXml）。
- 用 `nohup ... &` 后台跑 wine 会因 stdio 句柄失效直接崩（`init_sys_streams: WinError 6`），构建要在前台跑。

## 界面

| 页面 | 用途 |
| --- | --- |
| 仪表盘 | 六项统计卡一行、线程数与间隔、注册方式、开始/停止、进度、11 个场次偏好与状态标签、最近成功、**就绪状态提示** |
| 邮箱池 | 粘贴/文件导入凭据、取件协议选择、凭据校验、账号列表、**重置失败与卡住项**、**按状态清理**、导出凭据 |
| 代理池 | 代理列表编辑、批量测出口 IP、健康状态（成功/失败/冷却/最近错误） |
| 数据库 | 预约记录与账号身份两张表、导出全量 CSV |
| 日志 | 分级着色的实时控制台、自动滚动、导出；同时写 `oasis_run.log` |
| 设置 | captcha 字段、各类超时、数据库与日志路径、调试堆栈、主题 |

### 表格显示上限 vs 导出

表格只渲染最近 **500** 行（8000+ 行全渲染没有意义，卡的是界面），卡片标题会写明「共 N 条，显示最近 500」。**导出不受这个上限影响**，CSV 与凭据导出都是全量。

> 早期版本这里有个 bug：`accounts()` / `registrations()` 默认 `limit=500`，导出直接复用了带默认值的调用，于是 CSV 只出 500 条。现在这两个方法默认 `limit=None`（全量），只有表格视图显式传 `DISPLAY_LIMIT`。

### 清理

「邮箱池」右下角四个按钮按状态删除：已注册 / 失败 / 待注册 / 全部。删除前弹确认框并显示条数，删除后自动 `VACUUM` 回收文件空间。账号删除时预约记录通过外键 `ON DELETE CASCADE` 一并删除。

「重置失败与卡住项」把 `failed` 和 `running` 一起放回 `pending`——`running` 是中断的运行留下的残留，不会被自动回收。

### 字体

不覆盖任何字体。字体大小全部交给 qfluentwidgets 的标签类（`SubtitleLabel` 20pt / `BodyLabel` 14pt / `CaptionLabel` 12pt 等），表格行高由字体度量算出（`fm.height() + 9`）而不是写死像素。此前给按钮和表格硬设的 9pt 其实与应用默认字体（9pt）相同，属于无效改动；真正有影响的是日志控制台的 JetBrains Mono 和统计卡/横幅的手写字号，现已移除。


## 文件结构

```
oasis_gui/
  app.py                  Fluent UI 主程序（六个页面 + 线程信号桥）
  run.sh                  源码方式启动
  build_windows_wine.sh   Linux 下用 Wine 交叉打包 exe
  build_windows.bat       Windows 下打包
  OasisConsole.spec       PyInstaller 配置
  core/
    registrar.py          curl_cffi 注册核心（三步流程、场次常量、指纹池）
    engine.py             线程池调度、身份唯一性、代理租约、落库
    browser_registrar.py  Playwright：常驻共享浏览器、captcha、浏览器模式
    relay.py              本地 HTTP relay：链式代理、SOCKS5 上游、Google 分流
    store.py              SQLite，去重约束与写事务安全
    mailbox.py            Graph / IMAP / Gmail 收信，token 轮换持久化
    proxy_pool.py         代理解析、健康计数、失败冷却
    identity.py           随机身份生成（美/德/法）
    config.py             配置持久化
  oasis_config.json       运行时生成
```

## 注意事项

- Microsoft 每次刷新都会**轮换 refresh_token**，程序会把新 token 写回数据库；丢失它等于丢失邮箱。
- 住宅上游代理会偶发断隧道（`SSL_connect: Connection closed abruptly`），HTTP 层已带 5 次退避重试；代理连续失败 3 次进入 45 秒冷却。
- `creds/*.txt` 内含明文密码，注意权限。
