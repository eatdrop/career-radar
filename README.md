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

进入“求职偏好”后设置：

- 搜索关键词与城市；
- 岗位来源；
- 是否只看校招/初级岗位；
- Senior、Lead、外包、年限等排除词；
- 最低匹配分和每日推荐上限；
- 每日执行时间与邮件推送。

内置调度器适用于单机、单进程部署。多副本生产环境应设置 `SCHEDULER_ENABLED=false`，改由 Cron、Kubernetes CronJob 或工作流平台调用 `POST /api/v1/radar/run`，避免重复执行。

## RAG 是怎样落地的

旧版本在 Embedding 失败时写入哈希伪向量，会产生随机相似度。该逻辑已被移除。

当前实现采用本地“证据检索增强”：

1. 分析简历时按段落切分证据块并持久化；
2. 对每个 JD 检索最相关的简历块；
3. 匹配结果展示实际引用证据；
4. 调用大模型改写时，把检索证据置于提示词前部；
5. 只有用户在本次请求中明确授权，才会把简历与 JD 发送给已配置模型；
6. 模型被明确禁止新增事实，联系信息不会进入岗位证据与雷达运行记录。

这使每个推荐都能回答“为什么匹配”，也避免把失败的向量调用包装成 RAG 成功。

## 工程能力

- 单进程 Web + REST，核心流程无需 MCP 中转；
- REST 与 MCP 复用同一业务服务；
- 版本化 JSON API 与正确的 4xx/5xx；
- SQLite WAL、`busy_timeout`、短连接并发模型；
- 请求体大小限制、速率限制、并发上限；
- 默认仅监听 `127.0.0.1`，CORS 默认关闭；
- CSP、点击劫持防护、MIME 嗅探防护等安全头；
- 请求 ID、超时、外部能力显式降级；
- 调度幂等：同一自然日最多自动尝试一次；
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
│   ├── scheduler.py       # 单进程每日调度器
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
| POST | `/api/v1/radar/run` | 手动运行完整工作流 |
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

## 测试

```bash
pip install -e '.[dev]'
pytest
```

测试覆盖离线核心闭环、校招过滤、RAG 证据、调度幂等、投递 CRUD、HTTP 契约与安全响应头。

## Docker

```bash
docker build -t career-radar .
docker run --rm -p 3000:3000 \
  -v career-radar-data:/data \
  --env-file .env \
  -e APP_HOST=0.0.0.0 \
  career-radar
```

示例显式覆盖 `.env` 中的本机安全默认值，使容器监听 `0.0.0.0` 以支持端口映射；
本机源码启动仍默认只监听 `127.0.0.1`。

## 隐私与安全

- 简历、投递和运行记录包含个人敏感信息，数据库文件不会进入构建产物；
- `.env`、本地数据库、向量旧数据、虚拟环境和调试页面均已加入忽略；
- 如果密钥曾进入代码目录、压缩包或版本历史，请在供应商控制台轮换，单纯删除文件并不能使旧密钥失效；
- 公网部署必须在反向代理层增加 TLS、身份认证、访问审计和备份策略；
- 默认 `STORE_RAW_RESUME=false`，不持久化原文和结构化联系方式；系统只保留脱敏后的最小证据，跨请求匹配与本地定向改写仍可用；
- 即使服务端配置了 AI Key，简历工坊也只会在用户对本次处理明确授权后调用外部模型。

## MCP

MCP 是可选适配层，不再是 Web 运行前置条件：

```bash
pip install -e '.[mcp]'
jobsearch-mcp-server
```

MCP 默认监听 `127.0.0.1:8000`，仅暴露 allowlist 工具，不提供任意工具调用或服务端路径读取。
