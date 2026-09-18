#!/usr/bin/env python3
"""Oasis Live '27 中签检测台。

A Fluent desktop front-end over the hit monitor:

  * 账号池导入与凭据校验
  * 定时把每个账号的收件箱翻一遍，看 Oasis 有没有来信
  * 中签名单：邮件证据与「站点已确认但成功邮件没来」的旧记录合并成同一张表
  * live log with per-level colouring, plus a log file
  * SQLite persistence, with the de-duplication rules enforced in the schema

程序不再向站点提交任何东西 —— 活动结束、注册环节已整体移除（见 README）。
这里唯一的动作是读邮箱。

Layout notes: HeaderCardWidget.viewLayout is a QHBoxLayout, so stacking content
directly into it puts everything side by side and blows the minimum width past
the viewport. Every card here therefore gets an inner vertical body widget via
make_card(). Rows are 25px and the dashboard fits one screen without scrolling.

Run:  python3 app.py
"""
import csv
import os
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFontMetrics, QTextCursor
from PyQt6.QtWidgets import (QApplication, QFileDialog, QFrame, QGridLayout,
                             QHBoxLayout, QTableWidgetItem, QTextEdit,
                             QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CaptionLabel,
                            CompactSpinBox, ComboBox, FluentIcon as FIF,
                            FluentWindow, HeaderCardWidget, InfoBar,
                            InfoBarPosition, LineEdit, MenuAnimationType,
                            MessageBox, NavigationItemPosition,
                            PrimaryPushButton, ProgressBar, PushButton, ScrollArea,
                            SimpleCardWidget, SubtitleLabel,
                            SwitchButton, TableWidget, TextEdit, TitleLabel,
                            setTheme, setThemeColor, Theme)

from core import hitcheck
from core import mailbox
from core import sysinfo
from core.config import Config
from core.monitor import HitMonitor
from core.store import Store

def app_dir():
    """Directory the app reads and writes beside.

    Under a PyInstaller onefile build, __file__ points into the temporary
    extraction directory that is deleted on exit, so config, database and logs
    would vanish with it. sys.executable is the real .exe path, which is where
    the operator expects those files to land.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE = app_dir()

PAD = 14          # page content margin
GAP = 9           # spacing between cards
# Rows rendered in the table views. Exports deliberately ignore this.
DISPLAY_LIMIT = 500

# No per-column widths are hard-coded: each table measures its own content and
# every divider stays draggable. These bounds just stop one long value (a JSON
# blob, a 60-char error) from monopolising the row.
MIN_COL = 52
MAX_COL = 340


class FlexTable(TableWidget):
    """Compact table that sizes columns to content and lets them be dragged.

    On every fill (and on window resize, until the operator drags something) the
    columns are measured from their real content, clamped, and any leftover
    width is handed to the widest column so the table has no dead space.
    """

    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.setBorderVisible(True)
        self.setBorderRadius(6)
        self.setWordWrap(False)
        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        # row height follows the app font instead of a hardcoded pixel value
        fm = QFontMetrics(self.font())
        self.verticalHeader().setDefaultSectionSize(max(24, fm.height() + 9))
        hh = self.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setFixedHeight(max(28, fm.height() + 14))
        hh.setMinimumSectionSize(40)
        for i in range(len(headers)):
            hh.setSectionResizeMode(i, hh.ResizeMode.Interactive)
        self._suppress = False
        self._user_sized = False
        hh.sectionResized.connect(self._on_section_resized)

    def _on_section_resized(self, *args):
        if not self._suppress:
            self._user_sized = True

    def autosize(self):
        """Fit every column to its content, then leave the rest alone.

        Leftover width is deliberately not redistributed: any fill policy either
        starves the headers or bloats a numeric column (a 231px-wide "成功").
        Content-fit plus draggable dividers keeps every header readable and lets
        the operator decide where the slack goes.
        """
        if self._user_sized or self.columnCount() == 0:
            return
        self._suppress = True
        self.resizeColumnsToContents()
        hh = self.horizontalHeader()
        fm = QFontMetrics(hh.font())
        for i in range(self.columnCount()):
            need = fm.horizontalAdvance(self.horizontalHeaderItem(i).text()) + 22
            w = max(MIN_COL, need, min(self.columnWidth(i), MAX_COL))
            self.setColumnWidth(i, w)
        self._suppress = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.autosize()

LEVEL_COLOR = {
    "info": "#8a8f98", "ok": "#1f9d55", "warn": "#c8871a",
    "error": "#d64545", "debug": "#7a6ad8", "time": "#5a8fd8",
}

# 中签检测没有「模式」可选：唯一的动作是读邮箱，通道由账号自己的
# protocol 决定（graph / imap / alias-imap / hme），不需要操作者选。
HIT_COLORS = {
    hitcheck.SOURCE_SUCCESS_MAIL: "#1f9d55",
    hitcheck.SOURCE_OASIS_MAIL: "#3fa88a",
    hitcheck.SOURCE_SITE_OK: "#c8871a",
}


class Bridge(QObject):
    """Marshals monitor callbacks from worker threads onto the UI thread.

    Every widget touched from a worker must come through here: creating an
    InfoBar off the GUI thread produces an unowned top-level window that cannot
    be dismissed.
    """
    log = pyqtSignal(str, str)
    stats = pyqtSignal(dict)
    hit = pyqtSignal(dict)
    sweep = pyqtSignal(dict)
    started = pyqtSignal(dict)
    stopped = pyqtSignal(dict)
    toast = pyqtSignal(str, str)
    refreshAll = pyqtSignal()


def tight(layout, m=PAD, s=GAP):
    layout.setContentsMargins(m, m, m, m)
    layout.setSpacing(s)
    return layout


def make_card(title):
    """A header card with a vertical body. Returns (card, body_layout)."""
    card = HeaderCardWidget()
    card.setTitle(title)
    card.headerView.setFixedHeight(36)
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    card.viewLayout.setContentsMargins(14, 10, 14, 12)
    card.viewLayout.setSpacing(0)
    card.viewLayout.addWidget(body)
    return card, lay


def make_table(headers):
    return FlexTable(headers)


class Combo(ComboBox):
    """ComboBox that opens already laid out.

    qfluentwidgets animates the drop menu's height. While that animation runs the
    rows paint with the plain delegate and only re-render once it settles, which
    reads as "plain text flashes, then the real options". Showing the menu
    without the expand animation removes the flicker.
    """

    def _showComboMenu(self):
        if not self.items:
            return
        menu = self._createComboMenu()
        for i, item in enumerate(self.items):
            action = QAction(item.icon, item.text,
                             triggered=lambda c, x=i: self._onItemClicked(x))
            action.setEnabled(item.isEnabled)
            menu.addAction(action)
        if menu.view.width() < self.width():
            menu.view.setMinimumWidth(self.width())
            menu.adjustSize()
        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu
        if self.currentIndex() >= 0 and self.items:
            menu.setDefaultAction(menu.actions()[self.currentIndex()])
        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        pos = self.mapToGlobal(QPoint(x, self.height()))
        menu.view.adjustSize(pos, MenuAnimationType.NONE)
        menu.exec(pos, aniType=MenuAnimationType.NONE)


def full_name(row):
    """Unclaimed mailboxes carry NULL name parts; don't render 'None None'."""
    return " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p)


