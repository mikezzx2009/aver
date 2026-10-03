# Aver 部署与恢复指南

本文面向 Ubuntu 24.04 LTS x86_64 的小规模部署。示例中的 `aver.example.com`、`203.0.113.10` 都是文档占位值，必须换成你自己的域名与公网地址。安装脚本保留 `agent-cloud` 服务名及磁盘路径，避免破坏已有安装。

返回 [项目介绍](../README.md) · [中文介绍](../README.zh-CN.md)

网页提交任务，服务器上的 worker 在后台执行。关闭网页或断开浏览器连接不会取消任务；重新登录后可以查看已保存的对话、任务过程和工作空间文件。

文件按对话管理。当前对话的上传文件和 Agent 成果放在 `/workspace/conversations/<对话ID>/`，文件按钮只列出这个目录，并在上传、下载、分页请求中校验对话归属。切换对话会关闭旧文件面板，避免异步结果或上传落入错误的对话。Agent 每次任务都收到当前对话的绝对目录，持久化容器和用户级工具继续共用；这是同一用户内的文件分类，不是每个对话独立的安全沙箱。

历史文件分类脚本为 `deploy/migrate-conversation-files.py`。只有一个已有对话的用户可自动将旧工作空间内容复制到该对话；已有同名文件内容不一致时停止，不覆盖。多对话用户的未分类旧文件需由维护者确定归属。原文件及已公开的站点快照保留，分类后的文件随现有工作空间一起备份。

模型代理统一提供每次请求 49,152 个输出 token 的上限，包含思考与正文；已在线的 SDK 进程也使用此预算。达到上限会保留已执行的结果并显示具体原因，可在原对话发送新消息继续；不会自动重新执行有副作用的原始任务。

静态网站使用固定短网址，例如 `http://203.0.113.10/ruler/`；新项目自动分配 6 位名称。Agent 将页面和资源放入当前对话的 `public/<项目>/`，使用相对资源路径，并在工作空间根目录为当前任务写入 `.ever-publish/<任务ID>.json`：`{"version":1,"task_id":"当前任务ID","directory":"conversations/当前对话ID/public/my-site","title":"网站名称"}`。同一用户、对话和项目目录自动沿用原网址；移动项目目录时可在发布请求中指定已有 `site_id`，发布器校验其所有权与对话。Agent 每次任务会收到当前对话已有的网站名称、目录和固定网址。

每次发布仍保留独立版本，固定短网址原子切换到最新版本，重复处理旧任务不会回退网站。历史 `public/<项目>/` 请求及 `/sites/<32位版本ID>/` 访问保持兼容。对话中的旧长链接在显示时转换为对应短网址，数据库原始记录保留。任务回复提供真实的固定访问链接，容器内 localhost 或私网地址不能作为用户访问地址。

启用后，公网 IP 的 Nginx 站点只读取 `published/` 中的快照和宿主管理的 `published-short/` 别名，其他路径返回 404；Aver 的 HTTPS 登录站点继续使用 `aver.example.com`。短网址使用 root 所有的相对符号链接指向公开快照，用户容器无法修改这两处目录。发布不开放整个工作空间，隐藏文件、用户文件链接和特殊文件被过滤或拒绝。短网址禁用旧内容缓存，刷新后读取当前版本。当前发布器用于纯前端网站，动态后端服务需要另行配置启动和代理。公开成品、固定别名及私有发布记录纳入备份。

任务状态、过程、停止和重试操作显示在对应发送消息内。登录后的顶部状态条每 5 秒刷新服务器 CPU、内存、数据盘占用及最近 1 分钟平均负载；百分比是整台服务器的统计，负载为数值而非百分比。后台采样每 2 秒进行，浏览器隐藏时停止请求，恢复显示后立即刷新。

任务过程、文件和设置面板的标题与关闭按钮固定在顶部，面板内容单独滚动。过程历史加载和实时更新保留内容区的阅读位置。

每位用户的容器内运行一个常驻 agent daemon，SDK 运行时、PTY 和正常任务启动的后台进程由它保持。正常任务结束后容器与 daemon 继续运行，后续任务可继续使用该用户环境；后台进程仍受该容器的资源限制。取消任务、运行中的 worker 故障恢复或执行通道异常时，会重启该用户容器以确认旧进程全部停止，再将任务标记为取消/中断；主机重启后也需要重新启动进程。持久目录中的文件和会话记录继续保留。

