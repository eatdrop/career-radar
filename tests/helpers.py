from pathlib import Path

from jobsearch_mcp_server.config import Settings


def make_settings(tmp_path: Path, *, port: int = 0) -> Settings:
    base = Settings.from_env(tmp_path / "missing.env")
    return base.with_overrides(
        host="127.0.0.1",
        port=port,
        data_dir=tmp_path / "data",
        web_dir=Path(__file__).resolve().parents[1] / "web",
        scheduler_enabled=False,
        demo_enabled=True,
        store_raw_resume=True,
        ai_api_key="",
        serpapi_key="",
        smtp_host="",
        smtp_user="",
        smtp_password="",
        smtp_from="",
        allowed_origins=(),
    )


SAMPLE_RESUME = """沈同学
电话：13800138000
邮箱：student@example.com
求职意向：大模型应用开发实习生
某大学 人工智能专业 本科 2024-2028
技能：Python、FastAPI、RAG、Qdrant、MySQL、Docker、Git
项目经验：智职引擎，负责使用 FastAPI 和 RAG 构建求职助手，将岗位筛选时间降低 40%。
实习经历：参与 NLP 数据处理与模型评估，完成 3 个数据集清洗。
"""