def _when(value):
    """epoch -> 本地时间字符串。空值给空串，别把时间列填成 1970。"""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def fill_row(table, rows):
    table.setRowCount(len(rows))
    for i, vals in enumerate(rows):
        for j, v in enumerate(vals):
            table.setItem(i, j, QTableWidgetItem("" if v is None else str(v)))
    if hasattr(table, "autosize"):
        table.autosize()


def shrink_buttons(root, height=28):
    """qfluentwidgets buttons ship 34px tall; trim every plain
    PushButton/PrimaryPushButton to a denser height. The font is left alone -
    the app font is already the right one and overriding it only made buttons
    inconsistent with the rest of the UI."""
    for b in root.findChildren(QWidget):
        if type(b) in (PushButton, PrimaryPushButton):
            b.setFixedHeight(height)


class StatCard(SimpleCardWidget):
    def __init__(self, title, accent="#3b7dd8", parent=None):
        super().__init__(parent)
        self.setFixedHeight(64)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        # library labels keep the Fluent type ramp instead of a hand-set size
        self.value = SubtitleLabel("0")
        self.value.setStyleSheet(f"color: {accent};")
        lay.addWidget(self.value)
        lay.addWidget(CaptionLabel(title))

    def set_value(self, v):
        self.value.setText(str(v))