当前面向阿里云 Ubuntu 24.04 LTS x86_64、2 vCPU / 2 GiB / 40 GiB 的小范围测试。每位用户使用独立的 gVisor Linux 容器和持久目录，全站一次执行 1 个任务，其余任务保存在 SQLite 队列中。没有人为设置“只能排几个任务”的数量上限，但队列、历史和文件仍受磁盘容量限制。默认没有任务时长上限。不要据此承诺这台机器能同时运行多个浏览器或多台完整虚拟机。

## 已实现的持久化范围

| 内容 | 服务器路径 | 行为 |
| --- | --- | --- |
| 账户、对话、任务队列、过程事件 | `/srv/agent-cloud/data/agent-cloud.sqlite3` | SQLite WAL，业务事务同步提交 |
| 用户工作空间 `/workspace` | `/srv/agent-cloud/data/workspaces/<用户 ID>` | 宿主机持久目录，容器重建仍可使用 |
| 对话文件 `/workspace/conversations/<对话 ID>` | 工作空间内的独立对话目录 | 当前对话上传和成果分类保存 |
| 用户 home、SDK 会话、用户级安装 | `/srv/agent-cloud/sandboxes/<用户 ID>/home` | 映射到 `/home/agent`，跨容器和服务器重启保留 |
| 任务过程文件 | `/srv/agent-cloud/runs/<任务 ID>.jsonl` | 每条过程记录落盘 |
| 管理员配置、模型 API Key | `/etc/agent-cloud/config.json` | root 所有，权限 `0600`；不会挂载到用户容器 |
| 临时模型代理凭据 | `/srv/agent-cloud/data/proxy.sqlite3` | 每任务临时凭据，worker 恢复时撤销 |
| 本机备份 | `/srv/agent-cloud/backups` | 定时生成，默认不自动清理 |

容器的系统根目录只读，`/tmp` 和 `/dev/shm` 是临时存储。重要文件应放进 `/workspace` 或 `/home/agent`。Python 虚拟环境、用户级工具和 npm 依赖应安装在这些持久目录中；不能以 root 安装系统包。基础镜像已包含 shell、Python、Git、Node/npm 和编译工具。当前镜像没有 Chromium，也没有预先配置外部 connector 的账户授权。

服务器重启、OOM、进程故障或维护会终止正在执行的进程。worker 会先确认旧执行停止，再把任务标成 `interrupted`；它不会自动重做可能产生外部副作用的任务。文件、对话和已提交记录保留，RAM、打开的 shell 进程和未保存数据不能保证恢复。持续在线和数据安全还依赖服务器、磁盘、模型服务以及备份恢复操作；这里不承诺永不中断或绝对零丢失。

## 部署

1. 使用官方 Ubuntu 24.04 LTS x86_64 无桌面镜像。部署前给已有系统盘做快照，确认至少有足够空间安装 Docker、构建镜像和保留备份。
2. 将完整项目上传并解压到服务器。不要上传本机 `.venv`、`var`、真实配置或 API Key。
3. 在项目目录运行：

```bash
sudo bash deploy/install.sh
```

