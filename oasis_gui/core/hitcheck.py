#!/usr/bin/env python3
"""中签判定：把「收件箱里的一封信」和「库里的一条旧记录」折算成同一个结论。

活动结束后这套程序只做一件事 —— 盯着已预约账号的收件箱，看 Oasis 有没有来信。
判定必须合并两路证据，因为现场同时存在这两种已成交的账号：

  1. 邮件证据：Oasis 来信。正文含成功标记的（实测 4/4 成功信含
     "successfully registered"，0/15 验证信含）是硬证据；其余只要确实是 Oasis
     发来的新信，也按「新来信」计入 —— 活动结束后 Oasis 不会无缘无故再发信。

  2. 站点证据（merge 点）：账号在上一版程序里已经被站点确认过，只是它没发成功
     邮件。实测 `confirm` 对已接受的地址回 {"status":"OK"}，紧接着的成功邮件
     却始终不来，旧程序把它记成

         confirm answered OK but the success mail never arrived within 180s

     并按失败处理；浏览器模式更直接 —— 页面渲染出确认页、根本不发成功邮件，
     旧程序记成 `submitted`。这两种记录的本质是同一件事：**站点已经收下了**。
     活动已经结束，不可能再收到那封邮件，所以它们必须和中签信进同一张名单，
     否则会被「没有成功邮件」这个假象整批吞掉。判定因此把
     `submitted` 与带 `NO_MAIL_OK_MARK` 的错误文本一起折算成 `site-ok` 命中。
"""
import re

# --- 邮件证据 ---------------------------------------------------------------
# 实测：成功信正文含此串，验证信一封都不含。
SUCCESS_MARKS = ("successfully registered",)
# 成功信的标题（`email.header.decode_header` 解出来是这句）。标题不是判据的
# 主体 —— 站点把它 MIME 编码成 `Oasis_Live_=E2=80=9927_Registration_Complete`，
# 拿原始 header 去 grep 永远匹配不上，这点在旧版程序上栽过一次。
SUBJECT_MARKS = ("registration complete",)

# 验证信 —— 必须排除，否则整个名单会被它污染。
#
# 实测（2026-09-18，真实 Outlook 邮箱，回看窗口 30 天）：注册期间每个账号都被
# 自己触发过一封 "Verify Your Email"，它同样是「Oasis 的新来信」。若只看
# 「有没有新来信」，这些账号会全部被判成中签 —— 判定的是注册动作，不是结果。
# 两种指纹都能认出它，取交集之外的任何一个即可：
#   * 标题就是 "Verify Your Email"
#   * 正文里带 registration?token= 的一次性链接
VERIFY_SUBJECT_MARKS = ("verify your email", "verify email", "confirm your email")
VERIFY_LINK_RE = re.compile(
    r"oasis\.hq\.fan/registration\?token=", re.IGNORECASE)

# Oasis 来信的指纹。域名与 Apple 遮蔽后的地址形态是硬指纹；「oasis live」短语
# 用来兜住正文被截断、只剩标题的情况。单独一个 "oasis" 不能用 —— 这个词在
# 别家邮件里也出现，拿它当判据会把别人的信算成中签。
OASIS_STRINGS = (
    "oasis.hq.fan",
    "openstage.live",
    "openstageit.com",
    "openstageit_com",
    "oasis_at_openstageit_com",
    "oasis live",
    "oasis_live",
)

# --- 站点证据（merge 点） ---------------------------------------------------
# registrar.py 在 confirm 返回 OK 却等不到成功邮件时抛出的原文片段。它字面上是
# 个错误，语义上却是成功：站点已经接受，只是信没来。
NO_MAIL_OK_MARK = "success mail never arrived"
# 浏览器模式一条成功邮件都不发，页面确认就是唯一证据，旧程序记在这两句里。
LEGACY_SITE_NOTES = ("页面确认注册成功", "SUCCESS")


def normalise(text):
    """压平一封邮件，供关键字匹配。

    MIME 的 Q 编码把空格写成下划线，`decode_header` 之后标题里仍会留下痕迹，
    所以下划线一并当作空格看待。
    """
    return (text or "").replace("_", " ").lower()


def mail_text(mail):
    """(subject, sender, body) -> 一个可匹配的串。"""
    return f"{normalise(mail.subject)}\n{normalise(mail.sender)}\n{normalise(mail.body)}"


def is_oasis_mail(mail):
    hay = mail_text(mail)
    return any(s in hay for s in OASIS_STRINGS)


