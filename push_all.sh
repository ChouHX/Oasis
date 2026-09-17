#!/usr/bin/env bash
#
# 提交 + 推送 + 触发构建，一步做完。
#
#   bash push_all.sh
#
# 这次的修复来自 chromium 自己的报错：
#   chrome_crashpad_handler: --database is required
# 是 crashpad 崩溃上报器启动失败把浏览器拖死的（SIGTRAP / code -5）。
# Playwright 的 launch() 默认带 --disable-breakpad，我们手工拉起时漏了。

set -uo pipefail
cd "$(dirname "$0")"

echo "=========================================="
echo " 1. 自查：改动是否都在"
echo "=========================================="
FAIL=0
check () {
    if grep -q -- "$1" oasis_gui/core/browser_registrar.py; then
        echo "  ✓ $1"
    else
        echo "  !! 缺 $1"
        FAIL=1
    fi
}
check '"--disable-breakpad"'
check '"--disable-crash-reporter"'
check '"--no-sandbox"'
check 'stderr=self._errlog'
check 'def _tail_err'
check 'raise TransientError('

if [ "$FAIL" = "1" ]; then
    echo
    echo "  改动不完整，先别推。"
    exit 1
fi

echo
echo "=========================================="
echo " 2. 提交"
echo "=========================================="
if [ -z "$(git status --porcelain)" ]; then
    echo "  （工作区干净，没有新改动）"
else
    git add -A
    git commit -q -F - <<'MSG'
chromium 启动失败的真因：crashpad —— 补上 --disable-breakpad

上一轮把 chromium 的 stderr 留下来之后，它自己给出了原因：

  chrome_crashpad_handler: --database is required
  recvmsg: Connection reset by peer (104)

是崩溃上报处理器 chrome_crashpad_handler 启动失败（拿不到 --database），
把浏览器一起拖死，表现为 SIGTRAP（code -5）。

根本原因：Playwright 的 launch() 默认会传 --disable-breakpad，而我们是用
subprocess.Popen 手工拉起的，漏了这组开关。补齐 Playwright 那套默认参数：

  --disable-breakpad / --disable-crash-reporter / --disable-features=Crashpad
  --disable-background-timer-throttling / --disable-backgrounding-occluded-windows
  --disable-renderer-backgrounding / --disable-hang-monitor
  --disable-prompt-on-repost / --metrics-recording-only
  --password-store=basic / --use-mock-keychain

保留上一轮的 --no-sandbox：容器里以非 root 运行仍然需要它。

这一轮验证的价值在于「让组件自己说话」比推断可靠：对着 code -5 猜了两轮
（沙箱、namespace 权限）都没解决，把 stderr 留下来之后一次就定位到了。
MSG
    echo "  ✓ 已提交"
fi
git log --oneline -3 | sed 's/^/    /'

echo
echo "=========================================="
echo " 3. 推送（HTTPS 带超时，失败自动换 SSH）"
echo "=========================================="
push_via () {
    git remote set-url origin "$2"
    timeout 90 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=15 \
                push origin main 2>&1 | tail -4
}

if push_via "HTTPS" "https://github.com/ChouHX/Oasis.git"; then
    echo "  ✓ HTTPS 推送成功"
elif push_via "SSH" "git@github.com:ChouHX/Oasis.git"; then
    echo "  ✓ SSH 推送成功（远端已改为 SSH）"
else
    git remote set-url origin "https://github.com/ChouHX/Oasis.git"
    echo "  ✗ 两种都失败。试试走本地代理："
    echo "      ALL_PROXY=socks5://127.0.0.1:10808 git push origin main"
    echo "    或把这条的输出发我：GIT_CURL_VERBOSE=1 git push origin main"
    exit 1
fi

echo
echo "=========================================="
echo " 4. 触发构建"
echo "=========================================="
if command -v gh >/dev/null 2>&1; then
    gh workflow run docker --repo ChouHX/Oasis 2>/dev/null \
        && echo "  ✓ 已触发" || echo "  · 未触发（push 应该已经触发）"
    sleep 8
    gh run list --repo ChouHX/Oasis --limit 3 \
        --json status,conclusion,displayTitle \
        --jq '.[] | "  \(.status) \(.conclusion // "-") \(.displayTitle)"' 2>/dev/null || true
fi

echo
echo "=========================================="
echo " 5. 服务器更新"
echo "=========================================="
cat <<'TIP'
  等 CI 跑完（约 2 分钟）：

    cd oasis_gui
    docker compose pull
    docker compose up -d
    docker compose logs -f

  chromium 这次应该能起来了。如果还不行，日志里那行
  "chromium 报错：..." 会给出新的原因，发我。

  另外：之前那些被退回队列的账号没有被消耗，会自动重新排队——
  这也是上一轮把「浏览器起不来」归为瞬时错误的价值。
TIP
