# Oasis Live '27 注册控制台

批量化注册工具，带 FluentUI 桌面端和无头服务端两种形态。

详细文档在 **[`oasis_gui/README.md`](oasis_gui/README.md)**，那里记录了全部实测结论——
包括为什么不能带 captcha、为什么浏览器模式不发成功邮件、以及各个接口的坑。

## 快速开始

```bash
cd oasis_gui
pip install -r requirements.txt        # 服务端依赖
# 桌面端另需: PyQt6==6.6.1 PyQt6-Fluent-Widgets==1.11.3
python3 app.py
```

或打包成 Windows 单文件：

```bash
./build_windows_wine.sh          # Linux 下用 Wine 交叉编译
build_windows.bat                # Windows 下直接编译
```

或部署到服务器：

```bash
cd oasis_gui
mkdir -p data && cp .env.example .env    # 填管理密码、代理、Google 出口、iCloud
docker compose pull                      # 拉 CI 构建好的镜像，不在服务器上编译
docker compose up -d
```

镜像由 GitHub Actions 发布到 `ghcr.io/chouhx/oasis:latest`（公开包，无需登录）。
2 vCPU / 3GB 的机器编译 Playwright 基础镜像既慢又吃磁盘，所以 compose 默认是
**拉取**而不是 `build`。要改代码后本地构建，用覆盖文件（只有显式指定才编译）：

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## 结构

```
oasis_gui/
  app.py                  FluentUI 桌面端
  service.py              无头服务端（Web 管理界面 + /health + /stats）
  tools_spa_drive.py      CDP 驱动官方 SPA 的对照工具
  core/
    registrar.py          curl_cffi 注册核心（三步流程、场次常量）
    engine.py             线程池调度、身份唯一性、代理租约、落库
    browser_registrar.py  Playwright：共享浏览器、captcha、表单驱动
    relay.py              本地 relay：链式代理、SOCKS5 上游、Google 分流
    store.py              SQLite，去重约束与写事务安全
    mailbox.py            Graph / IMAP / Gmail / iCloud HME 取件
    proxy_pool.py         代理解析、健康计数、失败冷却
    identity.py           随机身份生成（美/德/法）
    sysinfo.py            内存与推荐线程数
    webui.py              Web 管理界面（标准库实现，带密码认证）
    config.py             配置持久化
```

## ⚠️ 不要提交数据库和凭据

`oasis.db` 存的是邮箱密码和 OAuth refresh token，`exports/`、`creds/`、`oasis_config.json`
是同一批数据的不同形态。仓库里的 `.gitignore` 已经把它们排除掉了——**在此之前先确认
`git status` 里没有它们**。一旦误提交，凭据就等于公开了，改密码也来不及。

## 另一种做法

根目录下的 `register.py`、`fingerprint.py`、`relay.py`、`API_NOTES.md` 等是最早期
的探索脚本，保留了接口逆向的过程记录；可用的实现都在 `oasis_gui/core/` 里。
