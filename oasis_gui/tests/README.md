# 回归测试

四个探针，都直接跑真实代码路径（不是「能 import 就算数」）。前三个离线，
最后一个会连一次真实 IMAP。

```bash
cd oasis_gui
python3 tests/test_hitcheck.py           # 判定链路（假收件箱）
python3 tests/test_merge.py              # 旧记录并入中签名单
python3 tests/test_search_fallback.py    # 粗筛被服务端拒绝时退回全量
python3 tests/test_opted.py              # 检测范围（已预约）的推断与标记
QT_QPA_PLATFORM=offscreen python3 tests/test_desktop.py   # 桌面版渲染
python3 tests/test_live_sweep.py         # 轮次预算 / 单账号超时（联网）
```

| 文件 | 覆盖什么 | 联网 |
| --- | --- | --- |
| `test_hitcheck.py` | 成功邮件 → 中签；**验证信不算中签**；基线之前的旧信不算；别家邮件不算；`confirm OK 无邮件` 与页面确认并入 `site-ok`；读信失败只记错误；同收件箱别名复用一条连接；第二轮不改变已有结论 | 否 |
| `test_merge.py` | 旧库首次打开时的合并：`registered` → `success-mail`，`submitted` 与 `confirm answered OK but the success mail never arrived within 180s`（含只落在 `registrations.response` 的情形）→ `site-ok`；真正的失败不并入；重复打开不改结论；读信失败不抹掉已成立的中签 | 否 |
| `test_search_fallback.py` | 服务端拒绝粗筛（IMAP 的 OR / Graph 的 `$filter`）时抛出的是明确的失败而不是空邮箱；不加筛选照常读到信；monitor 收到之后退回全量、账号照样判中签 | 否 |
| `test_opted.py` | 旧库打开时把「预约过」的证据全翻出来（registered / submitted / 那条 confirm OK / 有预约行），纯待办与失败的不误标；`check_targets` 只含已预约；一键标记与单条取消；导入即标记；统计口径自洽（checked + unchecked = opted − hits）；导入行解析对数组与文本都给干净邮箱 | 否 |
| `test_desktop.py` | Fluent 窗口真的建起来、五张页面各自 refresh、统计卡与表格渲染出行、按钮与文案是检测语义 | 否 |
| `test_live_sweep.py` | 一个读不通的邮箱落在轮次预算内收尾，且不推进 `checked_at`（下一轮仍排最前） | 是，两个账号 |

`test_hitcheck.py` 里有一条断言值得单独说：它记录每次读信有没有要求「服务端只回
Oasis 的信」，并断言**首次检测一个账号走全量、之后才走粗筛**。那正是「结果可能
早就发过了」这个场景的保险。

`test_live_sweep.py` 与 `test_desktop.py` 需要一份账号池（默认找
`../oasis.db`），找不到就跳过并返回 0，所以在没有数据的机器上也能整套跑完。