class Banner(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(72)
        self.setStyleSheet("""
            QFrame { border-radius: 8px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #b4531f, stop:0.45 #d9822b, stop:1 #f2c14e); }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 10, 20, 10)
        lay.setSpacing(2)
        title = SubtitleLabel("Oasis Live '27  中签检测台")
        title.setStyleSheet("color:#fff8f0;")
        sub = CaptionLabel("定时巡检已导入账号的收件箱 · 邮件证据与站点记录合并判定")
        sub.setStyleSheet("color:rgba(255,248,240,0.9);")
        lay.addWidget(title)
        lay.addWidget(sub)


# --------------------------------------------------------------------- pages
class DashboardPage(ScrollArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("DashboardPage")
        self.setWidgetResizable(True)
        self.view = QWidget()
        self.setWidget(self.view)
        self.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                           "QWidget{background:transparent;}")
        root = tight(QVBoxLayout(self.view), PAD, GAP)

        root.addWidget(Banner())

        row = QHBoxLayout()
        row.setSpacing(GAP)
        self.cards = {
            "total": StatCard("账号总数", "#3b7dd8"),
            "hits": StatCard("已中签", "#1f9d55"),
            "unchecked": StatCard("还没查过", "#8a8f98"),
            "checked": StatCard("查过未中签", "#3fa88a"),
            "check_errors": StatCard("读信失败", "#d64545"),
            "registrations": StatCard("旧预约记录", "#7a6ad8"),
        }
        for c in self.cards.values():
            row.addWidget(c, 1)
        root.addLayout(row)

        ctrl, body = make_card("检测控制")
        cl = QHBoxLayout()
        cl.setSpacing(8)
        cl.addWidget(BodyLabel("并发"))
        self.threads = CompactSpinBox()
        self.threads.setRange(1, 32)
        self.threads.setValue(int(win.cfg.get("threads", 2)))
        self.threads.setFixedWidth(84)
        self.threads.setToolTip(
            "同时打开几条收件箱连接。同一个 Gmail 收件箱下的 iCloud 别名共用一条\n"
            "连接，所以这个数字是并发数，不是「同时读几封信」。")
        cl.addWidget(self.threads)
        cl.addWidget(BodyLabel("间隔s"))
        self.interval = CompactSpinBox()
        self.interval.setRange(30, 86400)
        self.interval.setSingleStep(30)
        self.interval.setValue(int(win.cfg.get("interval", 300)))
        self.interval.setFixedWidth(96)
        self.interval.setToolTip(
            "一轮跑完到下一轮开始之间的等待。中签通知不是秒级事件，\n"
            "频率再高只是把对方的收件箱打成请求尖峰。")
        cl.addWidget(self.interval)
        cl.addWidget(BodyLabel("回看天"))
        self.lookback = CompactSpinBox()
        self.lookback.setRange(0, 3650)
        self.lookback.setValue(int(win.cfg.get("lookback_days", 30)))
        self.lookback.setFixedWidth(96)
        self.lookback.setToolTip(
            "首次检测回看多少天。活动已经结束，中签结果很可能早就发出去了，\n"
            "这个窗口要覆盖「结果可能已发」的那段时间；0 = 不设基线，\n"
            "邮箱里所有 Oasis 来信都算数。")
        cl.addWidget(self.lookback)
        self.btn_start = PrimaryPushButton(FIF.PLAY, "开始检测")
        self.btn_start.clicked.connect(self.on_start)
        cl.addWidget(self.btn_start)
        self.btn_check = PushButton(FIF.SYNC, "立即检查一轮")
        self.btn_check.clicked.connect(self.on_check_now)
        cl.addWidget(self.btn_check)
        self.btn_stop = PushButton(FIF.CANCEL, "停止")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        cl.addWidget(self.btn_stop)
        self.progress = ProgressBar()
        self.progress.setFixedWidth(200)
        cl.addWidget(self.progress)
        cl.addStretch(1)
        body.addLayout(cl)
        self.run_hint = CaptionLabel("")
        body.addWidget(self.run_hint)
        root.addWidget(ctrl)

        host, hbody = make_card("本机状态")
        hrow = QHBoxLayout()
        hrow.setSpacing(10)
        self.host_label = BodyLabel("读取中…")
        hrow.addWidget(self.host_label, 1)
        self.btn_apply_rec = PushButton(FIF.SYNC, "应用推荐并发")
        self.btn_apply_rec.clicked.connect(self.on_apply_recommended)
        hrow.addWidget(self.btn_apply_rec)
        hbody.addLayout(hrow)
        self.host_hint = CaptionLabel("")
        hbody.addWidget(self.host_hint)
        root.addWidget(host)

        recent, rbody = make_card("最近中签")
        self.recent = make_table(["中签时间", "邮箱", "证据来源", "说明",
                                  "最近来信"])
        self.recent.setFixedHeight(190)
        rbody.addWidget(self.recent)
        root.addWidget(recent)
        root.addStretch(1)

    def idle_hint(self):
        """Readiness line: says what is still missing rather than 'ready'."""
        s = self.win.store.stats()
        if not s.get("total", 0):
            return "先到「邮箱池」导入账号，然后点「开始检测」。"
        if s.get("hits"):
            return (f"就绪：{s['total']} 个账号 · 已中签 {s['hits']} · "
                    f"未检测 {s.get('unchecked', 0)} · "
                    f"并发 {self.threads.value()} · 每 {self.interval.value()}s 一轮")
        return (f"就绪：{s['total']} 个账号全部待检 · "
                f"并发 {self.threads.value()} · 每 {self.interval.value()}s 一轮")

    def refresh_host(self):
        """内存 + 推荐并发数。检测程序不吃内存，瓶颈在上游并发限制。"""
        info = sysinfo.summary("mail")
        if info["total_mb"] is None:
            self.host_label.setText("本机内存：读不到")
            self.host_hint.setText(f"按 CPU 核数估计：建议 {info['recommended']} 并发")
        else:
            used = info["total_mb"] - info["available_mb"]
            self.host_label.setText(
                f"内存 {used} / {info['total_mb']} MB 在用 · "
                f"可用 {info['available_mb']} MB · {info['cores']} 核")
            self.host_hint.setText(
                f"建议 {info['recommended']} 并发 —— {info['reason']}")
        self._recommended = info["recommended"]

    def on_apply_recommended(self):
        n = getattr(self, "_recommended", None)
        if not n:
            self.refresh_host()
            n = getattr(self, "_recommended", 1)
        self.win.cfg.set("threads", int(n))
        self.win.cfg.save()
        self.threads.setValue(int(n))
        self.win.notify("success", f"并发已设为 {n}")

    # ----------------------------------------------------------------- control
    def on_start(self):
        self.win.start_monitor(self.threads.value(), self.interval.value(),
                               self.lookback.value())

    def on_check_now(self):
        self.win.check_now()

    def on_stop(self):
        self.win.stop_monitor()

    def add_recent(self, d):
        t = self.recent
        t.insertRow(0)
        when = datetime.now().strftime("%H:%M:%S")
        vals = [d.get("at") or when, d.get("email", ""),
                hitcheck.SOURCE_LABEL.get(d.get("source"), d.get("source") or ""),
                d.get("note", ""), d.get("subject", "")]
        for i, v in enumerate(vals):
            t.setItem(0, i, QTableWidgetItem(v))
        while t.rowCount() > 200:
            t.removeRow(t.rowCount() - 1)
        t.autosize()


class MailboxPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("MailboxPage")
        root = tight(QVBoxLayout(self), PAD, GAP)

        head = QHBoxLayout()
        head.addWidget(TitleLabel("邮箱池"))
        head.addWidget(CaptionLabel(
            "一行一个账号，格式 email----password----client_id----refresh_token。"
            "重复邮箱会被数据库直接拒绝，不会重复入库。"))
        head.addStretch(1)
        root.addLayout(head)

        imp, ibody = make_card("导入凭据")
        prow = QHBoxLayout()
        prow.setSpacing(8)
        prow.addWidget(BodyLabel("取件协议"))
        self.protocol = Combo()
        for p in mailbox.PROTOCOLS:
            self.protocol.addItem(mailbox.PROTOCOL_LABEL[p])
        # auto by default: some client_ids can redeem the Graph scope but are
        # then rejected by Graph itself, and picking the wrong one wastes the
        # whole mail timeout per account.
        self.protocol.setCurrentIndex(mailbox.PROTOCOLS.index("auto"))
        self.protocol.setFixedWidth(196)
        prow.addWidget(self.protocol)
        self.proto_hint = CaptionLabel("")
        prow.addWidget(self.proto_hint)
        prow.addStretch(1)
        ibody.addLayout(prow)
        self.protocol.currentIndexChanged.connect(self.on_protocol_changed)
        self.text = TextEdit()
        self.text.setPlaceholderText(
            "每行一条：\n"
            "微软 4 段：user@outlook.com----password----client_id----refresh_token\n"
            "Gmail 5 段：user@gmail.com----password----client_id----client_secret----refresh_token\n"
            "iCloud 3 段：别名@icloud.com----acc_xxxxxxxx----hme\n\n"
            "Gmail 必须带 client_secret（Google 不给刷新）。"
            "iCloud 的 acc_ 从本地服务的 /api/accounts 取。空行与 # 开头的行会被忽略。")
        self.text.setFixedHeight(104)
        ibody.addWidget(self.text)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_import = PrimaryPushButton(FIF.ADD, "导入到数据库")
        self.btn_import.clicked.connect(self.on_import)
        self.btn_file = PushButton(FIF.FOLDER, "从文件导入")
        self.btn_file.clicked.connect(self.on_file)
        self.btn_verify = PushButton(FIF.CHECKBOX, "校验前 20 条")
        self.btn_verify.clicked.connect(self.on_verify)
        self.btn_export = PushButton(FIF.SAVE, "导出全部凭据")
        self.btn_export.clicked.connect(self.on_export)
        for b in (self.btn_import, self.btn_file, self.btn_verify, self.btn_export):
            row.addWidget(b)
        row.addStretch(1)
        ibody.addLayout(row)
        root.addWidget(imp)

        lst, lbody = make_card("账号列表")
        self.list_card = lst
        self.table = make_table(["ID", "邮箱", "协议", "中签", "证据来源",
                                 "最近来信", "检查次数", "检测错误", "更新时间"])
        lbody.addWidget(self.table, 1)
        rrow = QHBoxLayout()
        rrow.setSpacing(8)
        self.btn_refresh = PushButton(FIF.SYNC, "刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_reset = PushButton(FIF.UPDATE, "重置检测记录")
        self.btn_reset.clicked.connect(self.on_reset)
        rrow.addWidget(self.btn_refresh)
        rrow.addWidget(self.btn_reset)
        rrow.addStretch(1)
        rrow.addWidget(CaptionLabel("清理："))
        self.btn_clean_hit = PushButton(FIF.DELETE, "已中签")
        self.btn_clean_hit.clicked.connect(lambda: self.on_clean("hit"))
        self.btn_clean_pending = PushButton(FIF.DELETE, "未中签")
        self.btn_clean_pending.clicked.connect(lambda: self.on_clean("pending"))
        self.btn_clean_all = PushButton(FIF.DELETE, "全部")
        self.btn_clean_all.clicked.connect(lambda: self.on_clean(None))
        for b in (self.btn_clean_hit, self.btn_clean_pending,
                  self.btn_clean_all):
            rrow.addWidget(b)
        lbody.addLayout(rrow)
        root.addWidget(lst, 1)
        self.on_protocol_changed()

    def on_protocol_changed(self):
        proto = mailbox.PROTOCOLS[self.protocol.currentIndex()]
        hints = {
            "graph": "整批用 Graph REST 取信。注意：部分 client_id 能兑换 Graph scope "
                     "但会被 Graph 拒绝（401），这种号只能用 IMAP。",
            "imap": "整批用 IMAP（outlook.office365.com:993 + XOAUTH2）取信，"
                    "会同时搜 INBOX 与 Junk。",
            "auto": "先试 Graph，被拒或取不到就自动换 IMAP。不确定时选这个（默认）。",
        }
        self.proto_hint.setText(hints.get(proto, ""))

    def on_clean(self, bucket):
        """Delete a bucket of accounts; their registrations follow by CASCADE.

        「未中签」按 status='pending' 删 —— 检测程序从不改这个字段，中签是写
        hit_at 而不是换状态，所以 pending 就是「还没中签」的那一堆。
        """
        stats = self.win.store.stats()
        label = {"hit": "已中签", "pending": "未中签"}.get(bucket, "全部")
        if bucket == "hit":
            n = stats.get("hits", 0)
        elif bucket:
            n = stats.get(bucket, 0)
        else:
            n = stats.get("total", 0)
        if not n:
            self.win.notify("warning", f"没有{label}的账号可清理")
            return
        extra = ("\n\n注意：这些账号的旧预约记录会一并删除（外键级联）。"
                 if bucket else "")
        if not MessageBox("确认清理",
                          f"将永久删除 {n} 条「{label}」账号，不可撤销。{extra}\n\n继续吗？",
                          self).exec():
            return
        if bucket == "hit":
            removed = self.win.store.delete_hits()
        else:
            removed = self.win.store.delete_accounts(bucket)
        self.win.store.vacuum()
        self.refresh()
        self.win.notify("success", f"已清理 {removed} 条「{label}」账号")
        self.win.log("warn", f"清理数据库：删除 {removed} 条「{label}」账号")
        self.win._refresh_all()

    def on_import(self):
        proto = mailbox.PROTOCOLS[self.protocol.currentIndex()]
        added, dup = self.win.store.add_mailboxes(
            self.text.toPlainText().splitlines(), proto)
        self.text.clear()
        self.refresh()
        self.win.notify("success",
                        f"[{mailbox.PROTOCOL_LABEL[proto]}] 新增 {added}，重复跳过 {dup}")
        self.win.log("ok", f"导入邮箱({proto})：新增 {added}，重复 {dup}")

    def on_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择凭据文件", BASE, "文本 (*.txt)")
        if not path:
            return
        proto = mailbox.PROTOCOLS[self.protocol.currentIndex()]
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()
        added, dup = self.win.store.add_mailboxes(lines, proto)
        self.refresh()
        self.win.notify("success",
                        f"[{mailbox.PROTOCOL_LABEL[proto]}] 新增 {added}，重复 {dup}")

    def on_verify(self):
        rows = self.win.store.accounts(limit=20)
        if not rows:
            self.win.notify("warning", "数据库里还没有账号")
            return
        self.win.log("info", "校验 Graph 凭据…")

        def work():
            ok = bad = 0
            for r in rows:
                line = self.win.store.cred_line_for(r["id"])
                if not line:
                    continue
                proto = self.win.store.protocol_for(r["id"])
                try:
                    name, upn = mailbox.check_credentials(
                        line, proto, self.win.cfg.get("mail_proxy", ""))
                    ok += 1
                    self.win.log("ok", f"  [{proto}] {r['email']} -> {name or upn}")
                except Exception as e:
                    bad += 1
                    self.win.log("error",
                                 f"  [{proto}] {r['email']} -> "
                                 f"{type(e).__name__}: {str(e)[:80]}")
            self.win.log("info", f"校验完成：可用 {ok}，失败 {bad}")
            self.win.bridge.toast.emit(
                "success" if bad == 0 else "warning",
                f"凭据校验完成：可用 {ok}，失败 {bad}")
        threading.Thread(target=work, daemon=True).start()

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出凭据", os.path.join(BASE, "creds_export.txt"), "文本 (*.txt)")
        if not path:
            return
        lines = self.win.store.cred_lines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        self.win.notify("success", f"已导出 {len(lines)} 条")

    def on_reset(self):
        n = self.win.store.reset_checks()
        self.refresh()
        self.win.notify("success", f"{n} 条未中签账号已重置，下轮重新检测")
        self.win.log("warn", f"重置 {n} 条检测记录（已中签的不动）")

    def refresh(self):
        s = self.win.store.stats()
        rows = self.win.store.accounts(DISPLAY_LIMIT)
        self.list_card.setTitle(
            f"账号列表（共 {s.get('total', 0)} 条，中签 {s.get('hits', 0)}，"
            f"显示最近 {len(rows)}）")
        fill_row(self.table, [
            [r["id"], r["email"], r.get("protocol") or "graph",
             _when(r.get("hit_at")), hitcheck.SOURCE_LABEL.get(
                 r.get("hit_source"), r.get("hit_source") or ""),
             _when(r.get("last_mail_at")), r.get("check_count") or 0,
             (r.get("check_error") or "")[:56], r["updated_at"]] for r in rows])


class SettingsPage(ScrollArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("SettingsPage")
        self.setWidgetResizable(True)
        self.view = QWidget()
        self.setWidget(self.view)
        self.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                           "QWidget{background:transparent;}")
        root = tight(QVBoxLayout(self.view), PAD, GAP)
        root.addWidget(TitleLabel("设置"))

        adv, abody = make_card("检测节奏")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.interval = CompactSpinBox()
        self.interval.setRange(30, 86400)
        self.interval.setSingleStep(30)
        self.interval.setValue(int(win.cfg.get("interval", 300)))
        self.interval.setFixedWidth(110)
        self.interval.setToolTip(
            "一轮跑完到下一轮开始之间的等待（秒）。中签通知不是秒级事件，\n"
            "频率再高只是把对方的收件箱打成请求尖峰。")
        self.threads = CompactSpinBox()
        self.threads.setRange(1, 32)
        self.threads.setValue(int(win.cfg.get("threads", 2)))
        self.threads.setFixedWidth(110)
        self.threads.setToolTip(
            "同时打开几条收件箱连接。同一 Gmail 收件箱下的 iCloud 别名共用一条，\n"
            "所以这是并发连接数，不是「同时读几封信」。")
        self.lookback_days = CompactSpinBox()
        self.lookback_days.setRange(0, 3650)
        self.lookback_days.setValue(int(win.cfg.get("lookback_days", 30)))
        self.lookback_days.setFixedWidth(110)
        self.lookback_days.setToolTip(
            "首次检测回看多少天；0 = 不设基线，邮箱里所有 Oasis 来信都算数。\n"
            "活动已结束，中签结果可能早就发过了，这个窗口决定那些信算不算新信。")
        self.per_page = CompactSpinBox()
        self.per_page.setRange(1, 200)
        self.per_page.setValue(int(win.cfg.get("per_page", 20)))
        self.per_page.setFixedWidth(110)
        self.per_page.setToolTip("每个邮箱取最近多少封信来判。")
        self.skip_hits = SwitchButton()
        self.skip_hits.setChecked(bool(win.cfg.get("skip_hits", True)))
        self.skip_hits.setToolTip(
            "开启后已中签的账号不再重复检测 —— 同一个答案重复搜索没有意义。\n"
            "若想连后续的付款/取票通知一起盯，把它关掉。")
        self.mail_filter = SwitchButton()
        self.mail_filter.setChecked(bool(win.cfg.get("mail_filter", True)))
        self.mail_filter.setToolTip(
            "开启后只让服务端把 Oasis 的来信拉回来（发件人 openstage 或标题含 Oasis），\n"
            "而不是把「最近 N 封」整个拉回本地挑。首次检测一个账号时始终走全量。\n"
            "判据来自实测：验证信与成功邮件都是 oasis@openstageit.com 发的。\n"
            "若哪天站点换了发件人域、结果信标题里又没有 Oasis，把它关掉。")
        self.debug = SwitchButton()
        self.debug.setChecked(bool(win.cfg.get("debug", False)))

        rows = [("检测间隔(秒)", self.interval),
                ("并发连接数", self.threads),
                ("首次回看(天)", self.lookback_days),
                ("每箱取信(封)", self.per_page),
                ("跳过已中签", self.skip_hits),
                ("只拉 Oasis 来信", self.mail_filter),
                ("调试堆栈", self.debug)]
        for i, (label, w) in enumerate(rows):
            grid.addWidget(BodyLabel(label), i, 0)
            grid.addWidget(w, i, 1)
        grid.setColumnStretch(1, 1)
        abody.addLayout(grid)
        root.addWidget(adv)

        mail, mbody = make_card("取件通道")
        mgrid = QGridLayout()
        mgrid.setHorizontalSpacing(12)
        mgrid.setVerticalSpacing(8)
        self.mail_proxy = LineEdit()
        self.mail_proxy.setPlaceholderText(
            "留空 = 直连（推荐）。仅在网络必须走代理时才填，例如 socks5://127.0.0.1:1080")
        self.mail_proxy.setText(str(win.cfg.get("mail_proxy", "")))
        self.hme_base = LineEdit()
        self.hme_base.setPlaceholderText(
            "本地 iCloud 隐藏邮箱服务地址，默认 http://127.0.0.1:8081")
        self.hme_base.setText(str(win.cfg.get("hme_base", "http://127.0.0.1:8081")))
        self.hme_password = LineEdit()
        self.hme_password.setEchoMode(LineEdit.EchoMode.Password)
        self.hme_password.setPlaceholderText(
            "该服务的管理员密码（启动时的 ICLOUD_HME_ADMIN_PASSWORD）")
        self.hme_password.setText(str(win.cfg.get("hme_password", "")))
        self.alias_inbox = LineEdit()
        self.alias_inbox.setPlaceholderText(
            "承载 iCloud 别名的 Gmail 地址；导入裸别名时用它补全凭据行")
        self.alias_inbox.setText(str(win.cfg.get("alias_inbox", "")))
        self.alias_inbox_password = LineEdit()
        self.alias_inbox_password.setEchoMode(LineEdit.EchoMode.Password)
        self.alias_inbox_password.setPlaceholderText(
            "该 Gmail 的应用专用密码（不是账号密码）")
        self.alias_inbox_password.setText(
            str(win.cfg.get("alias_inbox_password", "")))
        mrows = [("取件代理", self.mail_proxy),
                 ("iCloud 服务", self.hme_base),
                 ("iCloud 密码", self.hme_password),
                 ("别名收件箱", self.alias_inbox),
                 ("收件箱密码", self.alias_inbox_password)]
        for i, (label, w) in enumerate(mrows):
            mgrid.addWidget(BodyLabel(label), i, 0)
            mgrid.addWidget(w, i, 1)
        mgrid.setColumnStretch(1, 1)
        mbody.addLayout(mgrid)
        root.addWidget(mail)

        store, sbody = make_card("存储与日志")
        sgrid = QGridLayout()
        sgrid.setHorizontalSpacing(12)
        sgrid.setVerticalSpacing(8)
        self.db_path = LineEdit()
        self.db_path.setText(str(win.cfg.get("db_path", "oasis.db")))
        self.log_file = LineEdit()
        self.log_file.setText(str(win.cfg.get("log_file", "oasis_run.log")))
        for i, (label, w) in enumerate((("数据库路径", self.db_path),
                                        ("日志文件", self.log_file))):
            sgrid.addWidget(BodyLabel(label), i, 0)
            sgrid.addWidget(w, i, 1)
        sgrid.setColumnStretch(1, 1)
        sbody.addLayout(sgrid)
        root.addWidget(store)

        theme, tbody = make_card("外观")
        trow = QHBoxLayout()
        trow.addWidget(BodyLabel("主题"))
        self.theme_box = Combo()
        self.theme_box.addItems(["浅色", "深色"])
        self.theme_box.currentIndexChanged.connect(
            lambda i: setTheme(Theme.DARK if i else Theme.LIGHT))
        trow.addWidget(self.theme_box)
        trow.addStretch(1)
        tbody.addLayout(trow)
        root.addWidget(theme)

        brow = QHBoxLayout()
        self.btn_save = PrimaryPushButton(FIF.SAVE, "保存设置")
        self.btn_save.clicked.connect(self.on_save)
        brow.addWidget(self.btn_save)
        brow.addStretch(1)
        root.addLayout(brow)
        root.addStretch(1)

    def on_save(self):
        self.win.cfg.update({
            "interval": self.interval.value(),
            "threads": self.threads.value(),
            "lookback_days": self.lookback_days.value(),
            "per_page": self.per_page.value(),
            "skip_hits": self.skip_hits.isChecked(),
            "mail_filter": self.mail_filter.isChecked(),
            "mail_proxy": self.mail_proxy.text().strip(),
            "hme_base": self.hme_base.text().strip(),
            "hme_password": self.hme_password.text().strip(),
            "alias_inbox": self.alias_inbox.text().strip(),
            "alias_inbox_password": self.alias_inbox_password.text().strip(),
            "db_path": self.db_path.text().strip() or "oasis.db",
            "log_file": self.log_file.text().strip() or "oasis_run.log",
            "debug": self.debug.isChecked()})
        self.win.cfg.save()
        self.win.notify("success", "设置已保存（下一轮生效）")


class LogPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("LogPage")
        root = tight(QVBoxLayout(self), PAD, GAP)

        row = QHBoxLayout()
        row.addWidget(TitleLabel("运行日志"))
        row.addSpacing(8)
        self.autoscroll = SwitchButton()
        self.autoscroll.setChecked(True)
        row.addWidget(BodyLabel("自动滚动"))
        row.addWidget(self.autoscroll)
        row.addStretch(1)
        b_clear = PushButton(FIF.DELETE, "清空")
        b_clear.clicked.connect(lambda: self.console.clear())
        b_save = PushButton(FIF.SAVE, "导出")
        b_save.clicked.connect(self.on_export)
        row.addWidget(b_clear)
        row.addWidget(b_save)
        root.addLayout(row)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.console.setStyleSheet("""
            QTextEdit { background:#14161a; color:#d6d9de;
                border:1px solid rgba(0,0,0,0.12); border-radius:6px; padding:8px; }
        """)
        root.addWidget(self.console, 1)

    def append(self, level, msg):
        color = LEVEL_COLOR.get(level, "#d6d9de")
        safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.console.append(
            f'<span style="color:{LEVEL_COLOR["time"]}">'
            f'[{datetime.now():%H:%M:%S}]</span> '
            f'<span style="color:{color}">{safe}</span>')
        if self.autoscroll.isChecked():
            self.console.moveCursor(QTextCursor.MoveOperation.End)

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", os.path.join(BASE, "oasis_console.log"),
            "日志 (*.log *.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.console.toPlainText())
        self.win.notify("success", "日志已导出")


class DataPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("DataPage")
        root = tight(QVBoxLayout(self), PAD, GAP)

        head = QHBoxLayout()
        head.addWidget(TitleLabel("数据库"))
        head.addStretch(1)
        b = PushButton(FIF.SYNC, "刷新")
        b.clicked.connect(self.refresh)
        b_csv = PushButton(FIF.DOWNLOAD, "导出 CSV")
        b_csv.clicked.connect(self.on_csv)
        head.addWidget(b)
        head.addWidget(b_csv)
        root.addLayout(head)

        reg, rbody = make_card("旧预约记录（上一版程序留下，只读）")
        self.reg_card = reg
        self.reg_table = make_table(["ID", "邮箱", "方式", "状态", "时间"])
        rbody.addWidget(self.reg_table, 1)
        root.addWidget(reg, 1)

        acc, abody = make_card("账号与检测结果")
        self.acc_card = acc
        self.acc_table = make_table(["ID", "邮箱", "协议", "姓名", "中签",
                                     "证据来源", "最近来信", "检查次数",
                                     "检测错误"])
        abody.addWidget(self.acc_table, 1)
        root.addWidget(acc, 1)

    def refresh(self):
        s = self.win.store.stats()
        regs = self.win.store.registrations(DISPLAY_LIMIT)
        accs = self.win.store.accounts(DISPLAY_LIMIT)
        self.reg_card.setTitle(
            f"旧预约记录（共 {s.get('registrations', 0)} 条，显示最近 {len(regs)}）")
        self.acc_card.setTitle(
            f"账号与检测结果（共 {s.get('total', 0)} 条，中签 "
            f"{s.get('hits', 0)}，显示最近 {len(accs)}）")
        fill_row(self.reg_table, [
            [r["id"], r["email"], r.get("mode") or "-", r["status"],
             r["created_at"]] for r in regs])
        fill_row(self.acc_table, [
            [r["id"], r["email"], r.get("protocol") or "graph", full_name(r),
             _when(r.get("hit_at")),
             hitcheck.SOURCE_LABEL.get(r.get("hit_source"),
                                       r.get("hit_source") or ""),
             _when(r.get("last_mail_at")), r.get("check_count") or 0,
             (r.get("check_error") or "")[:50]]
            for r in accs])

    def on_csv(self):
        """导出中签名单全量（表格显示有上限，导出没有）。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出中签名单（全量）", os.path.join(BASE, "oasis_hits.csv"),
            "CSV (*.csv)")
        if not path:
            return
        hits = self.win.store.hits()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "email", "中签时间", "证据来源", "说明",
                        "最近来信", "来信标题", "协议"])
            for h in hits:
                w.writerow([h["id"], h["email"], _when(h.get("hit_at")),
                            hitcheck.SOURCE_LABEL.get(h.get("hit_source"),
                                                      h.get("hit_source") or ""),
                            h.get("hit_note") or "",
                            _when(h.get("last_mail_at")),
                            h.get("last_mail_subject") or "",
                            h.get("protocol") or ""])
        self.win.notify("success",
                        f"已导出 {len(hits)} 条中签记录（不受表格显示上限影响）")
        self.win.log("ok", f"导出 CSV：{len(hits)} 条 -> {os.path.basename(path)}")