def has_success_mark(mail):
    """成功邮件的判据。

    `successfully registered` 只在正文里查 —— 实测 15 封验证信的正文一封都不含；
    标题判据查整封，因为它本来就长在标题上。
    """
    body = normalise(mail.body)
    if any(m in body for m in SUCCESS_MARKS):
        return True
    return any(s in normalise(mail.subject) for s in SUBJECT_MARKS)


def is_verification(mail):
    """注册流程自己触发的那种信，不是结果。"""
    return (any(s in normalise(mail.subject) for s in VERIFY_SUBJECT_MARKS)
            or bool(VERIFY_LINK_RE.search(mail.body or "")))


def classify(mail):
    """Oasis 来信的种类，或 None（不是 Oasis 的信）。

    "success" -> 成功/结果邮件，直接中签
    "oasis"   -> 确实是 Oasis 的信，又不是验证信 —— 结果类来信
    "verify"  -> 注册期自己触发的验证信，登记但不计中签
    """
    if not is_oasis_mail(mail):
        return None
    if has_success_mark(mail):
        return "success"
    if is_verification(mail):
        return "verify"
    return "oasis"


def legacy_site_ok(status, error="", response=""):
    """这条账号记录是否代表「站点已经收下」？

    覆盖三种旧记录，都是同一件事的不同写法：

      * `submitted` —— 页面确认，没发成功邮件（浏览器模式一律如此）
      * 错误文本含 NO_MAIL_OK_MARK —— confirm 回 OK，成功邮件 180s 没到
      * 上面的错误被写进了 registrations.response
    """
    blob = f"{error or ''}\n{response or ''}"
    if NO_MAIL_OK_MARK in blob:
        return True
    if (status or "") == "submitted":
        return True
    if (status or "") == "failed":
        return any(n in blob for n in LEGACY_SITE_NOTES)
    return False


def pick_oasis(mails, baseline_at=0.0, success_regardless=False):
    """从一堆信里挑出这一账号的结论。

    baseline_at 之前的 Oasis 来信不算「新来信」—— 否则导入的账号会立刻被上一轮
    活动留下的信糊满。但命中成功标记的信不看基线：那是结果本身，看见就算数。
    验证信（kind == "verify"）永远不算中签，只登记为「最近来信」：那封信是注册
    动作的回声，不是结果，把它当结果会让整张名单失去意义。

    `success_regardless` 关掉基线（回看窗口设为 0 时用），把所有历史 Oasis 来信
    都纳进来。

    返回 dict：
        first_new   最新一封基线之后 Oasis 结果信的 epoch（没有则 None）
        subject     那封信的标题
        success     (stamp, subject) 命中成功标记的最早一封信
        latest      (stamp, subject) 邮箱里最新一封 Oasis 来信（含验证信）
        n_oasis     Oasis 来信封数（含验证信）
        n_verify    其中验证信的封数
        n_total     邮箱里看到的信的总数
    """
    out = {"first_new": None, "subject": "", "success": None,
           "latest": None, "n_oasis": 0, "n_verify": 0, "n_total": len(mails)}
    for mail in mails:
        kind = classify(mail)
        if not kind:
            continue
        out["n_oasis"] += 1
        if kind == "verify":
            out["n_verify"] += 1
        stamp = mail.stamp or 0
        if out["latest"] is None or stamp >= (out["latest"][0] or 0):
            out["latest"] = (stamp, mail.subject)
        if kind == "success":
            # 取最早的那封：同一件事重复投递时，最早的才是结果本体。
            if out["success"] is None or stamp < out["success"][0]:
                out["success"] = (stamp, mail.subject)
        if kind == "verify":
            continue
        fresh = success_regardless or not baseline_at or stamp > baseline_at
        if fresh and (out["first_new"] is None or stamp > out["first_new"]):
            out["first_new"] = stamp
            out["subject"] = mail.subject
    return out


# --- 命中的来源标签，UI 与库里都用这三个值 --------------------------------
SOURCE_SUCCESS_MAIL = "success-mail"   # 收到 Oasis 的成功/结果邮件
SOURCE_OASIS_MAIL = "oasis-mail"       # 收到 Oasis 的其他新来信
SOURCE_SITE_OK = "site-ok"             # 站点已确认、活动结束后不可能再收到成功邮件

SOURCE_LABEL = {
    SOURCE_SUCCESS_MAIL: "成功邮件",
    SOURCE_OASIS_MAIL: "Oasis 来信",
    SOURCE_SITE_OK: "站点已确认（无成功邮件）",
}
