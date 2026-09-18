# 回归测试

四个探针，都直接跑真实代码路径（不是「能 import 就算数」）。前三个离线，
最后一个会连一次真实 IMAP。

```bash
cd oasis_gui
python3 tests/test_hitcheck.py        # 判定链路（假收件箱）
python3 tests/test_merge.py           # 旧记录并入中签名单
QT_QPA_PLATFORM=offscreen python3 tests/test_desktop.py   # 桌面版渲染
python3 tests/test_live_sweep.py      # 轮次预算 / 单账号超时（联网）
```

| 文件 | 覆盖什么 | 联网 |
| --- | --- | --- |
| `test_hitcheck.py` | 成功邮件 → 中签；**验证信不算中签**；基线之前的旧信不算；别家邮件不算；`confirm OK 无邮件` 与页面确认并入 `site-ok`；读信失败只记错误；同收件箱别名复用一条连接；第二轮不改变已有结论 | 否 |
| `test_merge.py` | 旧库首次打开时的合并：`registered` → `success-mail`，`submitted` 与 `confirm answered OK but the success mail never arrived within 180s`（含只落在 `registrations.response` 的情形）→ `site-ok`；真正的失败不并入；重复打开不改结论；读信失败不抹掉已成立的中签 | 否 |
| `test_desktop.py` | Fluent 窗口真的建起来、五张页面各自 refresh、统计卡与表格渲染出行、按钮与文案是检测语义 | 否 |
| `test_live_sweep.py` | 一个读不通的邮箱落在轮次预算内收尾，且不推进 `checked_at`（下一轮仍排最前） | 是，两个账号 |

`test_live_sweep.py` 与 `test_desktop.py` 需要一份账号池（默认找
`../oasis.db`），找不到就跳过并返回 0，所以在没有数据的机器上也能整套跑完。
