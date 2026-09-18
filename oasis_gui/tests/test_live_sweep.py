#!/usr/bin/env python3
"""护栏回归：一个不可达的邮箱不能再把整轮拖住。

这条规则是实测逼出来的：一个直连不通的 Gmail 账号曾把整轮巡检拖住 8.5 分钟
（token 重试嵌在握手重试里，两笔预算相乘）。现在它必须落在「轮次预算」和
「单账号超时」之内，并且**不推进 checked_at** —— 卡住的账号下一轮仍排在最前面。

需要一台真能读信、同时也有读不通地址的机器，所以默认对着 ROOT/oasis.db 跑；
有账号池也可以显式指定：

    python3 tests/test_live_sweep.py [oasis.db]

会真的连一次 IMAP，但只连两个账号。
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.monitor import HitMonitor          # noqa: E402
from core.store import Store                # noqa: E402

def default_pool():
    """账号池的常见落点：源码 checkout 里它在仓库根，部署里它在 oasis_gui/。"""
    for candidate in (ROOT / "oasis.db", ROOT.parent / "oasis.db"):
        if candidate.exists():
            return candidate
    return ROOT / "oasis.db"


SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_pool()
if not SRC.exists():
    print(f"跳过：找不到账号池 {SRC}")
    sys.exit(0)

# 一个能读的、一个读不通的。第一类里挑只有验证信的账号，第二类优先 Gmail
# （Google 在多数国内网络上直连不通）。
READABLE = "joyceplaisted2634@outlook.com"
UNREACHABLE = "robertlaro84@gmail.com"

work = tempfile.mkdtemp(prefix="oasis-live-")
DB = os.path.join(work, "oasis.db")
shutil.copy(SRC, DB)

store = Store(DB)
real_targets = store.check_targets
emails = {r["email"] for r in store.accounts()}
picked = {e for e in (READABLE, UNREACHABLE) if e in emails}
if len(picked) < 2:
    print(f"跳过：账号池里没有预期的那两个样本（找到 {sorted(picked)}）")
    sys.exit(0)


def limited(limit=None, skip_hits=True, only=None, only_opted=True):
    # 这个桩自己指定两个样本，所以 opted 的筛选在这里不参与 —— 但签名要跟上，
    # 否则 monitor 一传 only_opted 就 TypeError。
    return [r for r in real_targets(skip_hits=skip_hits, only=only,
                                    only_opted=False)
            if r["email"] in picked]


store.check_targets = limited
seen = []
mon = HitMonitor(store, lambda lvl, msg: seen.append((lvl, msg)),
                 lambda kind, payload=None: None,
                 {"threads": 2, "lookback_days": 30, "per_page": 5})
mon.sweep_budget = 40.0

t0 = time.time()
mon.sweep()
elapsed = time.time() - t0

rows = {r["email"]: r for r in store.accounts()}
read = rows[READABLE]
stuck = rows[UNREACHABLE]
for lvl, msg in seen:
    print(f"  [{lvl}] {msg}")

checks = [
    (elapsed < 90, f"一轮在预算内收尾（实际 {elapsed:.0f}s，此前实测 8.5 分钟）"),
    (read["checked_at"] is not None, "能读的账号推进了 checked_at"),
    (stuck["checked_at"] is None, "读不通的账号不推进 checked_at（下一轮仍优先）"),
    (bool(stuck["check_error"]), f"读不通的账号记了原因：{stuck['check_error']}"),
]
ok = True
for good, why in checks:
    print(f"[{'OK ' if good else 'FAIL'}] {why}")
    ok = ok and good
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
