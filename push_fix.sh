#!/usr/bin/env bash
#
# 手动提交并推送 chromium 启动修复，然后触发镜像构建。
#
# 背景：沙箱里的子进程工具挂了（spawn systemd-run ENOENT），我这边推不了，
# 但改动已经在工作区里写好了。这个脚本把该做的事做完。
#
#   bash push_fix.sh
#
# 做完之后服务器上：
#   cd oasis_gui && docker compose pull && docker compose up -d

set -euo pipefail
cd "$(dirname "$0")"

echo "=========================================="
echo " 1. 提交前的自查"
echo "=========================================="

# 改动必须真的在文件里，别推一个空的 commit 上去
if ! grep -q -- '"--no-sandbox"' oasis_gui/core/browser_registrar.py; then
    echo "!! oasis_gui/core/browser_registrar.py 里没有 --no-sandbox"
    echo "   说明改动丢了，先别推，告诉我。"
    exit 1
fi
echo "  ✓ --no-sandbox 在 browser_registrar.py 里"

for pat in '"--disable-setuid-sandbox"' 'stderr=self._errlog' 'def _tail_err'; do
    if grep -q -- "$pat" oasis_gui/core/browser_registrar.py; then
        echo "  ✓ $pat"
    else
        echo "  !! 没找到 $pat —— 改动可能不完整"
        exit 1
    fi
done

# 启动失败必须是"瞬时错误"，否则坏掉的 chromium 会把整队刷成 failed
if grep -q "raise TransientError(" oasis_gui/core/browser_registrar.py; then
    echo "  ✓ 启动失败按瞬时错误处理"
else
    echo "  !! 启动失败没有归类为 TransientError"
    exit 1
fi

echo
echo "=========================================="
echo " 2. 将要提交的改动"
echo "=========================================="
git status --short || true

if [ -z "$(git status --porcelain)" ]; then
    echo "  （工作区干净，没有可提交的东西）"
else
    git add -A
    git commit -q -F - <<'MSG'
容器里 chromium 加 --no-sandbox，并让启动失败不再烧账号

症状：每个账号瞬间失败，日志是 shared chromium exited immediately (code -5)。

-5 是被信号 5（SIGTRAP）杀掉，那是 Chromium 内部 CHECK 失败的表现，说明它在启动
阶段就崩了。原因是共享浏览器由我们自己 subprocess.Popen 拉起，没有 Playwright
会加的那组沙箱参数；容器里进程以非 root（uid 10001）运行，没有沙箱需要的
namespace，chromium 直接死。Playwright 的 launch() 会处理这件事，手工拉起就得
自己加：--no-sandbox --disable-setuid-sandbox。

补一个背景：之前成功的运行都是 Windows 上的系统 Chrome（日志里 system Chrome
(C:\Program Files\Google\Chrome\...\chrome.exe)），所以容器里的共享浏览器路径
很可能一直没被真正跑过。

同时改两处，让这类问题以后不用猜：
- chromium 的 stderr 原来丢进 DEVNULL，等于把唯一的解释扔掉。现在留下来，
  启动失败时把最后几行附在错误消息里
- 浏览器起不来归为瞬时错误（TransientError），账号退回队列而不是标记失败。
  否则一个坏掉的 chromium 会在几秒内把整个队列烧光，而那些账号本身毫无问题
MSG
    echo "  ✓ 已提交"
fi

echo
echo "=========================================="
echo " 3. 推送"
echo "=========================================="
git push origin main
echo "  ✓ 已推送"
git log --oneline -1

echo
echo "=========================================="
echo " 4. 触发镜像构建"
echo "=========================================="
# push 本身就会触发（workflow 里有 oasis_gui/core/** 的路径过滤），
# 但显式再触发一次也不会有害，而且能确认 gh 是通的。
if command -v gh >/dev/null 2>&1; then
    if gh workflow run docker --repo ChouHX/Oasis 2>/dev/null; then
        echo "  ✓ 已触发 workflow_dispatch"
    else
        echo "  · 没触发成功（push 应该已经触发过一次，去 Actions 页面确认即可）"
    fi
    sleep 5
    gh run list --repo ChouHX/Oasis --limit 3 \
        --json status,conclusion,displayTitle \
        --jq '.[] | "  \(.status) \(.conclusion // "-") \(.displayTitle)"' 2>/dev/null || true
else
    echo "  · 没装 gh，去 GitHub 的 Actions 页面看构建"
fi

echo
echo "=========================================="
echo " 5. 服务器更新"
echo "=========================================="
cat <<'TIP'
  等 CI 跑完（约 2 分钟），然后：

    cd oasis_gui
    docker compose pull
    docker compose up -d
    docker compose logs -f

  起来之后如果还是 chromium 起不来，现在日志里会多一行它自己的报错
  （chromium 报错：...），把那行发我。
TIP