安装脚本安装 Ubuntu 的 Docker、Buildx、Nginx、Python venv 和 Certbot；从 gVisor 官方签名 apt 仓库安装完整 `runsc` 包，配置 `runsc-systrap` 并做实际容器启动检查。下载、签名或运行检查失败会停止安装，不能改成普通 `runc` 来绕过。脚本需要访问 Ubuntu 软件源、gVisor、Docker Hub 和 PyPI；国内服务器应先核实这些站点可达。当前官方包包含运行所需的 sidecar 二进制，不应只下载单个 `runsc` 文件。参见 [gVisor 官方安装说明](https://gvisor.dev/docs/user_guide/install/)。

代码安装到 `/opt/agent-cloud`，运行数据和密钥配置分开保存。重复安装会保留数据与已有 `config.json`，但会重启服务，并可能中断当前任务。应安排维护时间；不要在运行中删除数据目录、Docker 网络或用户容器。

先检查私有服务：

```bash
sudo systemctl status agent-cloud-network agent-cloud agent-cloud-worker --no-pager
curl --fail http://127.0.0.1:8080/health
sudo journalctl -u agent-cloud-worker -n 50 --no-pager
sudo systemctl list-timers agent-cloud-backup.timer --all
```

安装脚本不启用网站的公网 Nginx 配置。网页服务只监听 `127.0.0.1:8080`；模型代理只监听专用 bridge 的 `172.29.0.1:8090`。初次在未发布状态下测试，可以通过 SSH 将本地端口转发到服务器的 `127.0.0.1:8080`。不要向公网开放 `8080`、`8090` 或 Docker API。

## 配置网站地址

默认 `domain`、`site_base_url` 为空，没有可直接访问的演示站点。请在 `/etc/agent-cloud/config.json` 合并下面字段，替换示例值，保留安装器生成的初始化口令和已有密钥。配置文件保持 root 所有、权限 `0600`：

```json
{
  "domain": "aver.example.com",
  "site_base_url": "http://203.0.113.10"
}
```

`domain` 是 Aver 登录网站的主机名，不带协议或路径；`site_base_url` 是 Agent 成品网站的公开源，需要 HTTP(S) 协议，不带路径、查询参数、账号或密码。它可以是公网 IP 或单独的站点子域。两者应使用不同源，因为发布的页面会执行 Agent 生成的 JavaScript。未设置 `site_base_url` 时仍能对话和保存文件，发布请求会明确报错。

## HTTPS 与首次管理员

安装不会自动申请证书或启用公开网站。先为自己的域名配置 DNS，并在 `/etc/agent-cloud/config.json` 设置 `domain`。将正式 Nginx 模板、ACME 示例和证书续期 hook 中的 `aver.example.com` 替换为该域名。已有有效证书时可以直接执行“启用正式模板”步骤；新安装按下面的 webroot 流程申请。

将 `aver.example.com` 的 A 记录指向服务器公网 IP。只有服务器确实支持 IPv6 时才设置 AAAA。阿里云防火墙放行 TCP `80/443`，SSH 继续保留；`80` 只用于 ACME 和跳转 HTTPS。若使用中国内地地域的轻量服务器，应先完成域名备案/接入；非内地地域不适用该要求。参见 [阿里云使用须知](https://help.aliyun.com/zh/simple-application-server/product-overview/usage-notes)。

首次申请时，先创建只提供 ACME 验证的临时 Nginx vhost，再申请证书。下面命令使用占位域名和 `/var/www/html` webroot；请先替换域名，且不要覆盖服务器上其他网站的配置。

```bash
sudo tee /etc/nginx/sites-available/agent-cloud-acme >/dev/null <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name aver.example.com;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        try_files $uri =404;
    }
    location / { return 404; }
}
NGINX
sudo ln -sfn /etc/nginx/sites-available/agent-cloud-acme /etc/nginx/sites-enabled/agent-cloud-acme
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/html -d aver.example.com
```

证书申请成功后启用正式模板：

```bash
sudo rm -f /etc/nginx/sites-enabled/agent-cloud-acme
sudo ln -sfn /etc/nginx/sites-available/agent-cloud /etc/nginx/sites-enabled/agent-cloud
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run
```

安装脚本已设置 Certbot 的证书更新 hook：更新本域名证书后先校验 Nginx，再 reload，让新证书生效。正式模板中的证书路径必须存在才能通过 `nginx -t`。测试期若尚未完成证书或域名发布，保持模板未启用。参见 [Certbot webroot 和 deploy hook 说明](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)。

首次服务启动生成一次性管理员设置口令。仅在自己的服务器管理终端读取：

```bash
sudo python3 -c 'import json; print(json.load(open("/etc/agent-cloud/config.json"))["bootstrap_token"])'
```

在 HTTPS 网页填写该口令并创建首个账户。它成为管理员，口令随后失效。管理员在“设置”中保存 DeepSeek API Key，并决定是否开放测试用户注册。不要把口令、配置文件或包含密钥的备份发到公开渠道。首次配置完成后可以提交一个创建文件的轻任务，关闭网页再登录，核实执行与文件记录仍然存在。

## 启用静态成品网站

先设置上面的 `site_base_url`。将 `/etc/nginx/sites-available/agent-cloud-sites` 中的 `server_name 203.0.113.10` 换成对应公网 IP 或站点主机名。当前模板使用 HTTP 80；如果配置 HTTPS 子域，还需为该子域提供证书与 HTTPS vhost。

发布器只公开宿主机管理的快照和固定别名目录，不能将 `/workspace` 或 `/srv/agent-cloud` 整体作为 Nginx root。检查 `data/`、`sandboxes/`、`backups/` 等私有目录没有 others 读取权限后，再允许 Nginx 穿越数据根目录，并启用模板：

```bash
sudo install -d -o root -g root -m 0755 /srv/agent-cloud/published /srv/agent-cloud/published-short
sudo chmod 0711 /srv/agent-cloud
sudo ln -sfn /etc/nginx/sites-available/agent-cloud-sites /etc/nginx/sites-enabled/agent-cloud-sites
sudo nginx -t
sudo systemctl reload nginx
```

按所选源开放 Nginx 80/443，并确认它与 Aver 的 HTTPS vhost 没有冲突。首次发布后，实际打开回复中的短网址；测试资源相对路径可加载，`/api/status`、隐藏文件和私有配置均不能从该源访问。固定别名及公开快照归 root 管理，用户容器无权修改。保留旧快照以维持版本访问，容量不足时需制定备份和保留策略。

## 隔离和资源限制

用户容器使用固定 `runsc-systrap`、非 root UID/GID `10001`、只读根目录、移除全部 Linux capabilities、`no-new-privileges`，只挂载本用户的 home/workspace，不挂载 Docker socket、宿主配置或其他用户目录。控制服务当前以 root 运行以管理 Docker，网站权限和宿主访问必须由 API 与 worker 严格控制。

每个容器限制 CPU `1`、内存 `768 MiB`（含 swap 上限）、进程 `256`、临时目录 `96 MiB`、共享内存 `64 MiB`。容器保留 `unless-stopped` 重启策略；后台执行仍由宿主 worker 统一调度。2 GiB 机器上，常驻容器的基础开销也会累加，用户数量增加前需要测量并扩容；暂不启用 browser。

宿主网页服务设置内存软限额 `384 MiB`、硬上限 `512 MiB`，worker 为 `256 MiB / 384 MiB`，约束 JSON、代理缓存和过程转发占用。达到软限额会触发内存回收，超过硬上限可能被系统终止并重启，对应请求或运行任务会受影响。这些限额用于降低单个服务耗尽宿主内存的风险；宿主系统、Docker/gVisor 和所有常驻容器仍共同使用约 2 GiB 的物理内存，扩展用户前需观察实际峰值。

专用 `agent-net` 使用 `172.29.0.0/24`、bridge `agent-br` 和 `enable_icc=false`。`network.sh` 通过 `DOCKER-USER` 与宿主 `INPUT` 规则限制该 bridge：阻断租户互访、私网、link-local、阿里云元数据地址 `100.100.100.200` 和 IPv6；只允许专用网关模型代理 `8090`、公网 DNS 和 HTTPS。HTTP `80` 以及任意公网端口默认也不允许。规则只匹配 `agent-br`，不修改 SSH 的入站策略；systemd 在 Docker 启动/重启后恢复规则。Docker 的 bridge 默认允许同网容器互联，因此这里需要额外关闭并过滤，见 [Docker bridge 文档](https://docs.docker.com/engine/network/drivers/bridge/) 和 [DOCKER-USER 规则说明](https://docs.docker.com/engine/network/firewall-iptables/)。

Python venv 仅负责依赖管理，隔离边界是 gVisor、容器挂载、网络策略和业务租户授权。可出网并不等于已授权发送邮件、调用连接器或操作外部账户；这些能力还需要分别接入和授权。

## 备份

定时器在北京时间每天约 `03:15` 运行，并在开机后约 15 分钟触发；时间可能有少量随机延迟。也可以手动备份：

```bash
sudo systemctl start agent-cloud-backup.service
sudo journalctl -u agent-cloud-backup.service -n 30 --no-pager
sudo ls -lh /srv/agent-cloud/backups
```

归档包含两份 SQLite 在线备份、本用户 home/workspaces、任务过程文件、公开网站版本、固定网址的相对链接、私有发布记录、私有配置以及应用源代码。数据库各自是一致快照；用户文件是在运行中复制的，两份数据库和所有文件不构成同一瞬间的原子快照。归档不包含内存、运行进程、Docker 镜像、操作系统或 TLS 证书。

需要更严格的文件一致性时，安排停机窗口：停止 worker 和网页写入，确认所有用户容器已停止，再运行备份。不要把强制停止容器描述成任务可以无缝继续。基础镜像元数据在 `sandboxes/base-image.json`；重要恢复演练建议另外用 `docker image save` 保存实际使用的镜像，并把镜像归档与备份一起放到异地。

备份默认全部保留，应用不会静默清理历史。磁盘剩余不足 2 GiB 时暂停新任务执行，备份也会明确失败；管理员需监控容量、增加磁盘或将确认保存的旧备份移出服务器。每个备份包含模型密钥和用户数据，应按私密文件管理、加密传输和限制访问。

这些备份都在同一服务器磁盘上。服务器被删除、系统盘被重置、整盘故障或账号失陷时，本机备份可能同时丢失。当前没有自动异地备份或高可用；正式使用前应配置独立存储的异地副本、云盘快照和定期恢复演练，并明确可接受的数据回退时间。

## 恢复数据库和文件

恢复会回到所选备份保存的状态。应先保留故障机器现有数据的独立副本，再操作；不要直接覆盖正在运行的数据目录。新服务器必须仍是受支持的 Ubuntu x86_64 环境。

1. 停止 `agent-cloud-backup.timer`、`agent-cloud-worker` 和 `agent-cloud`，确认没有执行中的任务。停止并移除属于本应用的 `agent-pc-<32 位用户 ID>` 容器，保留它们的 bind mount 目录；这一步防止旧容器继续指向恢复前的目录。不要移除其他 Docker 容器。
2. 将服务生成的备份解压到新的、空的暂存目录。先检查 `databases/manifest.json` 和归档内容，不要向现有数据目录直接解压，也不要恢复来源不明的归档。
3. 安装同一应用版本及 gVisor。可用归档的 `source/` 部署，并载入保存的原始 sandbox 镜像；重新构建同名标签不保证得到原来完全相同的基础系统和依赖。若安装过程启动了服务，继续恢复前再次停止它们。
4. 创建新的 `/srv/agent-cloud/data`，把 `databases/agent-cloud.sqlite3` 放到 `/srv/agent-cloud/data/agent-cloud.sqlite3`。模型代理凭据数据库可从归档恢复到同目录，worker 启动会撤销旧凭据。不要混入故障目录中的旧 `-wal`/`-shm` 文件。
5. 将归档 `sandboxes/`、`runs/`、`data/workspaces/`、`published/`、`published-short/`、`publication-records/` 恢复到 `/srv/agent-cloud/` 的对应路径。解压时保留 `published-short/` 的相对符号链接，不要将其展开成目录；链接和公开快照均归 root 所有，公开目录权限为 `0755`，私有发布记录目录为 `0700`。数据根目录可用 `0711` 供 Nginx 穿越，用户数据与其他私有子目录保持 `0700`。保留 home/workspace 的 UID/GID `10001:10001` 和私有目录权限，数据库归 root 所有、权限 `0600`。将 `private/config.json` 恢复到 `/etc/agent-cloud/config.json`，root 所有、权限 `0600`。恢复公网 IP 的 Nginx 站点后执行 `nginx -t`，再加载配置。TLS 证书应独立恢复或重新申请。
6. 对恢复的 SQLite 执行 `PRAGMA integrity_check`，应返回 `ok`。先只启动 network 和网页服务，管理员核对账户、对话、队列和文件；确认记录匹配且域名配置正确后，再启动 worker 和备份定时器。用户容器会在后续任务需要时按持久目录重建。
7. 运行中断任务需检查已经产生的文件及外部操作再决定是否重新提交。旧备份之后已经完成的外部操作不会因恢复数据库而撤销；避免重复付款、重复发送或重复写入外部系统。

恢复验收至少包括：一个用户只能看见自己的文件/对话；保存的文件内容一致；旧会话可继续使用；关闭网页后任务继续；服务重启后队列和历史保留、运行中任务标为中断；实际执行仍使用 gVisor；用户容器不能访问宿主其他端口、其他租户和元数据；本机备份可在另一台隔离测试机恢复。

## 常用维护命令

```bash
sudo journalctl -u agent-cloud -u agent-cloud-worker -f
sudo systemctl restart agent-cloud-network
sudo systemctl restart agent-cloud agent-cloud-worker
sudo docker stats --no-stream
df -h /srv/agent-cloud
```

重启服务属于维护动作，可能中断当前任务。不要用 `docker system prune --volumes`、自动清理脚本或系统盘重装来释放未确认备份的数据。上线后的容量、备份和 HTTPS 续期状态需要持续检查。
