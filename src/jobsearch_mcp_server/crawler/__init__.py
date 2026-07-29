# src/jobsearch_mcp_server/crawler/__init__.py
"""职位爬虫模块：支持多城市、多关键词的职位数据采集"""

from .crawl_51job import CITY_CODES, fetch_51job, format_jobs_text

__all__ = ["fetch_51job", "format_jobs_text", "CITY_CODES"]
