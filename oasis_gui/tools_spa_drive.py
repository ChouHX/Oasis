#!/usr/bin/env python3
"""CDP 驱动官方 SPA 走完整个注册流程，并抓下它自己发出的 confirm。

这是**对照基准**：我们的 hybrid/browser 模式是「页面里发我们自己拼的 body」，
这个脚本是「让 SPA 自己提交」——包括它自己填 ip、county、consentMessaging。

用法：
    OASIS_PROXY='socks5://user-sid-x:pass@host:3000' \
    OASIS_LINK=/tmp/link.txt  python3 tools_spa_drive.py

前置：/tmp/link.txt 里放一封验证邮件的链接。
产出：屏幕日志（每步页面状态）+ /tmp/spa_confirm.json（SPA 自己的请求体）
"""
import os, sys, json, time, random
sys.path.insert(0, "/home/beacon/dev/ai/tickets/oasis/oasis_gui")
os.chdir("/home/beacon/dev/ai/tickets/oasis/oasis_gui")
from core import identity
from core.relay import RelayPool
from core.browser_registrar import BrowserRegistrar
UP = os.environ.get("OASIS_PROXY", "") or sys.exit(
    "set OASIS_PROXY, e.g. socks5://user-sid-XXXX:pass@us.1024proxy.io:3000")
FRONT = os.environ.get("OASIS_FRONT_PROXY", "socks5://127.0.0.1:10808")
LINK = open(os.environ.get("OASIS_LINK", "/tmp/cdp_link.txt")).read().strip()
ident = identity.random_identity(country="US")
pool = RelayPool(front=FRONT, google_fallback="127.0.0.1:10808", log=lambda m: None)
br = BrowserRegistrar(pool, headless=True, log=lambda m: None)
from playwright.sync_api import sync_playwright
b1 = br._shared_browser(pool.get(UP).url)
pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp(b1.endpoint())
ctx = browser.new_context(viewport={"width":1366,"height":768},
    user_agent=br._user_agent(random.Random()), locale="en-US", timezone_id="America/New_York")
ctx.add_init_script(br._stealth_js(random.Random()))
page = ctx.new_page()
cap = []
page.on("request", lambda r: cap.append(("REQ", r.url.split("/fan2/")[-1], r.post_data))
        if ("openstage.live" in r.url and r.method=="POST" and "telemetry" not in r.url) else None)
page.on("response", lambda r: cap.append(("RESP", r.status, r.url.split("/fan2/")[-1], r.text()[:250]))
        if ("openstage.live" in r.url and r.request.method=="POST" and "telemetry" not in r.url) else None)
for k in range(3):
    try: page.goto(LINK, wait_until="domcontentloaded", timeout=120000); break
    except Exception: pass
page.wait_for_timeout(6000)
# 资料
page.fill("#soundcheckConfirmFirstName", ident["first_name"]); page.wait_for_timeout(300)
page.fill("#soundcheckConfirmLastName", ident["last_name"]); page.wait_for_timeout(300)
for a in range(3):
    page.click("input#soundcheckConfirmPhoneNumber")
    page.fill("input#soundcheckConfirmPhoneNumber",""); page.wait_for_timeout(300)
    page.type("input#soundcheckConfirmPhoneNumber", ident["phone"], delay=110)
    page.wait_for_timeout(700); page.keyboard.press("Tab"); page.wait_for_timeout(1100)
    if "Please enter a valid phone number" not in page.inner_text("body"): break
page.click("#birthDate"); page.wait_for_timeout(2000)
for tid, val in (("#year", str(ident["dob_year"])), ("#month", ident["dob_month"])):
    try:
        page.click(tid, force=True, timeout=6000); page.wait_for_timeout(1300)
        page.click(f'[role=option]:has-text("{val}")', timeout=6000); page.wait_for_timeout(1300)
    except Exception: page.keyboard.press("Escape")
cal = page.query_selector("[data-testid=calendar]")
if cal:
    for c in cal.query_selector_all("button, [role=gridcell], td"):
        if (c.inner_text() or "").strip() == str(ident["dob_day"]):
            try: c.click(); break
            except Exception: pass
page.wait_for_timeout(1200)
for a in range(3):
    page.click("input#soundcheckConfirmLocation"); page.fill("input#soundcheckConfirmLocation","")
    page.type("input#soundcheckConfirmLocation", ident["location_query"], delay=100)
    page.wait_for_timeout(4500)
    o = page.query_selector_all("[role=option]")
    if o: o[0].click(); page.wait_for_timeout(1200); break
page.click("button:has-text('Continue')"); page.wait_for_timeout(9000)
print("进入城市步骤:", "choose up to three cities" in page.inner_text("body"), flush=True)

# ---- 第一偏好：点开 multiselect ----
ms = page.query_selector("[data-testid=multiselect]")
btn = ms.query_selector("[role=combobox]") if ms else None
if btn:
    btn.click(); page.wait_for_timeout(2000)
    opts = page.query_selector_all("[role=option]")
    print(f"\n第一偏好选项 {len(opts)} 个:", flush=True)
    for i, o in enumerate(opts[:8]):
        print(f"   {i}: {(o.inner_text() or '').strip()[:40]!r}", flush=True)
    if opts:
        opts[0].click(); page.wait_for_timeout(1200)
        # 多选：再点两个
        for want in (1, 2):
            if len(opts) > want:
                try: opts[want].click(); page.wait_for_timeout(1000)
                except Exception: pass
        page.keyboard.press("Escape"); page.wait_for_timeout(800)
