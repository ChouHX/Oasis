#!/usr/bin/env python3
"""Operator settings, persisted as JSON next to the database.

The program is a hit monitor: it only ever reads mailboxes. So the settings are
the rhythm of that reading (how often, how many at once, how far back) and the
three ways a mailbox can be reached.

An old config file still carries the registration keys it was born with. They
are dropped on load rather than left in place: a `mode: browser` sitting in the
file would keep implying that something drives a browser here.
"""
import json
import os

# 上一版程序留下的键，加载时丢掉。留着它们只会让配置文件说谎。
RETIRED_KEYS = (
    "mode", "shows", "link_timeout", "verify_success", "success_timeout",
    "proxies", "front_proxy", "google_proxy",
    # 从没接到 monitor 上过：账号之间的停顿由 monitor 的 PAGE_GAP 承担。
    "delay_between",
    # 被 ballot_cutoff 取代：判定基线现在是「注册截止」，不再是一个回看天数。
    "lookback_days",
)

DEFAULTS = {
    # --- where things live -------------------------------------------------
    "db_path": "oasis.db",
    # --- 检测节奏 ----------------------------------------------------------
    # 一轮跑完到下一轮开始之间的等待。5 分钟是刻意的：中签通知不是秒级事件，
    # 而一个账号一轮就是一次 IMAP 登录加一次 SEARCH，频率再高只是把对方的收件箱
    # 打成请求尖峰。
    "interval": 300,
    # 同时打开几条收件箱连接 —— 不是同时读几封信：同一个 Gmail 收件箱下的别名
    # 共用一条连接，组与组之间才并行。
    "threads": 2,
    # 注册截止时间（UTC）。Oasis 官方：Registration closes on Thursday
    # 17 September at 4pm BST == 15:00 UTC。中签结果信只可能出现在这之后，
    # 此前的每一封 Oasis 信都只是「预约成功」的回声 —— 判定把它当作硬门槛。
    # 格式：2026-09-17T15:00:00Z（也可写成本地时间的 2026-09-17 23:00）。
    "ballot_cutoff": "2026-09-17T15:00:00Z",
    # 每个邮箱取最近多少封信来判断。
    "per_page": 20,
    # 只检测「预约过」的地址。没预约过的邮箱不会收到中签信，扫它纯属浪费 ——
    # 一个八百人的池子里可能只有一半提交过注册。已预约的判定来自三处：上一版
    # 程序留下的成功记录（自动并入）、导入时的标记、界面上的手工标记。
    "only_opted": True,
    # 服务端只拉 Oasis 的来信（发件人 openstage 或标题含 Oasis），而不是把
    # 「最近 N 封」整个拉回来再本地挑。首次检测一个账号时始终走全量，之后才粗筛。
    # 判据来自实测：验证信与成功邮件都是 oasis@openstageit.com 发的。若哪天站点
    # 换了发件人域、而结果信标题里又没有 Oasis，把它关掉即可。
    "mail_filter": True,
    # 中签后是否继续检测。默认跳过：同一个答案重复搜索没有意义，省下一整轮的
    # 无用登录；若想连后续的付款/取票通知一起盯，把它关掉。
    "skip_hits": True,
    # 一轮最多检测多少个账号；0 = 全部。用于「先拿一两个试」。
    "limit": 0,
    # --- 取件通道 ----------------------------------------------------------
    # Mailbox fetches go direct unless this is set.
    "mail_proxy": "",
    # --- iCloud Hide-My-Email service (see core.mailbox.HmeMailbox) --------
    "hme_base": "http://127.0.0.1:8081",
    "hme_password": "",
    # --- iCloud aliases read through Gmail (see core.mailbox.GmailAliasMailbox)
    # An alias is only a forwarding address: what is sent to it lands in the
    # Gmail account that owns the Apple ID, so one inbox credential covers the
    # whole alias list. Paste bare alias addresses at import time and these fill
    # in the rest of the credential line.
    "alias_inbox": "",
    "alias_inbox_password": "",
    # --- diagnostics -------------------------------------------------------
    "debug": False,
    # Desktop-only: the console writes its own log file. The service logs to
    # stdout, which is where docker picks it up.
    "log_file": "oasis_run.log",
}


class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                self.data.update(stored)
        except Exception:
            pass
        for key in RETIRED_KEYS:
            self.data.pop(key, None)
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        self.data[key] = value

    def update(self, mapping):
        self.data.update({k: v for k, v in mapping.items()
                          if k not in RETIRED_KEYS})

    @property
    def db_path(self):
        p = self.get("db_path")
        return p if os.path.isabs(p) else os.path.join(
            os.path.dirname(self.path), p)
