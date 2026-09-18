#!/usr/bin/env python3
"""中签判定：只有「注册截止之后收到的 Oasis 结果信」才算中签。

预约与中签是两件事，把它们混起来是这个程序犯过的最大一个错：

  * **预约（registration）** 在 2026-09-17 16:00 BST 截止。注册期内收到的每一封
    信 —— 包括那封写着 "successfully registered" 的 Registration Complete ——
    都只说明「这个地址预约成功了」，一张票都没拿到。
  * **中签（ballot result）** 是 Oasis 之后发的结果通知，只可能出现在截止之后。

上一版把注册成功当成中签，于是名单上出现几百个「已中签」。那不只是数字难看：
那些账号会被 `skip_hits` 当成已经查过，真正的结果信到了反而不查。

判据因此只有一条硬的：**信比截止时间晚**。外加两条排除 —— 不是注册期那种带
token 链接的验证信，也不是明说「注册成功」的信。注册截止之后 Oasis 不会无缘无故
再发信，所以「截止后的新 Oasis 来信」就是结果。
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

# --- 注册截止时间 -----------------------------------------------------------
# Oasis 官方：Registration closes on Thursday 17 September at 4pm BST ——
# 16:00 BST == 15:00 UTC。中签（结果）信只可能出现在这之后。
DEFAULT_CUTOFF = "2026-09-17T15:00:00Z"

# 上一版程序留下的、代表「站点已经接受这次预约」的痕迹。
#
# 它字面上是个错误（confirm 回了 OK 却等不到成功邮件），语义上却是成功 —— 只不
# 过那个「成功」是**预约成功**，不是中签。现在它只用来推断「这个地址预约过」，
# 不再作为中签依据。
NO_MAIL_OK_MARK = "success mail never arrived"
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


def parse_cutoff(value, default=None):
    """把配置里的截止时间解析成 epoch。

    接受 `2026-09-17T15:00:00Z`（官方声明用的就是 UTC），也接受不带时区的
    `2026-09-17 15:00` —— 后者按**本地时间**理解，因为那是操作者在界面上敲进去
    的形状。解析不出来时退回 default，并且不抛：一个填错的截止时间不该让整个
    检测停摆。
    """
    text = str(value or "").strip()
    if not text:
        return default
    try:
        from datetime import datetime, timezone
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.astimezone()          # 无时区 -> 当成本地时间
        return dt.timestamp()
    except Exception:
        return default


def format_cutoff(epoch):
    """epoch -> 本地时间 + 时区缩写，给人看。

    时区必须带上：官方说的是 16:00 BST，同一个时刻在 UTC 里是 15:00。只显示
    「2026-09-17 15:00」，在一个 UTC 的容器里和在一个 +08:00 的机器上指的是
    不同时刻 —— 而这个时间决定了哪些信算结果，含糊不得。
    """
    import time as _time
    if not epoch:
        return ""
    return _time.strftime("%Y-%m-%d %H:%M %Z", _time.localtime(epoch)).strip()


def pick_result(mails, cutoff_at):
    """从一堆信里挑出这一账号的中签结论。

    只认截止之后的非验证、非「注册成功」的 Oasis 来信。截止之前的信一律不计 ——
    这就是「预约成功」与「中签」的分界线。

    返回 dict：
        result      (stamp, subject) 最早那封结果信（没有则 None）
        latest      (stamp, subject) 邮箱里最新一封 Oasis 来信（含注册期的）
        n_oasis     Oasis 来信封数
        n_verify    其中验证信
        n_early     其中早于截止时间的（预约期的回声）
        n_total     看到的信的总数
    """
    out = {"result": None, "latest": None, "n_oasis": 0, "n_verify": 0,
           "n_early": 0, "n_total": len(mails)}
    for mail in mails:
        kind = classify(mail)
        if not kind:
            continue
        out["n_oasis"] += 1
        stamp = mail.stamp or 0
        if out["latest"] is None or stamp >= (out["latest"][0] or 0):
            out["latest"] = (stamp, mail.subject)
        if kind == "verify":
            out["n_verify"] += 1
            continue
        if kind == "success":
            # 明说「注册成功」的信：预约的证据，不是结果。
            out["n_early"] += 1
            continue
        if cutoff_at and stamp <= cutoff_at:
            out["n_early"] += 1
            continue
        if out["result"] is None or stamp < out["result"][0]:
            out["result"] = (stamp, mail.subject)
    return out


# --- 命中的来源标签 ---------------------------------------------------------
# 只剩一个来源。曾经还有「成功邮件」与「站点已确认」，那两个描述的其实是**预约**
# 成功 —— 把它们当中签，就是这一版要修的问题。
SOURCE_OASIS_MAIL = "oasis-mail"

SOURCE_LABEL = {
    SOURCE_OASIS_MAIL: "结果信（注册截止后的 Oasis 来信）",
}
