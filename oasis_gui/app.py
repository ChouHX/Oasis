#!/usr/bin/env python3
"""Oasis Live '27 registration console.

A Fluent desktop front-end over the curl_cffi registration core:

  * proxy pool configuration and health testing
  * configurable worker threads
  * live log with per-level colouring, plus a log file
  * SQLite persistence of every mailbox, identity and registration, with the
    de-duplication rules enforced in the schema

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
                             QHBoxLayout, QLabel, QTableWidgetItem, QTextEdit,
                             QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CaptionLabel, CompactDoubleSpinBox,
                            CompactSpinBox, ComboBox, FluentIcon as FIF,
                            FluentWindow, HeaderCardWidget, InfoBar,
                            InfoBarPosition, LineEdit, MenuAnimationType,
                            MessageBox, NavigationItemPosition,
                            PrimaryPushButton, ProgressBar, PushButton, ScrollArea,
                            SimpleCardWidget, SubtitleLabel,
                            SwitchButton, TableWidget, TextEdit, TitleLabel,
                            setTheme, setThemeColor, Theme)

from core import mailbox
from core import registrar
from core.config import Config
from core.engine import Engine
from core.proxy_pool import ProxyPool
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

# Keep Playwright's browsers beside the app instead of in the user profile, so a
# copied folder stays self-contained. Only set when the operator has not already
# pointed it somewhere else.
if getattr(sys, "frozen", False):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(BASE, "browsers"))

PAD = 14          # page content margin
GAP = 9           # spacing between cards
CHIP_COLS = 6
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
CHIP_ON = ("QLabel{border-radius:6px;padding:3px 7px;font-size:12px;"
           "background:rgba(217,130,43,0.16);color:#9c5a0c;"
           "border:1px solid rgba(217,130,43,0.5);font-weight:600;}")
CHIP_OFF = ("QLabel{border-radius:6px;padding:3px 7px;font-size:12px;"
            "background:rgba(0,0,0,0.030);color:#6b7280;"
            "border:1px solid rgba(0,0,0,0.055);}")
BADGE = ["①", "②", "③"]

# Registration transports. There is deliberately no captcha-less path: the
# server answers {"status":"OK"} without a token, but that is the request most
# likely to get an account flagged later, and a token now costs ~0.6s.
#   browser - the whole flow runs inside a real Chromium page, driving the
#             official form. hybrid (curl_cffi submission) was removed: measured
#             to be caught by the site's risk control.
MODES = ("browser",)
MODE_LABEL = {"browser": "浏览器 (驱动官方 SPA 全流程)"}


class Bridge(QObject):
    """Marshals engine callbacks from worker threads onto the UI thread.

    Every widget touched from a worker must come through here: creating an
    InfoBar off the GUI thread (as an earlier build did from the proxy test)
    produces an unowned top-level window that cannot be dismissed.
    """
    log = pyqtSignal(str, str)
    stats = pyqtSignal(dict)
    registered = pyqtSignal(dict)
    failed = pyqtSignal(dict)
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
        title = SubtitleLabel("Oasis Live '27  注册控制台")
        title.setStyleSheet("color:#fff8f0;")
        sub = CaptionLabel("浏览器真实 captcha · curl_cffi 提交 · SQLite 去重落库")
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
            "pending": StatCard("待注册", "#8a8f98"),
            "running": StatCard("进行中", "#c8871a"),
            "registered": StatCard("邮件确认注册", "#1f9d55"),
            "submitted": StatCard("页面确认提交", "#3fa88a"),
            "failed": StatCard("失败", "#d64545"),
            "registrations": StatCard("预约记录", "#7a6ad8"),
        }
        for c in self.cards.values():
            row.addWidget(c, 1)
        root.addLayout(row)

        ctrl, body = make_card("运行控制")
        cl = QHBoxLayout()
        cl.setSpacing(8)
        cl.addWidget(BodyLabel("线程"))
        self.threads = CompactSpinBox()
        self.threads.setRange(1, 64)
        self.threads.setValue(int(win.cfg.get("threads", 4)))
        self.threads.setFixedWidth(84)
        cl.addWidget(self.threads)
        cl.addWidget(BodyLabel("间隔s"))
        self.delay = CompactDoubleSpinBox()
        self.delay.setRange(0.0, 30.0)
        self.delay.setSingleStep(0.5)
        self.delay.setValue(float(win.cfg.get("delay_between", 0.0)))
        self.delay.setFixedWidth(92)
        cl.addWidget(self.delay)
        cl.addWidget(BodyLabel("方式"))
        # Only one flow is carried: driving the real SPA form. A plain curl
        # submission is caught by the site's risk control, so the picker is a
        # label now rather than a choice that would quietly lose accounts.
        self.mode_label = BodyLabel("浏览器（驱动官方 SPA 全流程）")
        self.mode_label.setToolTip(
            "填官方表单、逐页点 Continue、由页面自己提交。\n"
            "captcha 由页面签发、location.county / ip 由 SPA 自己填，"
            "三者自洽；这是我们能复现真人行为的最接近方式。")
        self.mode_label.setFixedWidth(196)
        cl.addWidget(self.mode_label)
        self.btn_start = PrimaryPushButton(FIF.PLAY, "开始注册")
        self.btn_start.clicked.connect(self.on_start)
        cl.addWidget(self.btn_start)
        self.btn_stop = PushButton(FIF.CANCEL, "停止")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        cl.addWidget(self.btn_stop)
        self.progress = ProgressBar()
        self.progress.setFixedWidth(220)
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
        self.btn_apply_rec = PushButton(FIF.SYNC, "应用推荐线程")
        self.btn_apply_rec.clicked.connect(self.on_apply_recommended)
        hrow.addWidget(self.btn_apply_rec)
        hbody.addLayout(hrow)
        self.host_hint = CaptionLabel("")
        hbody.addWidget(self.host_hint)
        root.addWidget(host)

        shows, sbody = make_card("场次偏好（全部场次，前三名按顺序提交）")
        prow = QHBoxLayout()
        prow.setSpacing(8)
        order = win.cfg.get("shows", registrar.DEFAULT_ORDER)
        for i, tag in enumerate(["第一", "第二", "第三"]):
            prow.addWidget(BodyLabel(tag))
            cb = Combo()
            for key in registrar.SHOW_ORDER:
                cb.addItem(registrar.SHOW_LABEL[key])
            want = order[i] if i < len(order) and order[i] in registrar.SHOWS else None
            cb.setCurrentIndex(registrar.SHOW_ORDER.index(want) if want else i)
            cb.setFixedWidth(186)
            cb.currentIndexChanged.connect(self.on_shows_changed)
            setattr(self, f"show{i}", cb)
            prow.addWidget(cb)
            if i < 2:
                prow.addWidget(QLabel(">"))
        prow.addStretch(1)
        sbody.addLayout(prow)

        grid = QGridLayout()
        grid.setSpacing(5)
        self.chips = {}
        for i, key in enumerate(registrar.SHOW_ORDER):
            chip = QLabel()
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.chips[key] = chip
            grid.addWidget(chip, i // CHIP_COLS, i % CHIP_COLS)
        sbody.addLayout(grid)
        root.addWidget(shows)
        self.on_shows_changed()

        recent, rbody = make_card("最近成功")
        self.recent = make_table(["时间", "邮箱", "方式", "姓名", "电话", "城市",
                                  "场次", "耗时"])
        self.recent.setFixedHeight(170)
        rbody.addWidget(self.recent)
        root.addWidget(recent)
        root.addStretch(1)

    # ---------------------------------------------------------------- shows
    def current_order(self):
        idx = [getattr(self, f"show{i}").currentIndex() for i in range(3)]
        if len(set(idx)) != 3:
            return None
        return [registrar.SHOW_ORDER[i] for i in idx]

    def on_shows_changed(self):
        order = self.current_order()
        for key, chip in self.chips.items():
            date, venue, country = registrar.SHOW_META[key]
            text = f"{date}  {venue}  {country}"
            if order and key in order:
                chip.setText(f"{BADGE[order.index(key)]} {text}")
                chip.setStyleSheet(CHIP_ON)
            else:
                chip.setText(text)
                chip.setStyleSheet(CHIP_OFF)
        if order is None:
            self.run_hint.setText("三个偏好不能重复。")
        else:
            self.win.cfg.set("shows", order)
            self.win.cfg.save()
            self.run_hint.setText("偏好已保存：" +
                                  " > ".join(registrar.SHOW_LABEL[k] for k in order))

    def idle_hint(self):
        mode = "browser"
        """Readiness line: says what is still missing rather than 'ready'."""
        s = self.win.store.stats()
        pending = s.get("pending", 0)
        proxies = len(self.win.pool)
        if not pending and not proxies:
            return "先做两步：到「邮箱池」导入账号，到「代理池」填代理（每行一个）。"
        if not pending:
            return (f"代理 {proxies} 个已就绪，但队列里没有待注册账号 —— "
                    f"到「邮箱池」导入，或点「重置失败与卡住项」把旧账号放回队列。")
        if not proxies:
            return (f"待注册 {pending} 条已就绪，但还没有代理 —— "
                    f"到「代理池」填一行 http://user:pass@host:port 并保存。")
        return (f"就绪：待注册 {pending} 条 · 代理 {proxies} 个 · "
                f"方式 {MODE_LABEL[mode]} · 线程 {self.threads.value()}")

    def refresh_host(self):
        mode = "browser"
        """内存 + 推荐线程数。浏览器模式每线程约 250MB，超了会一起变慢。"""
        from core import sysinfo
        info = sysinfo.summary(mode)
        if info["total_mb"] is None:
            self.host_label.setText("本机内存：读不到")
            self.host_hint.setText("按 CPU 核数估计：建议 %d 线程" % info["recommended"])
        else:
            used = info["total_mb"] - info["available_mb"]
            self.host_label.setText(
                f"内存 {used} / {info['total_mb']} MB 在用 · "
                f"可用 {info['available_mb']} MB · {info['cores']} 核")
            self.host_hint.setText(
                f"当前模式（{MODE_LABEL.get(mode, mode)[:6]}）建议 "
                f"{info['recommended']} 线程 —— {info['reason']}")
        self._recommended = info["recommended"]

    def on_apply_recommended(self):
        n = getattr(self, "_recommended", None)
        if not n:
            self.refresh_host()
            n = getattr(self, "_recommended", 1)
        self.win.cfg.set("threads", int(n))
        self.win.cfg.save()
        self.threads.setValue(int(n))
        self.win.notify("success", f"线程数已设为 {n}")


    def on_start(self):
        self.win.start_engine(self.threads.value(), self.delay.value(),
                              "browser")

    def on_stop(self):
        self.win.stop_engine()

    def add_recent(self, d):
        t = self.recent
        t.insertRow(0)
        vals = [datetime.now().strftime("%H:%M:%S"), d.get("email", ""),
                d.get("mode") or "-", d.get("name", ""), d.get("phone", ""),
                d.get("city", ""), d.get("show", ""), str(d.get("elapsed", ""))]
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
        self.table = make_table(["ID", "邮箱", "协议", "姓名", "电话", "生日",
                                 "状态", "错误", "更新时间"])
        lbody.addWidget(self.table, 1)
        rrow = QHBoxLayout()
        rrow.setSpacing(8)
        self.btn_refresh = PushButton(FIF.SYNC, "刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_reset = PushButton(FIF.UPDATE, "重置失败与卡住项")
        self.btn_reset.clicked.connect(self.on_reset)
        rrow.addWidget(self.btn_refresh)
        rrow.addWidget(self.btn_reset)
        rrow.addStretch(1)
        rrow.addWidget(CaptionLabel("清理："))
        self.btn_clean_reg = PushButton(FIF.DELETE, "邮件确认")
        self.btn_clean_reg.clicked.connect(lambda: self.on_clean("registered"))
        self.btn_clean_sub = PushButton(FIF.DELETE, "页面确认")
        self.btn_clean_sub.clicked.connect(lambda: self.on_clean("submitted"))
        self.btn_clean_fail = PushButton(FIF.DELETE, "失败")
        self.btn_clean_fail.clicked.connect(lambda: self.on_clean("failed"))
        self.btn_clean_pending = PushButton(FIF.DELETE, "待注册")
        self.btn_clean_pending.clicked.connect(lambda: self.on_clean("pending"))
        self.btn_clean_all = PushButton(FIF.DELETE, "全部")
        self.btn_clean_all.clicked.connect(lambda: self.on_clean(None))
        for b in (self.btn_clean_reg, self.btn_clean_sub, self.btn_clean_fail,
                  self.btn_clean_pending, self.btn_clean_all):
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

    def on_clean(self, status):
        """Delete a bucket of accounts; registrations follow via CASCADE."""
        stats = self.win.store.stats()
        label = {"registered": "已注册（邮件确认）", "submitted": "页面确认提交",
                 "failed": "失败", "pending": "待注册"}.get(
            status, "全部")
        n = stats.get(status, 0) if status else stats.get("total", 0)
        if not n:
            self.win.notify("warning", f"没有{label}的账号可清理")
            return
        extra = ("\n\n注意：已注册账号的记录会一并删除（预约表通过外键级联）。"
                 if status in (None, "registered", "submitted") else "")
        if not MessageBox("确认清理",
                          f"将永久删除 {n} 条「{label}」账号，不可撤销。{extra}\n\n继续吗？",
                          self).exec():
            return
        removed = self.win.store.delete_accounts(status)
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
        n = self.win.store.reset_failed()
        self.refresh()
        self.win.notify("success", f"{n} 条（失败 + 卡住）已重置为待注册")
        self.win.log("warn", f"重置 {n} 条为待注册")

    def refresh(self):
        s = self.win.store.stats()
        rows = self.win.store.accounts(DISPLAY_LIMIT)
        self.list_card.setTitle(
            f"账号列表（共 {s.get('total', 0)} 条，显示最近 {len(rows)}）")
        fill_row(self.table, [
            [r["id"], r["email"], r.get("protocol") or "graph",
             full_name(r), r["phone"], r["date_of_birth"], r["status"],
             (r["error"] or "")[:60], r["updated_at"]] for r in rows])


class ProxyPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("ProxyPage")
        root = tight(QVBoxLayout(self), PAD, GAP)

        head = QHBoxLayout()
        head.addWidget(TitleLabel("代理池"))
        head.addWidget(CaptionLabel(
            "一行一个代理，支持 http://user:pass@host:port、socks5://…、host:port、"
            "host:port:user:pass。浏览器/混合方式需要 http(s) 上游。"))
        head.addStretch(1)
        root.addLayout(head)

        box, bbody = make_card("代理列表")
        self.text = TextEdit()
        self.text.setPlaceholderText("每行一个，例如 http://user:pass@HOST:PORT 或 socks5://user:pass@HOST:PORT")
        self.text.setFixedHeight(92)
        self.text.setPlainText("\n".join(win.cfg.get("proxies", [])))
        bbody.addWidget(self.text)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_save = PrimaryPushButton(FIF.SAVE, "保存代理池")
        self.btn_save.clicked.connect(self.on_save)
        self.btn_test = PushButton(FIF.SPEED_HIGH, "测试全部")
        self.btn_test.clicked.connect(self.on_test)
        self.btn_clear = PushButton(FIF.DELETE, "清空")
        self.btn_clear.clicked.connect(lambda: self.text.clear())
        for b in (self.btn_save, self.btn_test, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        bbody.addLayout(row)
        root.addWidget(box)

        health, hbody = make_card("健康状态")
        self.table = make_table(["代理", "成功", "失败", "冷却", "最近错误"])
        hbody.addWidget(self.table, 1)
        root.addWidget(health, 1)

    def on_save(self):
        lines = [l for l in self.text.toPlainText().splitlines() if l.strip()]
        n = self.win.pool.load(lines)
        self.win.cfg.set("proxies", lines)
        self.win.cfg.save()
        self.win.notify("success", f"已保存 {n} 个可用代理")
        self.win.log("ok", f"代理池已更新，{n} 个条目")

    def on_test(self):
        urls = self.win.pool.urls()
        if not urls:
            self.win.notify("warning", "代理池为空")
            return
        self.win.log("info", f"测试 {len(urls)} 个代理…")

        def work():
            for u in urls:
                ok, info, cost = registrar.probe_proxy(u)
                self.win.pool.report(u, ok, "" if ok else info)
                self.win.log("ok" if ok else "error",
                             f"  {u.split('@')[-1]} -> "
                             f"{'OK ' + info if ok else info} ({cost}s)")
            # back to the GUI thread for anything that touches a widget
            self.win.bridge.refreshAll.emit()
            self.win.bridge.toast.emit("success", "代理测试完成")
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        fill_row(self.table, [
            [r["url"].split("@")[-1], r["ok"], r["fail"],
             "是" if r["cooling"] else "否", r["last_error"][:60]]
            for r in self.win.pool.snapshot()])


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

        adv, abody = make_card("请求参数")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.link_timeout = CompactSpinBox()
        self.link_timeout.setRange(30, 3600)
        self.link_timeout.setValue(int(win.cfg.get("link_timeout", 300)))
        self.link_timeout.setFixedWidth(110)
        self.db_path = LineEdit()
        self.db_path.setText(str(win.cfg.get("db_path", "oasis.db")))
        self.log_file = LineEdit()
        self.log_file.setText(str(win.cfg.get("log_file", "oasis_run.log")))
        self.mail_proxy = LineEdit()
        self.mail_proxy.setPlaceholderText(
            "留空 = 直连（推荐）。仅在网络必须走代理时才填，例如 socks5://127.0.0.1:1080")
        self.mail_proxy.setText(str(win.cfg.get("mail_proxy", "")))
        self.google_proxy = LineEdit()
        self.google_proxy.setPlaceholderText(
            "留空 = 不分流，Google 与注册流量走同一个上游。"
            "仅当上游屏蔽 Google（reCAPTCHA 加载不了）时才填一个能访问 Google 的代理")
        self.google_proxy.setText(str(win.cfg.get("google_proxy", "")))
        self.hme_base = LineEdit()
        self.hme_base.setPlaceholderText(
            "本地 iCloud 隐藏邮箱服务地址，默认 http://127.0.0.1:8081")
        self.hme_base.setText(str(win.cfg.get("hme_base", "http://127.0.0.1:8081")))
        self.hme_password = LineEdit()
        self.hme_password.setEchoMode(LineEdit.EchoMode.Password)
        self.hme_password.setPlaceholderText(
            "该服务的管理员密码（启动时的 ICLOUD_HME_ADMIN_PASSWORD）")
        self.hme_password.setText(str(win.cfg.get("hme_password", "")))
        self.front_proxy = LineEdit()
        self.front_proxy.setPlaceholderText(
            "留空 = 直连上游。短效住宅代理在国内常无法直连时填，例如 socks5://127.0.0.1:10808。"
            "填了之后所有上游都经它拨号（自动链式），HTTP 模式也会改走本地 relay")
        self.front_proxy.setText(str(win.cfg.get("front_proxy", "")))
        self.success_timeout = CompactSpinBox()
        self.success_timeout.setRange(0, 900)
        self.success_timeout.setValue(int(win.cfg.get("success_timeout", 180)))
        self.success_timeout.setFixedWidth(110)
        self.success_timeout.setToolTip(
            "只在页面没确认注册时才等满这个时长（那时邮件是唯一证据）。\n"
            "页面已经确认的情况下最多再看 30 秒——浏览器模式实测不发成功邮件。")
        self.delay_between = CompactDoubleSpinBox()
        self.delay_between.setRange(0.0, 30.0)
        self.delay_between.setSingleStep(0.5)
        self.delay_between.setValue(float(win.cfg.get("delay_between", 0.0)))
        self.delay_between.setFixedWidth(110)
        self.delay_between.setToolTip("每个账号跑完后的停顿，0 = 不停顿。")
        self.verify_success = SwitchButton()
        self.verify_success.setChecked(bool(win.cfg.get("verify_success", False)))
        self.verify_success.setToolTip(
            "开启后，confirm 返回 OK 之后还会等「Registration Complete」邮件确认。\n"
            "必须知道：confirm 对「被拒绝的地址」同样返回 OK，不看邮件就会把失败记成成功。\n"
            "代价是每个账号多等最多 180 秒。")
        self.debug = SwitchButton()
        self.debug.setChecked(bool(win.cfg.get("debug", False)))

        rows = [                ("等邮件超时(秒)", self.link_timeout),
                ("成功后校验邮件", self.verify_success),
                ("成功邮件超时(秒)", self.success_timeout),
                ("账号间停顿(秒)", self.delay_between),
                ("取件代理", self.mail_proxy),
                ("iCloud 服务", self.hme_base),
                ("iCloud 密码", self.hme_password),
                ("前置代理(链路)", self.front_proxy),
                ("Google 分流代理", self.google_proxy),
                ("数据库路径", self.db_path),
                ("日志文件", self.log_file),
                ("调试堆栈", self.debug)]
        for i, (label, w) in enumerate(rows):
            grid.addWidget(BodyLabel(label), i, 0)
            grid.addWidget(w, i, 1)
        grid.setColumnStretch(1, 1)
        abody.addLayout(grid)
        root.addWidget(adv)

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
            "link_timeout": self.link_timeout.value(),
            "verify_success": self.verify_success.isChecked(),
            "success_timeout": self.success_timeout.value(),
            "delay_between": self.delay_between.value(),
            "mail_proxy": self.mail_proxy.text().strip(),
            "hme_base": self.hme_base.text().strip(),
            "hme_password": self.hme_password.text().strip(),
            "google_proxy": self.google_proxy.text().strip(),
            "front_proxy": self.front_proxy.text().strip(),
            "db_path": self.db_path.text().strip() or "oasis.db",
            "log_file": self.log_file.text().strip() or "oasis_run.log",
            "debug": self.debug.isChecked()})
        self.win.cfg.save()
        self.win.notify("success", "设置已保存")


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

        reg, rbody = make_card("预约记录（account + artist 唯一）")
        self.reg_card = reg
        self.reg_table = make_table(["ID", "邮箱", "方式", "场次答案", "状态", "时间"])
        rbody.addWidget(self.reg_table, 1)
        root.addWidget(reg, 1)

        acc, abody = make_card("账号与身份")
        self.acc_card = acc
        self.acc_table = make_table(["ID", "邮箱", "协议", "姓名", "电话", "生日",
                                     "状态", "更新时间"])
        abody.addWidget(self.acc_table, 1)
        root.addWidget(acc, 1)

    def refresh(self):
        s = self.win.store.stats()
        regs = self.win.store.registrations(DISPLAY_LIMIT)
        accs = self.win.store.accounts(DISPLAY_LIMIT)
        self.reg_card.setTitle(
            f"预约记录（共 {s.get('registrations', 0)} 条，显示最近 {len(regs)}）")
        self.acc_card.setTitle(
            f"账号与身份（共 {s.get('total', 0)} 条，显示最近 {len(accs)}）")
        fill_row(self.reg_table, [
            [r["id"], r["email"], r.get("mode") or "-", r["poll_answer_ids"],
             r["status"], r["created_at"]] for r in regs])
        fill_row(self.acc_table, [
            [r["id"], r["email"], r.get("protocol") or "graph", full_name(r),
             r["phone"], r["date_of_birth"], r["status"], r["updated_at"]]
            for r in accs])

    def on_csv(self):
        """Export every registration - the table view is capped, this is not."""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV（全量）", os.path.join(BASE, "oasis_data.csv"),
            "CSV (*.csv)")
        if not path:
            return
        regs = self.win.store.registrations()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "email", "mode", "poll_answer_ids", "status",
                        "created_at"])
            for r in regs:
                w.writerow([r["id"], r["email"], r.get("mode") or "-",
                            r["poll_answer_ids"], r["status"], r["created_at"]])
        self.win.notify("success",
                        f"已导出全部 {len(regs)} 条预约记录（不受表格显示上限影响）")
        self.win.log("ok", f"导出 CSV：{len(regs)} 条 -> {os.path.basename(path)}")


# ----------------------------------------------------------------- main window
class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        setThemeColor("#d9822b")
        self.cfg = Config(os.path.join(BASE, "oasis_config.json"))
        self.store = Store(self.cfg.db_path)
        self.pool = ProxyPool(self.cfg.get("proxies", []))
        self.bridge = Bridge()
        self.engine = Engine(self.store, self.pool, self._engine_log,
                             self._engine_event, self.cfg.data)

        self.dash = DashboardPage(self)
        self.mail = MailboxPage(self)
        self.proxy = ProxyPage(self)
        self.data = DataPage(self)
        self.logs = LogPage(self)
        self.settings = SettingsPage(self)

        self.addSubInterface(self.dash, FIF.HOME, "仪表盘")
        self.addSubInterface(self.mail, FIF.MAIL, "邮箱池")
        self.addSubInterface(self.proxy, FIF.GLOBE, "代理池")
        self.addSubInterface(self.data, FIF.DOCUMENT, "数据库")
        self.addSubInterface(self.logs, FIF.VIEW, "日志")
        self.addSubInterface(self.settings, FIF.SETTING, "设置",
                             NavigationItemPosition.BOTTOM)

        # the default expanded sidebar is 322px wide and eats the content area
        self.navigationInterface.setExpandWidth(168)
        shrink_buttons(self)

        self.resize(1320, 840)
        self.setMinimumSize(1080, 680)
        self.setWindowTitle("Oasis Live '27 注册控制台")
        self._log_fh = None

        self.bridge.log.connect(self._on_log)
        self.bridge.stats.connect(self._on_stats)
        self.bridge.registered.connect(self._on_registered)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.stopped.connect(self._on_stopped)
        self.bridge.toast.connect(self.notify)
        self.bridge.refreshAll.connect(self._refresh_all)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)
        self._refresh_all()
        self.log("info", "控制台就绪。浏览器模式：驱动官方 SPA 表单，"
                         "captcha 由页面自己签，提交也由 SPA 发出。")

    # ---------------------------------------------------------------- plumbing
    def _engine_log(self, level, msg):
        self.bridge.log.emit(level, msg)

    def _engine_event(self, kind, payload):
        payload = payload or {}
        signal = {"stats": self.bridge.stats,
                  "registered": self.bridge.registered,
                  "failed": self.bridge.failed,
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

    def _on_registered(self, d):
        self.dash.add_recent(d)
        self.notify("success", f"{d.get('email')} 注册成功")
        self.data.refresh()

    def _on_failed(self, d):
        self.notify("error", f"{d.get('email')} 失败：{d.get('error', '')[:60]}")

    def _on_stopped(self, payload):
        self.dash.btn_start.setEnabled(True)
        self.dash.btn_stop.setEnabled(False)
        self.log("warn", "引擎已停止")
        self._refresh_all()

    def _apply_stats(self, s):
        for k, card in self.dash.cards.items():
            card.set_value(s.get(k, 0))
        total = s.get("total", 0)
        done = s.get("registered", 0) + s.get("failed", 0)
        self.dash.progress.setValue(int(done / total * 100) if total else 0)

    def _tick(self):
        if self.engine.running:
            self._apply_stats(self.store.stats())

    def _refresh_all(self):
        self._apply_stats(self.store.stats())
        self.mail.refresh()
        self.proxy.refresh()
        self.data.refresh()
        if not self.engine.running:
            self.dash.run_hint.setText(self.dash.idle_hint())

    # ----------------------------------------------------------------- control
    def start_engine(self, threads, delay, mode="browser"):
        self.cfg.set("threads", threads)
        self.cfg.set("delay_between", delay)
        self.cfg.set("mode", mode)
        self.cfg.save()
        self.engine.config = self.cfg.data
        order = [s for s in self.cfg.get("shows", registrar.DEFAULT_ORDER)
                 if s in registrar.SHOWS]
        if len(order) != 3 or len(set(order)) != 3:
            self.notify("error", "场次偏好必须是三个不重复的场次")
            return
        self.dash.btn_start.setEnabled(False)
        self.dash.btn_stop.setEnabled(True)
        self.dash.run_hint.setText(
            f"运行中：{MODE_LABEL.get(mode, mode)} · {threads} 线程 · " +
            " > ".join(registrar.SHOW_LABEL[s] for s in order))
        self.engine.start(threads=threads, order=order,
                          link_timeout=int(self.cfg.get("link_timeout", 300)),
                          delay_between=delay, mode=mode)
        threading.Thread(target=self._wait_done, daemon=True).start()

    def _wait_done(self):
        self.engine.join()
        self.engine.finish()

    def stop_engine(self):
        self.engine.stop()
        self.dash.run_hint.setText("正在停止（等待当前账号完成）…")

    def closeEvent(self, e):
        if self.engine.running:
            if not MessageBox("确认退出", "引擎仍在运行，确定退出吗？", self).exec():
                e.ignore()
                return
            self.engine.stop()
        # browser mode keeps one shared chromium alive between accounts; nothing
        # else shuts it down, so an exited console would leave it running.
        try:
            self.engine.browser.close_warm()
        except Exception:
            pass
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