print("第一偏好已选:", flush=True)

# ---- travel 下拉 ----
cbs = page.query_selector_all("[role=combobox]")
print(f"\n当前 combobox 数: {len(cbs)}", flush=True)
for i, c in enumerate(cbs):
    print(f"   {i}: {(c.inner_text() or '').strip()[:40]!r}", flush=True)
travel = None
for c in cbs:
    if "Select an option" in (c.inner_text() or ""): travel = c; break
if travel:
    travel.click(); page.wait_for_timeout(1800)
    topts = page.query_selector_all("[role=option]")
    print(f"travel 选项 {len(topts)} 个:", flush=True)
    for i, o in enumerate(topts[:6]):
        print(f"   {i}: {(o.inner_text() or '').strip()[:40]!r}", flush=True)
    if topts:
        topts[0].click(); page.wait_for_timeout(1200)

page.screenshot(path="/tmp/city3.png", full_page=True)
print("\n=== 继续走剩余步骤 ===", flush=True)
for step in range(8):
    txt = page.inner_text("body")
    if "Check your email" in txt or "registration complete" in txt.lower():
        print(f"  [{step}] 到达终点", flush=True); break
    first = txt[:150].replace("\n"," | ")
    print(f"  [{step}] {first}", flush=True)
    # 勾选所有可见复选框
    for cb in page.query_selector_all("[role=checkbox], input[type=checkbox]"):
        try:
            if cb.is_visible() and not cb.is_checked():
                cb.click(); page.wait_for_timeout(400)
        except Exception: pass
    # 下拉：优先 "Select"
    for c in page.query_selector_all("[role=combobox]"):
        try:
            t = (c.inner_text() or "")
            if "Select" in t and c.is_visible():
                c.click(); page.wait_for_timeout(1500)
                opts = page.query_selector_all("[role=option]")
                if opts:
                    print(f"        下拉 {t.strip()[:24]!r} -> 选 {opts[0].inner_text().strip()[:30]!r}", flush=True)
                    opts[0].click(); page.wait_for_timeout(1200)
        except Exception: pass
    # 选项卡片
    for sel in ("[data-testid=option]", "[role=radio]", "[data-testid=card-label]"):
        for e in page.query_selector_all(sel):
            try:
                if e.is_visible():
                    e.click(); page.wait_for_timeout(700); break
            except Exception: pass
    try:
        btn = page.query_selector("button:has-text('Continue')") or \
              page.query_selector("button:has-text('Submit')") or \
              page.query_selector("button:has-text('Complete')") or \
              page.query_selector("button:has-text('Register')")
        if btn and btn.is_enabled():
            btn.click(); page.wait_for_timeout(11000)
            print("        点了 Continue", flush=True)
        else:
            print("        没找到可点的按钮", flush=True)
    except Exception as e:
        print("        点击失败", type(e).__name__, flush=True)

txt = page.inner_text("body")
print("\n=== T&C 页所有 button ===", flush=True)
for i, b in enumerate(page.query_selector_all("button")):
    try:
        print(f"  {i}: {repr((b.inner_text() or '').strip()[:40])} disabled={b.is_disabled()} "
              f"visible={b.is_visible()} testid={b.get_attribute('data-testid')}", flush=True)
    except Exception: pass
print("\n=== 复选框 ===", flush=True)
for i, c in enumerate(page.query_selector_all("[role=checkbox], input[type=checkbox]")):
    try:
        print(f"  {i}: checked={c.is_checked() if c.get_attribute('type')=='checkbox' else c.get_attribute('aria-checked')} "
              f"visible={c.is_visible()} text={(c.inner_text() or '')[:40]!r}", flush=True)
    except Exception: pass
print("\n=== 先滚到底，再提交 ===", flush=True)
sb = page.query_selector("button:has-text('Scroll to bottom')")
if sb:
    try:
        sb.click(); page.wait_for_timeout(3000)
        print("  已点 Scroll to bottom", flush=True)
    except Exception as e: print("  scroll 失败", type(e).__name__, flush=True)
sub = page.query_selector("button:has-text('Submit')")
print(f"  Submit disabled={sub.is_disabled() if sub else '?'}", flush=True)
if sub:
    try:
        sub.click(); page.wait_for_timeout(15000)
        print("  已点 Submit", flush=True)
    except Exception as e: print("  submit 失败", type(e).__name__, str(e)[:90], flush=True)
print("\n页面:", page.inner_text("body")[:350].replace("\n"," | "), flush=True)
print("\n=== API ===", flush=True)
for e in cap:
    if e[0]=="REQ":
        print(f"[REQ] {e[1]}", flush=True)
        try:
            b = json.loads(e[2])
            open("/tmp/spa_confirm.json", "w").write(
                json.dumps(b, indent=1, ensure_ascii=False))
            print("完整 confirm 已存 /tmp/spa_confirm.json", flush=True)
        except Exception: print("   ", str(e[2])[:400], flush=True)
    else: print(f"[RESP {e[1]}] {e[2]}", flush=True)
page.screenshot(path="/tmp/tc.png", full_page=True)
ctx.close(); browser.close(); pw.stop(); pool.stop_all()
