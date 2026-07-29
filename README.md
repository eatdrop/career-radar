# 智职引擎 · 每日求职雷达

> 面向大学生的自动化求职工作流：每天聚合岗位、过滤噪声、基于简历证据排序、生成逐岗改写建议，并把行动沉淀到投递看板。

## 它解决的不是“不会写简历”

大学生求职最耗时的往往是重复劳动：

1. 每天在多个平台反复搜索；
2. 从大量社招、资深、外包和过期岗位中找校招机会；
3. 逐条阅读 JD，再回头确认自己的项目是否匹配；
4. 为不同岗位调整简历；
5. 投递后忘记记录，面试时找不到上下文。

智职引擎把这段流程收敛为一条可执行管线：

```text
定时/手动触发
  → 持久任务排队、幂等去重与失败重试
  → 聚合岗位池、Google Jobs、51job
  → 去重与硬门槛过滤
  → 检索相关简历证据
  → 可解释匹配排序
  → 每岗生成简历动作
  → 站内摘要 / 邮件摘要
  → 一键进入投递看板
```

核心原则是“先用确定性规则减少噪声，再把 AI 用在需要判断和表达的地方”。没有 API Key 时，离线核心流程仍可运行；外部能力不会静默伪造成功。

## 5 分钟跑通

核心业务不依赖第三方运行时框架，推荐 Python 3.11 或 3.12；Windows
会自动安装 `tzdata` 以补充时区数据。

```bash
git clone https://github.com/eatdrop/career-radar.git
cd career-radar
python3 web_server.py
```

浏览器打开：

```text
http://127.0.0.1:3000
```

首次验收：

1. 在“简历画像”粘贴一份至少 40 字的简历；
2. 回到“今日雷达”，勾选“用演示数据验收”；
3. 点击“立即跑一次”；
4. 查看匹配理由、RAG 引用的简历证据和逐岗修改建议；
5. 将一个岗位加入投递看板并更新状态。

演示岗位始终带“演示”标识，不会与真实岗位混淆。

## 配置真实工作流

复制环境变量模板：

```bash
cp .env.example .env
```

### 岗位来源

| 来源 | 配置 | 说明 |
|---|---|---|
| 本地岗位池 | 无 | 粘贴一个或多个 JD，最稳定 |
| Google Jobs | `SERPAPI_KEY` | 通过 SerpAPI 聚合公开岗位 |
| 51job | 安装 `crawler` 可选依赖 | 依赖 Chrome，站点验证可能影响采集 |
| 演示岗位 | 无 | 只用于本地验收 |

安装实时采集能力：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[crawler]'
```

请遵守目标网站服务条款、robots 规则和合理访问频率。生产环境优先使用正式招聘 API 或已授权的数据源。

### PDF 简历解析

TXT 和 DOCX 解析无需额外依赖；PDF 解析需要安装文件处理依赖：

```bash
pip install -e '.[files]'
```

### DeepSeek 定向简历

```dotenv
DEEPSEEK_API_KEY=your_new_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

然后安装 AI 可选依赖：

```bash
pip install -e '.[ai]'
```