# ----------------------------------------------------------------- main window
class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        setThemeColor("#d9822b")
        self.cfg = Config(os.path.join(BASE, "oasis_config.json"))
        self.store = Store(self.cfg.db_path)
        self.bridge = Bridge()
        self.monitor = HitMonitor(self.store, self._monitor_log,
                                  self._monitor_event, self.cfg.data)

        self.dash = DashboardPage(self)
        self.mail = MailboxPage(self)
        self.data = DataPage(self)
        self.logs = LogPage(self)
        self.settings = SettingsPage(self)

        self.addSubInterface(self.dash, FIF.HOME, "仪表盘")
        self.addSubInterface(self.mail, FIF.MAIL, "邮箱池")
        self.addSubInterface(self.data, FIF.DOCUMENT, "数据库")

        self.addSubInterface(self.logs, FIF.VIEW, "日志")
        self.addSubInterface(self.settings, FIF.SETTING, "设置",
                             NavigationItemPosition.BOTTOM)

        # the default expanded sidebar is 322px wide and eats the content area
        self.navigationInterface.setExpandWidth(168)
        shrink_buttons(self)

        self.resize(1320, 840)
        self.setMinimumSize(1080, 680)
        self.setWindowTitle("Oasis Live '27 中签检测台")
        self._log_fh = None

        self.bridge.log.connect(self._on_log)
        self.bridge.stats.connect(self._on_stats)
        self.bridge.hit.connect(self._on_hit)
        self.bridge.sweep.connect(self._on_sweep)
        self.bridge.stopped.connect(self._on_stopped)
        self.bridge.toast.connect(self.notify)
        self.bridge.refreshAll.connect(self._refresh_all)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)
        self._refresh_all()
        s = self.store.stats()
        self.log("info", f"检测台就绪：{s.get('total', 0)} 个账号 · "
                         f"已中签 {s.get('hits', 0)}"
                         + ("（含上一版程序并入的记录）"
                            if s.get("hits") else ""))
        if not s.get("total"):
            self.log("warn", "账号池是空的 —— 到「邮箱池」导入账号后再开始检测")

    # ---------------------------------------------------------------- plumbing
    def _monitor_log(self, level, msg):
        self.bridge.log.emit(level, msg)

    def _monitor_event(self, kind, payload):
        payload = payload or {}
        signal = {"hit": self.bridge.hit,
                  "sweep": self.bridge.sweep,
                  "stats": self.bridge.stats,
                  "stopped": self.bridge.stopped,
                  "started": self.bridge.started}.get(kind)
        if signal is not None:
            signal.emit(payload)

    def log(self, level, msg):
        self.bridge.log.emit(level, msg)

    def notify(self, kind, text):
        pos = InfoBarPosition.TOP_RIGHT
        if kind == "success":
            InfoBar.success("成功", text, duration=2500, position=pos, parent=self)
        elif kind == "warning":
            InfoBar.warning("提示", text, duration=2500, position=pos, parent=self)
        elif kind == "error":
            InfoBar.error("错误", text, duration=4500, position=pos, parent=self)
        else:
            InfoBar.info("信息", text, duration=2500, position=pos, parent=self)

    def _on_log(self, level, msg):
        self.logs.append(level, msg)
        try:
            if self._log_fh is None:
                p = self.cfg.get("log_file", "oasis_run.log")
                if not os.path.isabs(p):
                    p = os.path.join(BASE, p)
                self._log_fh = open(p, "a", encoding="utf-8")
            self._log_fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}\n")
            self._log_fh.flush()
        except Exception:
            pass

    def _on_stats(self, s):
        self._apply_stats(s)

    def _on_hit(self, d):
        self.dash.add_recent(d)
        self.notify("success", f"{d.get('email')} 中签 —— "
                               f"{hitcheck.SOURCE_LABEL.get(d.get('source'), '')}")
        self.data.refresh()

    def _on_sweep(self, payload):
        self._apply_stats(payload.get("stats") or self.store.stats())
        self.data.refresh()

    def _on_stopped(self, payload):
        self.dash.btn_start.setEnabled(True)
        self.dash.btn_stop.setEnabled(False)
        self.log("warn", "检测已停止")
        self._refresh_all()

    def _apply_stats(self, s):
        for k, card in self.dash.cards.items():
            card.set_value(s.get(k, 0))
        total = s.get("total", 0)
        # 进度按「已经查过」算，不按中签算：检测的目标是看完整个池子，
        # 中签数是结果而不是进度。
        seen = s.get("checked", 0) + s.get("hits", 0)
        self.dash.progress.setValue(min(100, int(seen / total * 100))
                                    if total else 0)

    def _tick(self):
        if self.monitor.running:
            self._apply_stats(self.store.stats())

    def _refresh_all(self):
        self._apply_stats(self.store.stats())
        self.mail.refresh()
        self.data.refresh()
        if not self.monitor.running:
            self.dash.run_hint.setText(self.dash.idle_hint())

    # ----------------------------------------------------------------- control
    def start_monitor(self, threads, interval, lookback_days):
        s = self.store.stats()
        if not s.get("total", 0):
            self.notify("warning", "账号池是空的 —— 先到「邮箱池」导入账号")
            return
        self.cfg.update({"threads": int(threads),
                         "interval": int(interval),
                         "lookback_days": int(lookback_days)})
        self.cfg.save()
        self.monitor.config = self.cfg.data
        self.dash.btn_start.setEnabled(False)
        self.dash.btn_stop.setEnabled(True)
        self.dash.run_hint.setText(
            f"检测中：并发 {threads} · 每 {interval}s 一轮 · "
            f"首次回看 {lookback_days} 天")
        self.monitor.start(threads=int(threads), interval=int(interval),
                           lookback_days=float(lookback_days),
                           per_page=int(self.cfg.get("per_page", 20)),
                           skip_hits=bool(self.cfg.get("skip_hits", True)))

    def check_now(self):
        """不等下一个间隔，立刻跑一轮。"""
        if not self.store.stats().get("total", 0):
            self.notify("warning", "账号池是空的 —— 先到「邮箱池」导入账号")
            return
        if self.monitor.running:
            self.monitor.wake()
            self.notify("info", "已排队：当前轮结束后立刻再查一轮")
            return
        self.start_monitor(self.dash.threads.value(), self.dash.interval.value(),
                           self.dash.lookback.value())

    def stop_monitor(self):
        self.monitor.stop()
        self.dash.run_hint.setText("正在停止（等当前这批账号读完）…")

    def closeEvent(self, e):
        if self.monitor.running:
            if not MessageBox("确认退出", "检测仍在运行，确定退出吗？", self).exec():
                e.ignore()
                return
            self.monitor.stop()
            self.monitor.join()
        try:
            if self._log_fh:
                self._log_fh.close()
        except Exception:
            pass
        e.accept()


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Oasis Console")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
