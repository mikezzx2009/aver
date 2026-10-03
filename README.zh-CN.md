# Aver

**为 AI Agent 提供持久化云端工作空间。**

[English](README.md) · [部署与恢复指南](docs/deployment.zh-CN.md) · [参与贡献](CONTRIBUTING.md)

通过网页与服务器上的 Agent 对话、提交任务。关闭网页后，任务仍在服务器执行；再次登录可查看对话、过程和保存的文件。当前执行运行时使用 [DeepSeek Harness SDK](https://pypi.org/project/deepseek-harness-sdk/)，界面为中文，面向小范围可信用户的自托管测试。

## 功能

- 每个用户拥有常驻的 gVisor Linux 容器、持久化 home 与工作空间。
- SQLite 持久队列，全站一次执行一个任务，其余任务排队。
- 对话记录、过程查看、停止和明确的重试操作。
- 文件按对话整理，支持上传、下载与分页浏览。
- 静态网站按项目提供固定短网址，原子更新内容并保留历史快照。
- 服务器 CPU、内存、磁盘和负载状态，以及定时本机备份。

默认不设置排队数量和任务时长上限，实际仍受磁盘、内存、模型服务和费用限制。持续运行与持久化保存不等于绝对不中断或零丢失。

## 架构

| 组件 | 职责 |
| --- | --- |
| `static/` | 原生 HTML/CSS/JavaScript 对话界面 |
| `server.py` | 认证、用户权限、文件接口和模型代理 |
| `store.py` | SQLite WAL、任务租约和对话文件目录 |
| `worker.py` | 后台队列执行与用户容器管理 |
| `sandbox/` | Ubuntu 镜像、DeepSeek 运行时和常驻 Agent daemon |
| `publisher.py` | 校验公开文件、保留版本并维护固定访问地址 |
| `backup.py`、`deploy/` | 备份、systemd、网络策略与 Nginx 模板 |

浏览器只负责提交和查看任务，后台 worker 决定任务生命周期。Docker 配合 gVisor、非 root 用户、只读容器根目录和持久化挂载隔离执行。对话目录用于分类；同一用户的各个对话共用工具和进程，不是独立安全沙箱。

真实模型 API Key 仅保存在宿主机配置，不挂载进用户容器。Agent 使用绑定当前任务的临时凭据访问模型代理，由宿主机转发请求。

## 本地体验界面

在 Linux 或 macOS 上使用 Python 3.12。网页服务仅依赖 Python 标准库，本地预览无需模型 Key 或安装依赖。

```bash
git clone https://github.com/mikezzx2009/aver.git
cd aver
python3 server.py
```

另开终端，进入同一项目目录，读取自动生成的初始化口令：

```bash
python3 -c 'from common import config; print(config()["bootstrap_token"])'
```

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，输入口令并创建首个管理员账户。预览数据保存在私有的 `var/` 目录，重启服务仍会保留。`AGENT_CLOUD_DATA`、`AGENT_CLOUD_CONFIG` 可覆盖数据和配置路径。

此方式可体验账户、对话、文件和排队任务，不会启动容器 worker，也不会执行 Agent。完整执行需按下面的 Linux 服务器部署流程安装。

## 小规模服务器部署

安装器支持 **Ubuntu 24.04 LTS x86_64**。2 vCPU / 2 GiB / 40 GiB 可作为小范围测试起点；增加用户前需测量资源，每个空闲常驻容器也有开销。

1. 先为已有服务器做快照，克隆项目，运行 `sudo bash deploy/install.sh`。安装器配置 Docker、官方 gVisor 包、Nginx、systemd，构建并检查容器镜像。
2. 在 `/etc/agent-cloud/config.json` 设置登录网站的 `domain`，配置 DNS 与 HTTPS，替换 Nginx 模板和续期 hook 的示例域名。安装器不会自动启用公网模板。
3. 使用初始化口令创建管理员，在网站“设置”中保存 DeepSeek API Key，并选择是否开放注册。私有测试应保持注册关闭。
4. 如需发布生成的静态网站，将 `site_base_url` 设置为实际公网源，例如 `http://你的公网IP`。单独配置、启用静态 Nginx 模板，让生成页面使用与 Aver 登录网站不同的源。

**默认未配置网站发布地址。** 缺少 `site_base_url` 时，发布请求会明确失败，不会生成猜测的网址。`203.0.113.10` 和 `aver.example.com` 是文档占位值，需要自行替换。

将下面字段合并进已有私有配置，替换两个示例值，并保留已有设置和密钥：

```json
{
  "domain": "aver.example.com",
  "site_base_url": "http://203.0.113.10"
}
```

代码、数据和配置分别位于 `/opt/agent-cloud`、`/srv/agent-cloud`、`/etc/agent-cloud/config.json`。服务名与路径保留旧名称以兼容部署。网页内部端口是 loopback 8080，模型代理是私有 bridge 上的 8090；公网只应经过 Nginx，不要开放这些内部端口或 Docker API。

具体证书、静态站点、资源限制和维护命令见[部署指南](docs/deployment.zh-CN.md)。重装或重启服务可能中断运行任务，应作为维护操作安排。

## 数据与恢复

账户、对话、队列和事件提交到 SQLite。用户 home、SDK 会话和对话文件保存在宿主机目录，不依赖容器临时根目录。正常任务完成后，用户 daemon 和后台进程会继续运行。

```bash
sudo systemctl start agent-cloud-backup.service
sudo journalctl -u agent-cloud-backup.service -n 30 --no-pager
```

备份包含 SQLite 在线副本、用户文件、公开版本与固定别名、私有发布记录、配置及源码。归档包含密钥，应按私密文件保存。各数据库分别是一致快照，正在写入的文件不是同一时刻的原子快照；备份不包含内存、进程、容器镜像或 TLS 证书。

本机备份和运行数据位于同一服务器，删除服务器或磁盘故障可能同时损失两者。正式依赖服务前，应配置异地副本并演练恢复。请按[恢复流程](docs/deployment.zh-CN.md#恢复数据库和文件)操作空暂存目录，不要直接覆盖运行中的数据。

## 当前限制

- 单机、单任务执行，无高可用或自动异地备份。
- 取消、worker 恢复、OOM 或主机重启可能结束进程；已保存文件通常可跨重启保留，内存和未保存数据不能恢复。中断任务不会自动重做。
- 镜像包含 shell、Python、Git、Node/npm 和编译工具，没有 Chromium、GUI 桌面或预授权外部连接器。
- 发布器只服务静态前端网站；动态后台需要额外的进程管理和反向代理。
- 每个容器默认 1 CPU / 768 MiB，日志、快照和备份不会静默清理，需要监控容量。
- 宿主控制服务以 root 管理 Docker；当前项目面向小范围测试，不承诺已达到正式多租户产品的可靠性与隔离保障。

## 验证

CI 使用 Python 3.12、Node 24 和 Nginx。未安装 Nginx 时，真实 Nginx 发布测试会跳过。

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
node tests/test_frontend_history.js
python3 tests/smoke_web.py --app-dir .
```

冒烟测试使用临时数据和 loopback 端口，验证认证、文件、队列、备份及恢复，不需要模型 Key。CI 在临时 Ubuntu runner 上以 root 运行 Python 测试，覆盖真实容器 UID 权限。Docker/gVisor 与付费模型执行需在部署环境另行验收。

欢迎参考 [CONTRIBUTING.md](CONTRIBUTING.md) 参与。安全问题请按 [SECURITY.md](SECURITY.md) 报告。项目采用 [MIT 许可证](LICENSE)。