DeepSeek 已于 2026-07-24 退役 `deepseek-chat` / `deepseek-reasoner` 旧名称，本项目默认使用 `deepseek-v4-flash`。模型变化以 [DeepSeek 官方更新日志](https://api-docs.deepseek.com/updates/)为准。

### 每日邮件摘要

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-account
SMTP_PASSWORD=your-app-password
SMTP_FROM=your-account@example.com
SMTP_USE_TLS=true
```

SMTP 凭据只从服务端环境变量读取。前端和数据库只保存收件地址，不保存邮箱密码。
邮件通过事务 Outbox 异步发送：雷达结果先与“待发送”意图原子落库，短暂故障会自动退避重试；
如果 SMTP 在提交邮件后断开、无法判断是否送达，系统会隔离为 `delivery_unknown`，不会冒险重复发送。
管理员应先核对邮件服务商日志和收件箱，再通过本地运维命令确认送达或安排一次受控重试：

```bash
jobsearch-ops --data-dir /path/to/data outbox list-unknown
jobsearch-ops --data-dir /path/to/data outbox confirm-delivered 12
jobsearch-ops --data-dir /path/to/data outbox retry 12 --delay-seconds 60
```

不要在无法确认服务商是否已接收邮件时执行 `retry`，否则仍可能产生重复邮件。

进入“求职偏好”后设置：

- 搜索关键词与城市；
- 岗位来源；
- 是否只看校招/初级岗位；
- Senior、Lead、外包、年限等排除词；
- 最低匹配分和每日推荐上限；
- 每日执行时间与邮件推送。

内置调度器按“时区 + 自然日”写入持久幂等任务，服务重启后任务不会丢失。默认 Worker
使用 claim token、可续租 lease 和指数退避执行任务。单节点可直接使用默认配置；多副本部署应让实例
共享同一任务存储，或设置 `SCHEDULER_ENABLED=false`，由 Cron / Kubernetes CronJob 调用
`POST /api/v1/radar/runs` 并为同一业务批次复用 `Idempotency-Key`。

后台执行参数：

```dotenv
BACKGROUND_WORKERS_ENABLED=true
WORKER_POLL_SECONDS=2
WORKER_LEASE_SECONDS=900
```

默认 Web 工作流依赖同进程 Worker。若设置 `BACKGROUND_WORKERS_ENABLED=false`，服务会停止
内置调度、拒绝新的异步雷达任务，并让 `/readyz` 返回未就绪；兼容同步接口仍可由可信的内部
调用方使用。不要把“关闭 Worker”当作外部 Worker 模式，当前版本未提供独立 Worker 进程。

## RAG 是怎样落地的

旧版本在 Embedding 失败时写入哈希伪向量，会产生随机相似度。该逻辑已被移除。

当前实现采用本地“证据检索增强”：

1. 分析简历时按段落切分证据块并持久化；
2. 对每个 JD 检索最相关的简历块；
3. 匹配结果展示实际引用证据；
4. 调用大模型改写时，把检索证据置于提示词前部；
5. 只有用户在本次请求中明确授权，才会把简历与 JD 发送给已配置模型；
6. 模型被明确禁止新增事实；本地证据与雷达记录会脱敏已识别的邮箱、中国大陆手机号、
   `+` 开头的国际电话、微信/QQ 号以及 LinkedIn/GitHub 公开账号。

这使每个推荐都能回答“为什么匹配”，也避免把失败的向量调用包装成 RAG 成功。

## 工程能力

- 单进程 Web + REST，核心流程无需 MCP 中转；
- REST 与 MCP 复用同一业务服务；
- 版本化 JSON API 与正确的 4xx/5xx；
- SQLite WAL、`busy_timeout`、短连接并发模型；
- schema v3 迁移、持久任务、租约心跳、崩溃接管与旧 Worker fencing；
- 事务 Outbox、稳定邮件 Message-ID、自动重试与不确定送达隔离；
- 业务完成后的最终任务对账、锁竞争无损 defer，以及 Outbox 本地人工处置命令；
- 请求体大小限制、速率限制、并发上限；
- 默认仅监听 `127.0.0.1`，严格校验 `Host`，CORS 默认关闭；
- CSP、点击劫持防护、MIME 嗅探防护等安全头；
- 请求 ID、超时、外部能力显式降级；
- 调度幂等：同一时区的同一自然日只创建一个持久任务；
- PDF/DOCX/TXT 文件上传，不接受任意服务端路径；
- 响应式、键盘可操作、减少动画模式；
- Docker 非 root 运行与健康检查。

## 项目结构

```text
achievement/
├── web/
│   ├── index.html
│   └── assets/
│       ├── app.js
│       └── styles.css
├── src/jobsearch_mcp_server/
│   ├── config.py          # 环境配置与安全默认值
│   ├── repository.py      # SQLite、简历证据库与运行记录
│   ├── services.py        # 雷达、匹配、简历、投递业务逻辑
│   ├── scheduler.py       # 每日持久任务触发器
│   ├── worker.py          # 持久任务与邮件 Outbox Worker
│   ├── ops.py             # 可信本地队列处置命令
│   ├── webapp.py          # 版本化 REST 与静态资源服务
│   ├── server.py          # allowlist MCP 适配器
│   └── crawler/           # 51job 可选采集器
├── tests/
├── web_server.py          # 源码模式兼容启动器
├── pyproject.toml
├── Dockerfile
└── .env.example
```

## API 摘要

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/health` | 健康状态与能力探测 |
| GET | `/api/v1/dashboard` | 工作台汇总 |
| GET / PATCH | `/api/v1/radar` / `/api/v1/radar/settings` | 雷达结果与偏好 |
| POST | `/api/v1/radar/runs` | 幂等创建异步雷达任务（推荐） |
| GET | `/api/v1/radar/runs/{id}` | 查询排队、运行、重试或最终结果 |
| POST | `/api/v1/radar/run` | 同步运行（兼容旧客户端） |
| GET / POST | `/api/v1/jobs` / `/api/v1/jobs/import` | 岗位池 |
| POST | `/api/v1/jobs/crawl` | 采集或显式演示岗位 |
| POST | `/api/v1/resumes/analyze` | 建立简历画像和证据库 |
| POST | `/api/v1/resumes/enhance` | 生成 JD 定向版本 |
| GET / POST | `/api/v1/applications` | 查询或新增投递 |
| PATCH / DELETE | `/api/v1/applications/{id}` | 更新或删除投递 |

所有响应使用统一格式：

```json
{
  "success": true,
  "data": {},
  "meta": {
    "request_id": "…",
    "timestamp": "…"
  }
}
```

异步创建接口返回 HTTP `202`、`Location` 和 `Retry-After`。客户端必须为一次用户动作生成
8–128 位 `Idempotency-Key`，在网络超时或重试时复用；同一个 Key 携带不同参数会返回
`409 idempotency_conflict`。任务查询不会返回内部 payload、claim token 或幂等键。

## 测试

```bash
pip install -e '.[dev]'
pytest
```

84 项测试覆盖离线核心闭环、校招过滤、RAG 证据、调度幂等、投递 CRUD、HTTP 契约、
队列并发、租约恢复、邮件 Outbox、Host/DNS-rebinding 防护、隐私脱敏与安全响应头。

CI 在 Linux/Windows 和 Python 3.11/3.12 上执行静态检查与测试。质量矩阵通过后，
发布门禁会构建最终 wheel，在全新虚拟环境安装该 wheel 并验证 `/readyz`；另一个作业
会构建但不推送 Docker 镜像，并验证容器以非 root 用户运行且就绪探针正常。

## Docker

```bash
docker build -t career-radar .
docker run --rm -p 127.0.0.1:3000:3000 \
  -v career-radar-data:/data \
  --env-file .env \
  -e APP_HOST=0.0.0.0 \
  career-radar
```

容器内部监听 `0.0.0.0`，但示例只把端口发布到宿主机 `127.0.0.1`；本机源码启动也默认只
监听回环地址。API 不自带多用户身份认证，禁止直接把端口发布到局域网或公网；确需远程访问时，
必须在前置网关启用 TLS、身份认证与访问审计，并通过 `ALLOWED_HOSTS` 显式加入反向代理使用的
主机名。`ALLOWED_ORIGINS` 只控制浏览器跨域，不等同于身份认证。

### 数据升级、备份与恢复

启动 v1.1.0 时会在同一个事务中自动把旧数据库迁移到 schema v3；检测到由更高版本创建的
数据库时会拒绝启动，避免降级程序破坏数据。升级前请先停止服务并备份数据卷：

```bash
cp /path/to/data/job_tracker.db /safe/backup/job_tracker.db
```

恢复时停止服务，用备份替换数据库后再启动；同时保留同目录中的 `-wal` / `-shm` 文件时必须
来自同一次停机快照。更稳妥的在线备份应使用 SQLite Backup API 或运维平台的卷快照。
当前 SQLite 部署目标是单节点；真正的多节点横向扩展应把队列和业务库迁移到受管数据库。
雷达历史和终态队列默认保留在本地数据库中，便于审计和故障对账；列表 API 只返回摘要，
邮件 Outbox 只保存投递所需的最小字段。请根据所在组织的数据保留制度定期备份、归档或删除
本地数据卷，当前版本不会擅自执行 TTL 清理。

## 隐私与安全

- 简历、投递和运行记录包含个人敏感信息，数据库文件不会进入构建产物；
- `.env`、本地数据库、向量旧数据、虚拟环境和调试页面均已加入忽略；
- 如果密钥曾进入代码目录、压缩包或版本历史，请在供应商控制台轮换，单纯删除文件并不能使旧密钥失效；
- 公网部署必须在反向代理层增加 TLS、身份认证、访问审计和备份策略；
- 默认 `STORE_RAW_RESUME=false`，不持久化原文，并递归脱敏已识别的结构化联系方式；系统只保留本地匹配所需的最小证据；
- 即使服务端配置了 AI Key，简历工坊也只会在用户对本次处理明确授权后调用外部模型。

漏洞请按照 [Security Policy](SECURITY.md) 私密报告，不要在公开 Issue 中提交利用细节或真实数据。
正式版本发布前必须完成 [Release checklist](.github/RELEASE_CHECKLIST.md)。Dependabot
每周检查 Python、GitHub Actions 与 Docker 基础镜像更新；CI 中的 GitHub 官方 Actions
固定到完整提交 SHA，并由 Dependabot 提交升级 PR。

## MCP

MCP 是可选适配层，不再是 Web 运行前置条件：

```bash
pip install -e '.[mcp]'
jobsearch-mcp-server
```

MCP 默认监听 `127.0.0.1:8000`，仅暴露 allowlist 工具，不提供任意工具调用或服务端路径读取。
