#!/usr/bin/env bash
#
# 在容器里直接跑一次 chromium，看它到底能不能起来。
#
#   bash diagnose_docker.sh
#
# 这样能把「chromium 在这个容器里能不能跑」和「我们的代码做了什么」分开——
# 前面两轮我都是对着 code -5 推断，这次让容器自己回答。

set -uo pipefail

echo "=========================================="
echo " 1. 容器在跑哪个镜像？"
echo "=========================================="
docker inspect oasis --format '  镜像    : {{.Config.Image}}' 2>/dev/null || {
    echo "  !! 找不到名为 oasis 的容器"; exit 1; }
docker inspect oasis --format '  镜像 ID : {{.Image}}' 2>/dev/null
docker inspect oasis --format '  用户    : {{.Config.User}}' 2>/dev/null
echo "  启动时间: $(docker inspect oasis --format '{{.State.StartedAt}}' 2>/dev/null)"

echo
echo "=========================================="
echo " 2. 镜像里有前端产物吗（判断新旧）"
echo "=========================================="
docker exec oasis sh -c 'ls /app/webui_dist/ 2>/dev/null | head -3' 2>/dev/null \
    || echo "  （读不到）"

echo
echo "=========================================="
echo " 3. 容器里 chromium 的启动参数里有没有 --disable-breakpad"
echo "=========================================="
echo "  （看的是宿主机上这份源码，容器用的是镜像里的副本）"
if grep -q -- '"--disable-breakpad"' oasis_gui/core/browser_registrar.py 2>/dev/null; then
    echo "  ✓ 本地源码里有"
else
    echo "  · 本地源码里没有"
fi
docker exec oasis sh -c \
    'grep -c -- "--disable-breakpad" /app/core/browser_registrar.py 2>/dev/null' \
    2>/dev/null | sed 's/^/  容器里出现次数: /' || echo "  （读不到容器内文件）"

echo
echo "=========================================="
echo " 4. 关键：在容器里手动跑一次 chromium"
echo "=========================================="
docker exec oasis sh -c '
CHROME=$(ls -d /ms-playwright/chromium-*/chrome-linux64/chrome 2>/dev/null | head -1)
if [ -z "$CHROME" ]; then CHROME=$(command -v chromium || command -v google-chrome); fi
echo "  二进制: $CHROME"
echo "  版本  : $($CHROME --version 2>&1 | head -1)"
echo
echo "  --- 4a. 最小参数 ---"
timeout 25 $CHROME --headless=new --no-sandbox --dump-dom about:blank 2>&1 | head -8
echo "  （退出码 $?）"
echo
echo "  --- 4b. 加上 --disable-breakpad ---"
timeout 25 $CHROME --headless=new --no-sandbox --disable-breakpad \
    --dump-dom about:blank 2>&1 | head -8
echo "  （退出码 $?）"
' 2>&1 | head -40

echo
echo "=========================================="
echo " 5. 容器里有没有 chrome_crashpad_handler"
echo "=========================================="
docker exec oasis sh -c '
ls -la /ms-playwright/chromium-*/chrome-linux64/chrome_crashpad_handler 2>/dev/null \
  || echo "  （找不到 handler）"
echo "  下一个目录里还有什么:"
ls /ms-playwright/chromium-*/chrome-linux64/ 2>/dev/null | head -12
' 2>&1 | head -18

cat <<'TIP'

==========================================
 怎么看结果
==========================================
 4a 就成功  → chromium 本身没问题，是我们代码里的参数组合有问题
 4a 失败但 4b 成功 → 就是 crashpad，--disable-breakpad 是对的
 两个都失败 → chromium 在这个容器里跑不起来，4a/4b 打印的报错才是真原因
              （可能是缺库、/dev/shm 太小、CPU 指令集不支持）

 把 4a / 4b 两段的完整输出发我。
TIP
