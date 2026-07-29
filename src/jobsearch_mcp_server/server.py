"""MCP adapter for the same domain service used by the Web API.

The MCP surface is intentionally small and allow-listed. It does not accept
arbitrary server-side file paths or expose a generic tool dispatcher.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .config import Settings
from .services import AppError, CareerService

LOGGER = logging.getLogger("jobsearch.mcp")


def _encode(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _call(operation):  # type: ignore[no-untyped-def]
    try:
        return _encode({"success": True, "data": operation()})
    except AppError as exc:
        return _encode(
            {
                "success": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        )
    except Exception:
        LOGGER.exception("MCP tool failed")
        return _encode(
            {
                "success": False,
                "error": {
                    "code": "internal_error",
                    "message": "服务处理失败，请稍后重试",
                },
            }
        )


class JobSearchMCPServer:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        settings: Settings | None = None,
    ):
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:
            raise RuntimeError("MCP 组件未安装，请执行：pip install -e '.[mcp]'") from exc

        self.settings = settings or Settings.from_env()
        mcp_host = host or os.getenv("MCP_HOST", "127.0.0.1")
        mcp_port = port or int(os.getenv("MCP_PORT", "8000"))
        self.mcp = FastMCP(
            "jobsearch-mcp-server",
            host=mcp_host,
            port=mcp_port,
            streamable_http_path="/mcp",
        )
        self.service = CareerService(self.settings)
        self._register_tools()

    def _register_tools(self) -> None:
        service = self.service

        @self.mcp.tool()
        def get_system_status() -> str:
            """获取服务健康状态、数据库状态和可用增强能力。"""
            return _call(service.health)

        @self.mcp.tool()
        def crawl_jobs(
            keyword: str = "AI应用开发",
            city: str = "上海",
            pages: int = 1,
            use_demo: bool = False,
        ) -> str:
            """采集职位并保存为最新岗位；use_demo=true 可显式使用演示数据。"""
            return _call(
                lambda: service.crawl_jobs(
                    {
                        "keyword": keyword,
                        "city": city,
                        "pages": pages,
                        "use_demo": use_demo,
                    }
                )
            )

        @self.mcp.tool()
        def analyze_resume(resume_text: str) -> str:
            """本地解析并评分简历文本，保存为最新简历。"""
            return _call(lambda: service.analyse_resume({"text": resume_text}))

        @self.mcp.tool()
        def match_jobs(
            resume_text: str = "",
            job_text: str = "",
            use_latest_resume: bool = True,
            use_latest_jobs: bool = True,
        ) -> str:
            """使用可解释规则对简历与岗位进行匹配排序。"""
            return _call(
                lambda: service.match_jobs(
                    {
                        "resume_source": "latest" if use_latest_resume else "paste",
                        "job_source": "latest" if use_latest_jobs else "paste",
                        "resume_text": resume_text,
                        "job_text": job_text,
                    }
                )
            )

        @self.mcp.tool()
        def enhance_resume(
            job_description: str,
            resume_text: str = "",
            template: str = "standard",
            use_latest_resume: bool = True,
            allow_ai: bool = False,
        ) -> str:
            """根据 JD 优化简历；仅在 allow_ai=true 时调用已配置的外部 AI。"""
            return _call(
                lambda: service.enhance_resume(
                    {
                        "resume_source": "latest" if use_latest_resume else "paste",
                        "resume_text": resume_text,
                        "jd": job_description,
                        "template": template,
                        "allow_ai": allow_ai,
                    }
                )
            )

        @self.mcp.tool()
        def list_applications(status: str = "", query: str = "") -> str:
            """查询投递记录。status 可为 submitted/viewed/interviewing/offered/rejected。"""
            return _call(lambda: service.list_applications(status, query))

        @self.mcp.tool()
        def add_application(
            company_name: str,
            job_title: str,
            status: str = "submitted",
            location: str = "",
            salary_range: str = "",
            notes: str = "",
        ) -> str:
            """新增投递记录。"""
            return _call(
                lambda: service.add_application(
                    {
                        "company_name": company_name,
                        "job_title": job_title,
                        "status": status,
                        "location": location,
                        "salary_range": salary_range,
                        "notes": notes,
                    }
                )
            )

        @self.mcp.tool()
        def update_application_status(application_id: int, status: str) -> str:
            """更新投递状态。"""
            return _call(lambda: service.update_application(application_id, {"status": status}))

    def run(self) -> None:
        self.mcp.run(transport="streamable-http")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    JobSearchMCPServer().run()


if __name__ == "__main__":
    main()
