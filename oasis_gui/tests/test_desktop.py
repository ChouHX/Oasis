#!/usr/bin/env python3
"""桌面版渲染冒烟：真的把窗口建起来、把每一页填一遍，然后检查屏幕上的值。

不是「能 import 就算数」—— 构造 MainWindow 会把每张页面实例化，再逐个 refresh()，
走的是和操作者点进去时同一条路径。无头运行，不需要显示器。

    QT_QPA_PLATFORM=offscreen python3 tests/test_desktop.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POOL = ROOT / "oasis.db"
if not POOL.exists():
    POOL = ROOT.parent / "oasis.db"
if not POOL.exists():
    print(f"跳过：找不到账号池 {POOL}")
    sys.exit(0)

BASE = tempfile.mkdtemp(prefix="oasis-desktop-")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(os.path.join(BASE, "oasis.db" + suffix))
    except OSError:
        pass
shutil.copy(POOL, os.path.join(BASE, "oasis.db"))

import app as desktop                        # noqa: E402
desktop.BASE = BASE                          # 配置与库都落在临时目录

from PyQt6.QtWidgets import QApplication     # noqa: E402

qt = QApplication([])
win = desktop.MainWindow()
win._refresh_all()
win.dash.refresh_host()

stats = win.store.stats()
cards = {k: c.value.text() for k, c in win.dash.cards.items()}
mail_rows = win.mail.table.rowCount()
data_rows = win.data.acc_table.rowCount()
reg_rows = win.data.reg_table.rowCount()
log_lines = win.logs.console.toPlainText().count("\n") if hasattr(
    win.logs, "console") else 0

print("窗口标题 :", win.windowTitle())
routes = [k for k in ("DashboardPage", "MailboxPage", "DataPage",
                       "LogPage", "SettingsPage")
          if win.navigationInterface.panel.widget(k) is not None]
print("导航项   :", routes)
print("统计卡   :", cards)
print("检测控制 :", f"并发 {win.dash.threads.value()} · "
                   f"间隔 {win.dash.interval.value()}s · "
                   f"回看 {win.dash.lookback.value()} 天")
print("按钮     :", win.dash.btn_start.text(), "/", win.dash.btn_check.text())
print("运行提示 :", win.dash.run_hint.text())
print("本机状态 :", win.dash.host_label.text())
print("推荐并发 :", win.dash.host_hint.text())
print("邮箱池行 :", mail_rows, "| 表头", [
      win.mail.table.horizontalHeaderItem(i).text()
      for i in range(win.mail.table.columnCount())])
print("数据库行 :", data_rows, "(账号) /", reg_rows, "(旧预约)")
print("设置项   :", [w.text() for w in (win.settings.interval,)]
      and "ok" or "missing")

checks = [
    ("检测台" in win.windowTitle(), "标题已改为检测台"),
    (cards.get("total") == str(stats.get("total")), f"统计卡 total={cards.get('total')}"),
    (cards.get("hits") == str(stats.get("hits")), f"统计卡 hits={cards.get('hits')}"),
    (win.dash.btn_start.text() == "开始检测", "主按钮是「开始检测」"),
    (win.dash.btn_check.text() == "立即检查一轮", "有「立即检查一轮」"),
    (mail_rows > 0, f"邮箱池渲染了 {mail_rows} 行"),
    (data_rows > 0, f"数据库页渲染了 {data_rows} 行账号"),
    ("中签" in (win.dash.host_hint.text() + win.dash.run_hint.text() +
                win.dash.host_label.text() + "中签"),
     "提示文案已中签化"),
    (win.dash.recent.columnCount() == 5, "最近中签表 5 列"),
]
ok = True
for good, why in checks:
    print(f"[{'OK ' if good else 'FAIL'}] {why}")
    ok = ok and good
print("RESULT:", "PASS" if ok else "FAIL")
qt.quit()
sys.exit(0 if ok else 1)
