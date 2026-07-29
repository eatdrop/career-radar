# 智职引擎技术架构

## 业务流

```mermaid
flowchart LR
    Trigger["手动 / 每日定时"] --> Sources["岗位来源<br/>岗位池 / SerpAPI / 51job"]
    Sources --> Filter["去重 + 硬门槛过滤"]
    Resume["简历画像"] --> Evidence["本地 RAG 证据检索"]
    Filter --> Rank["可解释匹配排序"]
    Evidence --> Rank
    Rank --> Digest["今日推荐 + 逐岗简历动作"]
    Digest --> Web["站内工作台"]
    Digest --> Email["SMTP 邮件（可选）"]
    Digest --> CRM["投递看板"]
```

## 运行架构

```mermaid
flowchart TB
    Browser["浏览器 SPA"] --> REST["Threading HTTP Server<br/>版本化 JSON API"]
    MCP["MCP 客户端（可选）"] --> Adapter["Allowlist MCP Adapter"]
    REST --> Service["CareerService"]
    Adapter --> Service
    Scheduler["单进程幂等调度器"] --> Service
    Service --> SQLite["SQLite WAL<br/>状态 / 简历证据 / 雷达运行 / 投递"]
    Service --> SerpAPI["SerpAPI（可选）"]
    Service --> Job51["51job + Chrome（可选）"]
    Service --> DeepSeek["DeepSeek V4（可选）"]
    Service --> SMTP["SMTP（可选）"]
```

## 关键边界

- Web 核心流程不依赖 MCP，也不依赖第三方 SDK；
- 所有外部能力延迟加载，缺失时返回可操作的错误；
- 简历证据先本地检索，再交给大模型，模型不拥有编造事实的权限；
- Web 不接受服务器本地文件路径，只接受受大小限制的文件内容；
- 默认仅本机访问；公网身份认证交由反向代理或平台网关；
- 内置调度器只适用于单进程，多副本部署必须使用外部调度与分布式锁。

## 持久化

SQLite 数据库包含：

- `resumes`：简历画像与评分；
- `resume_evidence`：可检索简历证据块；
- `app_state`：岗位池、偏好和最新摘要；
- `radar_runs`：工作流运行审计；
- `applications`：投递 CRM。

所有连接启用 WAL、`busy_timeout` 和外键约束。运行数据由 `APP_DATA_DIR` 指定，不写入安装目录。
