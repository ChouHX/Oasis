#!/usr/bin/env bash
#
# 只做「推送」这一件事。提交已经成功了，卡住的是 push。
#
#   bash push_retry.sh
#
# 为什么会卡：我之前的推送是在工具沙箱里跑的，那里设了代理；
# 你直接在终端跑，github.com 的 HTTPS 可能连不上——git 就一直等，看起来像死锁。
# 也可能是在等凭据输入而没显示出来。

set -uo pipefail
cd "$(dirname "$0")"

echo "=========================================="
echo " 当前状态"
echo "=========================================="
git log --oneline -1
echo "  本地分支: $(git branch --show-current)"
echo "  远端    : $(git remote get-url origin)"
AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")
echo "  待推送  : $AHEAD 个提交"
echo
echo "  本地未推送的提交："
git log --oneline origin/main..HEAD 2>/dev/null | sed 's/^/    /' || echo "    （读不到远端）"

if [ "$AHEAD" = "0" ]; then
    echo
    echo "  远端已经是最新的，不用推。直接去看 CI："
    echo "    gh run list --repo ChouHX/Oasis --limit 3"
    exit 0
fi

try_push () {
    local label="$1" url="$2"
    echo
    echo "=========================================="
    echo " 尝试：$label"
    echo "=========================================="
    git remote set-url origin "$url"
    # 15 秒连不上就放弃，别让它无限挂着
    if timeout 90 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=15 \
                  push origin main 2>&1 | tail -6; then
        return 0
    fi
    return 1
}

# 1) HTTPS（和原来一样，但带超时）
if try_push "HTTPS" "https://github.com/ChouHX/Oasis.git"; then
    echo "  ✓ HTTPS 推送成功"
else
    echo "  · HTTPS 超时或失败"

    # 2) 之前验证过 SSH 是通的（Hi ChouHX! You've successfully authenticated）
    echo
    echo "  改用 SSH 重试……"
    if try_push "SSH" "git@github.com:ChouHX/Oasis.git"; then
        echo "  ✓ SSH 推送成功（远端地址已改成 SSH，以后直接用）"
    else
        echo "  ✗ SSH 也失败"
        git remote set-url origin "https://github.com/ChouHX/Oasis.git"
        cat <<'TIP'

  两种都推不上去，多半是网络。检查一下：

    # 直连 github 通不通
    curl -sI --max-time 10 https://github.com | head -1

    # 如果本地有代理（比如 10808），让 git 走它
    git -c http.proxy=socks5://127.0.0.1:10808 push origin main
    # 或者临时全局
    ALL_PROXY=socks5://127.0.0.1:10808 git push origin main

  还不行就把这条命令的输出发我：GIT_CURL_VERBOSE=1 git push origin main
TIP
        exit 1
    fi
fi

echo
echo "=========================================="
echo " 触发构建"
echo "=========================================="
if command -v gh >/dev/null 2>&1; then
    gh workflow run docker --repo ChouHX/Oasis 2>/dev/null \
        && echo "  ✓ 已触发 workflow_dispatch" \
        || echo "  · 没触发成功（push 应该已经触发过一次）"
    sleep 8
    gh run list --repo ChouHX/Oasis --limit 3 \
        --json status,conclusion,displayTitle \
        --jq '.[] | "  \(.status) \(.conclusion // "-") \(.displayTitle)"' 2>/dev/null || true
else
    echo "  · 没装 gh，去 Actions 页面看"
fi

echo
echo "等 CI 跑完（约 2 分钟）后："
echo "  cd oasis_gui && docker compose pull && docker compose up -d && docker compose logs -f"
