"""Domain services for the industrialised web application."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import importlib.util
import json
import logging
import re
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from . import __version__
from .config import Settings
from .repository import SQLiteRepository
from .worker import DeliveryUnknownError, PermanentWorkerError, RetryWithoutAttemptError

LOGGER = logging.getLogger("jobsearch.services")

STATUS_TO_STORAGE = {
    "submitted": "已投递",
    "viewed": "已查看",
    "interviewing": "面试中",
    "offered": "已录用",
    "rejected": "已拒绝",
}
STORAGE_TO_STATUS = {value: key for key, value in STATUS_TO_STORAGE.items()}

CITIES = ("北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "武汉", "西安")
TEMPLATES = {"standard", "technical", "concise"}

MAX_RESUME_TEXT_CHARS = 100_000
MAX_RESUME_LINE_CHARS = 12_000
MAX_DOCX_ENTRIES = 2_048
MAX_DOCX_EXPANDED_BYTES = 12 * 1024 * 1024
MAX_DOCX_DOCUMENT_BYTES = 4 * 1024 * 1024

EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}"
    r"(?![A-Za-z0-9.-])"
)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
CONTACT_HANDLE_PATTERN = re.compile(
    r"(?i)(?:微信|wechat|qq)(?:\s*(?:id|号))?\s*[:：]?\s*[A-Za-z0-9_-]{5,32}"
)
INTERNATIONAL_PHONE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\+\d{1,3}(?:[\s().-]*\d){7,14}(?![A-Za-z0-9])"
)
PUBLIC_PROFILE_PATTERN = re.compile(
    r"(?i)https?://(?:www\.)?(?:linkedin\.com/in|github\.com)/[^\s<>'\"]+"
)

SKILL_GROUPS: dict[str, tuple[str, ...]] = {
    "programming_languages": (
        "Python",
        "Java",
        "Go",
        "Golang",
        "C++",
        "C#",
        "JavaScript",
        "TypeScript",
        "Rust",
        "Kotlin",
        "Swift",
        "PHP",
        "SQL",
        "R",
    ),
    "frameworks": (
        "FastAPI",
        "Flask",
        "Django",
        "Spring",
        "Spring Boot",
        "Vue",
        "Vue.js",
        "React",
        "Next.js",
        "Node.js",
        "PyTorch",
        "TensorFlow",
        "LangChain",
        "LlamaIndex",
        "Transformers",
    ),
    "databases": (
        "MySQL",
        "PostgreSQL",
        "SQLite",
        "Redis",
        "MongoDB",
        "Qdrant",
        "Milvus",
        "Elasticsearch",
        "ClickHouse",
    ),
    "platforms": (
        "Docker",
        "Kubernetes",
        "K8s",
        "Git",
        "Linux",
        "AWS",
        "Azure",
        "阿里云",
        "腾讯云",
        "Kafka",
        "RabbitMQ",
        "Nginx",
        "Jenkins",
    ),
    "ai": (
        "RAG",
        "LLM",
        "大模型",
        "机器学习",
        "深度学习",
        "NLP",
        "BERT",
        "向量数据库",
        "Prompt",
        "Agent",
        "MCP",
    ),
}

GENERIC_TERMS = {
    "开发",
    "项目",
    "系统",
    "技术",
    "负责",
    "熟悉",
    "掌握",
    "能力",
    "经验",
    "工作",
    "岗位",
    "相关",
    "以及",
    "进行",
    "使用",
    "具备",
    "要求",
}


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _clean_text(value: Any, *, limit: int, field: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AppError(422, "invalid_field", f"{field} 必须是文本")
    text = value.replace("\x00", "").strip()
    if required and not text:
        raise AppError(422, "missing_field", f"请填写{field}")
    if len(text) > limit:
        raise AppError(
            422,
            "field_too_long",
            f"{field}不能超过 {limit} 个字符",
            {"field": field, "max_length": limit},
        )
    return text


def _require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise AppError(422, "invalid_boolean", f"{field}必须是布尔值")
    return value


def _clean_string_list(
    value: Any,
    *,
    field: str,
    item_limit: int,
    max_items: int,
    error_code: str,
    required: bool = True,
) -> list[str]:
    if not isinstance(value, list):
        raise AppError(422, error_code, f"{field}必须是列表")
    if len(value) > max_items:
        raise AppError(422, error_code, f"{field}最多允许 {max_items} 项")
    cleaned = [_clean_text(item, limit=item_limit, field=field, required=True) for item in value]
    cleaned = list(dict.fromkeys(cleaned))
    if required and not cleaned:
        raise AppError(422, error_code, f"至少设置一项{field}")
    return cleaned


def _normalise_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _validate_resume_text(text: str) -> str:
    if len(text) > MAX_RESUME_TEXT_CHARS:
        raise AppError(
            413,
            "resume_too_large",
            f"简历文本不能超过 {MAX_RESUME_TEXT_CHARS} 个字符",
        )
    if any(len(line) > MAX_RESUME_LINE_CHARS for line in text.splitlines()):
        raise AppError(
            422,
            "resume_line_too_long",
            f"简历单行不能超过 {MAX_RESUME_LINE_CHARS} 个字符",
        )
    normalised = _normalise_whitespace(text)
    if len(normalised) > MAX_RESUME_TEXT_CHARS:
        raise AppError(
            413,
            "resume_too_large",
            f"简历文本不能超过 {MAX_RESUME_TEXT_CHARS} 个字符",
        )
    return normalised


def _redact_contact_info(text: str) -> str:
    redacted = EMAIL_PATTERN.sub("[邮箱已隐藏]", text)
    redacted = PHONE_PATTERN.sub("[手机号已隐藏]", redacted)
    redacted = INTERNATIONAL_PHONE_PATTERN.sub("[国际电话已隐藏]", redacted)
    redacted = CONTACT_HANDLE_PATTERN.sub("[即时通讯账号已隐藏]", redacted)
    return PUBLIC_PROFILE_PATTERN.sub("[公开账号已隐藏]", redacted)


def _redact_nested_contact_info(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_contact_info(value)
    if isinstance(value, list):
        return [_redact_nested_contact_info(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_nested_contact_info(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_nested_contact_info(item) for key, item in value.items()}
    return value


def _extract_skills(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    lowered = text.lower()
    for group, terms in SKILL_GROUPS.items():
        values = []
        for term in terms:
            pattern = r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])"
            if re.search(pattern, lowered):
                canonical = "Go" if term == "Golang" else "Kubernetes" if term == "K8s" else term
                if canonical not in values:
                    values.append(canonical)
        found[group] = values
    return found


def _flatten_skills(groups: dict[str, list[str]]) -> list[str]:
    output: list[str] = []
    for values in groups.values():
        for value in values:
            if value not in output:
                output.append(value)
    return output


def _extract_keywords(text: str) -> set[str]:
    skills = set(_flatten_skills(_extract_skills(text)))
    latin = {
        item
        for item in re.findall(r"[A-Za-z][A-Za-z0-9.+#/-]{1,24}", text)
        if item.lower() not in {"and", "with", "the", "for", "from"}
    }
    chinese = {
        item for item in re.findall(r"[\u4e00-\u9fff]{2,8}", text) if item not in GENERIC_TERMS
    }
    return skills | latin | chinese


def _name_from_resume(lines: list[str]) -> str:
    for line in lines[:6]:
        candidate = re.sub(r"^(姓名|Name)\s*[:：]\s*", "", line, flags=re.I).strip()
        if (
            2 <= len(candidate) <= 20
            and not re.search(r"[@\d]|简历|求职|电话|手机|邮箱", candidate)
            and len(candidate.split()) <= 3
        ):
            return candidate
    return "未命名候选人"


def _parse_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_DOCX_ENTRIES:
                raise AppError(413, "expanded_file_too_large", "DOCX 文件条目过多")
            total_size = sum(item.file_size for item in members)
            if total_size > MAX_DOCX_EXPANDED_BYTES:
                raise AppError(413, "expanded_file_too_large", "DOCX 解压后体积过大")
            document_info = archive.getinfo("word/document.xml")
            if document_info.file_size > MAX_DOCX_DOCUMENT_BYTES:
                raise AppError(413, "expanded_file_too_large", "DOCX 正文解压后体积过大")
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise AppError(422, "invalid_docx", "DOCX 文件损坏或格式不正确") from exc

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise AppError(422, "invalid_docx", "DOCX 文件损坏或格式不正确") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        pieces = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
        value = "".join(pieces).strip()
        if value:
            paragraphs.append(value)
    return "\n".join(paragraphs)


def _parse_pdf(content: bytes) -> str:
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise AppError(
            503,
            "pdf_parser_unavailable",
            "PDF 解析组件未安装；请安装 files 可选依赖，或上传 DOCX/TXT",
        ) from exc
    try:
        document = fitz.open(stream=content, filetype="pdf")
        text = "\n".join(page.get_text() for page in document)
        document.close()
        return text
    except Exception as exc:
        raise AppError(422, "invalid_pdf", "PDF 文件无法解析") from exc


def _decode_resume_file(payload: dict[str, Any], max_bytes: int) -> tuple[str, str]:
    file_data = payload.get("file")
    if not isinstance(file_data, dict):
        raise AppError(422, "missing_file", "请选择简历文件")
    name = _clean_text(file_data.get("name"), limit=180, field="文件名", required=True)
    encoded = file_data.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise AppError(422, "missing_file_content", "文件内容为空")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppError(422, "invalid_file_encoding", "文件编码无效") from exc
    if len(content) > max_bytes:
        raise AppError(413, "file_too_large", "文件大小超过允许上限")
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in {"txt", "md"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise AppError(422, "unsupported_encoding", "文本文件请使用 UTF-8 编码") from exc
    elif suffix == "docx":
        text = _parse_docx(content)
    elif suffix == "pdf":
        text = _parse_pdf(content)
    else:
        raise AppError(422, "unsupported_file", "仅支持 TXT、Markdown、DOCX 和 PDF")
    return _validate_resume_text(text), name


def _analyse_resume(text: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    lines = [line.strip(" •·-\t") for line in text.splitlines() if line.strip()]
    name = _name_from_resume(lines)
    email_match = EMAIL_PATTERN.search(text)
    phone_match = PHONE_PATTERN.search(text)
    target_match = re.search(r"(?:求职意向|目标岗位|应聘岗位)\s*[:：]\s*([^\n]{2,40})", text)
    skills = _extract_skills(text)
    all_skills = _flatten_skills(skills)
    education = [
        line for line in lines if re.search(r"大学|学院|本科|硕士|博士|大专|专业|GPA", line, re.I)
    ][:8]
    experience = [
        line
        for line in lines
        if re.search(r"20\d{2}|实习|任职|有限公司|公司|工作经历", line) and line not in education
    ][:12]
    projects = [line for line in lines if re.search(r"项目|课设|毕设|作品", line)][:10]
    quantified = re.findall(r"\d+(?:\.\d+)?\s*(?:%|万|千|倍|人|个|项|ms|秒|天)", text, re.I)

    structured = {
        "basic_info": {
            "name": name,
            "email": email_match.group(0) if email_match else "",
            "phone": phone_match.group(0) if phone_match else "",
            "target_position": target_match.group(1).strip() if target_match else "",
        },
        "skills": skills,
        "education": education,
        "experience": experience,
        "projects": projects,
        "evidence": {"quantified_results": quantified[:12]},
    }

    contact_score = 10 if email_match and phone_match else 6 if email_match or phone_match else 2
    skills_score = min(25, 5 + len(all_skills) * 2)
    project_score = min(20, len(projects) * 5 + min(5, len(quantified)))
    experience_score = min(20, len(experience) * 3 + min(5, len(quantified)))
    education_score = 10 if education else 3
    clarity_score = 10 if 200 <= len(text) <= 5000 and len(lines) >= 8 else 6
    target_score = 5 if target_match else 1
    total = min(
        100,
        contact_score
        + skills_score
        + project_score
        + experience_score
        + education_score
        + clarity_score
        + target_score,
    )

    dimensions = [
        {"key": "contact", "label": "联系信息", "score": contact_score, "max": 10},
        {"key": "skills", "label": "技能覆盖", "score": skills_score, "max": 25},
        {"key": "projects", "label": "项目成果", "score": project_score, "max": 20},
        {"key": "experience", "label": "经历质量", "score": experience_score, "max": 20},
        {"key": "education", "label": "教育背景", "score": education_score, "max": 10},
        {"key": "clarity", "label": "结构可读", "score": clarity_score, "max": 10},
        {"key": "target", "label": "求职目标", "score": target_score, "max": 5},
    ]
    strengths: list[str] = []
    suggestions: list[str] = []
    if len(all_skills) >= 6:
        strengths.append("技术栈信息较完整，便于招聘系统检索")
    else:
        suggestions.append("补充与目标岗位相关的具体技术栈，并给出使用场景")
    if quantified:
        strengths.append("包含量化成果，能直观展示项目价值")
    else:
        suggestions.append("为项目与实习经历补充真实的规模、效率或质量指标")
    if not target_match:
        suggestions.append("增加明确的求职意向，让内容围绕目标岗位排序")
    if not projects:
        suggestions.append("补充 1–3 个项目，并写清角色、行动、技术与结果")
    if not (email_match and phone_match):
        suggestions.append("补全可用的邮箱与手机号")
    score = {
        "total": total,
        "level": "优秀" if total >= 85 else "良好" if total >= 70 else "待提升",
        "dimensions": dimensions,
        "strengths": strengths,
        "suggestions": suggestions[:5],
    }

    summary_parts = [name]
    target = structured["basic_info"]["target_position"]
    if target:
        summary_parts.append(f"目标岗位：{target}")
    if education:
        summary_parts.append(f"教育背景：{education[0]}")
    if all_skills:
        summary_parts.append(f"核心技能：{'、'.join(all_skills[:10])}")
    if projects:
        summary_parts.append(f"项目亮点：{projects[0]}")
    elif experience:
        summary_parts.append(f"经历亮点：{experience[0]}")
    summary = "\n".join(summary_parts)
    return structured, score, summary


def _safe_external_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return candidate


def _normalise_job(raw: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or f"job-{index}"),
        "title": str(
            raw.get("title") or raw.get("职位名称") or raw.get("position") or "未命名岗位"
        ),
        "company": str(raw.get("company") or raw.get("公司名称") or "未知公司"),
        "salary": str(raw.get("salary") or raw.get("薪资范围") or "面议"),
        "location": str(raw.get("location") or raw.get("工作地点") or ""),
        "description": str(
            raw.get("description") or raw.get("job_description") or raw.get("经验学历") or ""
        ),
        "source": str(raw.get("source") or raw.get("来源") or "导入"),
        "url": _safe_external_url(raw.get("url")),
    }


def _parse_job_text(text: str) -> list[dict[str, Any]]:
    blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n|(?=岗位(?:名称)?\s*[:：])", text)
        if block.strip()
    ]
    if len(blocks) > 30:
        blocks = blocks[:30]
    jobs: list[dict[str, Any]] = []
    for index, block in enumerate(blocks, 1):
        title_match = re.search(r"(?:岗位(?:名称)?|职位)\s*[:：]\s*([^\n|]{2,50})", block)
        company_match = re.search(r"(?:公司(?:名称)?)\s*[:：]\s*([^\n|]{2,50})", block)
        salary_match = re.search(r"(?:薪资(?:范围)?)\s*[:：]\s*([^\n|]{2,30})", block)
        location_match = re.search(r"(?:地点|工作地点)\s*[:：]\s*([^\n|]{2,30})", block)
        first_line = block.splitlines()[0][:50]
        jobs.append(
            {
                "id": f"pasted-{index}",
                "title": title_match.group(1).strip() if title_match else first_line,
                "company": company_match.group(1).strip() if company_match else "粘贴岗位",
                "salary": salary_match.group(1).strip() if salary_match else "面议",
                "location": location_match.group(1).strip() if location_match else "",
                "description": block,
                "source": "手动粘贴",
                "url": "",
            }
        )
    return jobs


DEMO_JOBS = [
    {
        "id": "demo-ai-01",
        "title": "大模型应用开发实习生",
        "company": "星云智能（演示）",
        "salary": "200–300 元/天",
        "location": "上海",
        "description": "负责 Python、FastAPI、RAG、向量数据库与大模型应用开发；熟悉 Docker、Git，具备项目实践。",
        "source": "演示数据",
        "url": "",
    },
    {
        "id": "demo-backend-02",
        "title": "Python 后端开发工程师",
        "company": "远航科技（演示）",
        "salary": "12–18K",
        "location": "杭州",
        "description": "使用 Python、Django 或 FastAPI 开发服务，熟悉 MySQL、Redis、Linux、Docker。",
        "source": "演示数据",
        "url": "",
    },
    {
        "id": "demo-algo-03",
        "title": "NLP 算法实习生",
        "company": "知行数据（演示）",
        "salary": "180–260 元/天",
        "location": "北京",
        "description": "参与 NLP、BERT、PyTorch、机器学习模型训练与评估，要求 Python 基础和论文复现经验。",
        "source": "演示数据",
        "url": "",
    },
]

DEFAULT_RADAR_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "schedule_time": "21:00",
    "keywords": ["AI应用开发", "Python后端"],
    "cities": ["上海", "杭州"],
    "sources": ["latest"],
    "new_grad_only": True,
    "exclude_keywords": [
        "Senior",
        "Lead",
        "Manager",
        "总监",
        "负责人",
        "专家",
        "5年以上",
        "劳务",
        "外包",
    ],
    "min_score": 55,
    "max_results": 20,
    "email_enabled": False,
    "email_to": "",
}

RADAR_JOB_TYPE = "radar_run"
RADAR_REQUEST_KEYS = frozenset({"sources", "use_demo", "send_email"})
PERMANENT_RADAR_JOB_ERRORS = frozenset(
    {
        "resume_unavailable",
        "no_job_sources",
        "serpapi_unavailable",
        "crawler_unavailable",
    }
)
PUBLIC_RADAR_JOB_ERRORS = frozenset(
    {
        *PERMANENT_RADAR_JOB_ERRORS,
        "aggregator_failed",
        "crawler_failed",
        "radar_failed",
        "radar_run_failed",
    }
)


@dataclass(slots=True)
class Capability:
    enabled: bool
    reason: str


class CareerService:
    def __init__(self, settings: Settings, repository: SQLiteRepository | None = None):
        self.settings = settings
        self.repository = repository or SQLiteRepository(settings.data_dir)
        self._radar_lock = threading.Lock()

    def capabilities(self) -> dict[str, dict[str, Any]]:
        openai_installed = importlib.util.find_spec("openai") is not None
        selenium_installed = (
            importlib.util.find_spec("selenium") is not None
            and importlib.util.find_spec("webdriver_manager") is not None
        )
        fitz_installed = importlib.util.find_spec("fitz") is not None
        return {
            "core": {"enabled": True, "reason": "离线核心能力已就绪"},
            "ai": {
                "enabled": bool(self.settings.ai_api_key and openai_installed),
                "reason": (
                    "DeepSeek 已配置"
                    if self.settings.ai_api_key and openai_installed
                    else "需要 DEEPSEEK_API_KEY 与 ai 可选依赖"
                ),
            },
            "crawler": {
                "enabled": selenium_installed,
                "reason": "Selenium 已安装"
                if selenium_installed
                else "需要 crawler 可选依赖与 Chrome",
            },
            "job_aggregator": {
                "enabled": bool(self.settings.serpapi_key),
                "reason": "SerpAPI 已配置" if self.settings.serpapi_key else "需要 SERPAPI_KEY",
            },
            "email": {
                "enabled": bool(
                    self.settings.smtp_host
                    and self.settings.smtp_user
                    and self.settings.smtp_password
                ),
                "reason": (
                    "SMTP 已配置"
                    if self.settings.smtp_host
                    and self.settings.smtp_user
                    and self.settings.smtp_password
                    else "需要 SMTP_HOST / SMTP_USER / SMTP_PASSWORD"
                ),
            },
            "pdf": {
                "enabled": fitz_installed,
                "reason": "PDF 解析已就绪" if fitz_installed else "需要 files 可选依赖",
            },
            "demo": {
                "enabled": self.settings.demo_enabled,
                "reason": "可显式使用演示岗位" if self.settings.demo_enabled else "演示模式已关闭",
            },
        }

    def health(self) -> dict[str, Any]:
        schema_version: int | None = None
        queues: dict[str, dict[str, int]] = {}
        try:
            self.repository.application_statistics()
            schema_version = self.repository.get_schema_version()
            queues = self.repository.queue_statistics()
            database = "ok"
        except sqlite3.Error:  # pragma: no cover - defensive branch
            database = "error"
        return {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "schema_version": schema_version,
            "queues": queues,
            "version": __version__,
            "capabilities": self.capabilities(),
        }

    def dashboard(self) -> dict[str, Any]:
        jobs = self.repository.get_state("latest_jobs", [])
        stats = self._public_stats(self.repository.application_statistics())
        runs = self.repository.list_radar_runs(1)
        return {
            "jobs": len(jobs) if isinstance(jobs, list) else 0,
            "resumes": self.repository.count_resumes(),
            "applications": stats,
            "radar": {
                "last_run": (
                    {key: value for key, value in runs[0].items() if key != "result"}
                    if runs
                    else None
                ),
                "settings": self.get_radar_settings(),
            },
            "capabilities": self.capabilities(),
        }

    def get_radar_settings(self) -> dict[str, Any]:
        stored = self.repository.get_state("radar_settings", {})
        settings = dict(DEFAULT_RADAR_SETTINGS)
        if isinstance(stored, dict):
            settings.update(stored)
        # Secrets are environment-only and never returned.
        settings["delivery_ready"] = self.capabilities()["email"]["enabled"]
        settings["aggregator_ready"] = self.capabilities()["job_aggregator"]["enabled"]
        return settings

    def update_radar_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_radar_settings()
        current.pop("delivery_ready", None)
        current.pop("aggregator_ready", None)

        if "enabled" in payload:
            current["enabled"] = _require_bool(payload["enabled"], field="雷达开关")
        if "schedule_time" in payload:
            schedule_time = _clean_text(
                payload["schedule_time"], limit=5, field="执行时间", required=True
            )
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
                raise AppError(422, "invalid_schedule", "执行时间格式应为 HH:MM")
            current["schedule_time"] = schedule_time
        if "keywords" in payload:
            current["keywords"] = _clean_string_list(
                payload["keywords"],
                field="岗位关键词",
                item_limit=40,
                max_items=6,
                error_code="invalid_keywords",
            )
        if "cities" in payload:
            current["cities"] = _clean_string_list(
                payload["cities"],
                field="城市",
                item_limit=20,
                max_items=5,
                error_code="invalid_cities",
            )
        if "sources" in payload:
            allowed_sources = {"latest", "serpapi", "51job", "demo"}
            sources = _clean_string_list(
                payload["sources"],
                field="职位来源",
                item_limit=20,
                max_items=len(allowed_sources),
                error_code="invalid_sources",
            )
            if any(source not in allowed_sources for source in sources):
                raise AppError(422, "invalid_sources", "包含不支持的职位来源")
            current["sources"] = sources
        if "new_grad_only" in payload:
            current["new_grad_only"] = _require_bool(
                payload["new_grad_only"], field="应届生筛选开关"
            )
        if "exclude_keywords" in payload:
            current["exclude_keywords"] = _clean_string_list(
                payload["exclude_keywords"],
                field="排除词",
                item_limit=30,
                max_items=30,
                error_code="invalid_exclusions",
                required=False,
            )
        if "min_score" in payload:
            try:
                score = int(payload["min_score"])
            except (TypeError, ValueError) as exc:
                raise AppError(422, "invalid_score", "最低匹配分必须是整数") from exc
            if not 0 <= score <= 100:
                raise AppError(422, "invalid_score", "最低匹配分必须在 0–100 之间")
            current["min_score"] = score
        if "max_results" in payload:
            try:
                count = int(payload["max_results"])
            except (TypeError, ValueError) as exc:
                raise AppError(422, "invalid_result_limit", "推荐数量必须是整数") from exc
            if not 1 <= count <= 50:
                raise AppError(422, "invalid_result_limit", "推荐数量必须在 1–50 之间")
            current["max_results"] = count
        if "email_enabled" in payload:
            current["email_enabled"] = _require_bool(payload["email_enabled"], field="邮件推送开关")
        if "email_to" in payload:
            email_to = _clean_text(payload["email_to"], limit=160, field="收件邮箱")
            if email_to and not EMAIL_PATTERN.fullmatch(email_to):
                raise AppError(422, "invalid_email", "收件邮箱格式无效")
            current["email_to"] = email_to

        if current["enabled"] and current["email_enabled"]:
            if not current["email_to"]:
                raise AppError(422, "missing_email", "开启邮件推送前请填写收件邮箱")
            if not self.capabilities()["email"]["enabled"]:
                raise AppError(409, "email_unavailable", "SMTP 尚未配置，无法开启邮件推送")
        self.repository.set_state("radar_settings", current)
        return self.get_radar_settings()

    def radar_overview(self) -> dict[str, Any]:
        runs = [
            {key: value for key, value in run.items() if key != "result"}
            for run in self.repository.list_radar_runs(20)
        ]
        latest = self.repository.get_state("latest_radar", None)
        return {
            "settings": self.get_radar_settings(),
            "latest": self._hydrate_email_delivery(latest),
            "runs": runs,
            "operations": [
                self._public_radar_operation(job, include_result=False)
                for job in self.repository.list_operation_jobs(
                    job_type=RADAR_JOB_TYPE,
                    limit=20,
                )
            ],
        }

    def _normalise_radar_request(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise AppError(422, "invalid_payload", "请求体必须是 JSON 对象")
        unknown = set(payload) - RADAR_REQUEST_KEYS
        if unknown:
            raise AppError(
                422,
                "unknown_fields",
                f"包含不支持的字段：{', '.join(sorted(unknown))}",
            )
        normalised: dict[str, Any] = {}
        if "use_demo" in payload:
            normalised["use_demo"] = _require_bool(payload["use_demo"], field="演示数据开关")
        if "send_email" in payload:
            normalised["send_email"] = _require_bool(payload["send_email"], field="邮件发送开关")
        if "sources" in payload:
            sources = _clean_string_list(
                payload["sources"],
                field="职位来源",
                item_limit=20,
                max_items=4,
                error_code="invalid_sources",
            )
            if any(source not in {"latest", "serpapi", "51job", "demo"} for source in sources):
                raise AppError(422, "invalid_sources", "包含不支持的职位来源")
            normalised["sources"] = sources
        return normalised

    def enqueue_radar_operation(
        self,
        payload: dict[str, Any] | None,
        *,
        idempotency_key: str,
        trigger_type: str = "manual",
    ) -> dict[str, Any]:
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not re.fullmatch(r"[A-Za-z0-9._:~/-]{8,128}", key):
            raise AppError(
                422,
                "invalid_idempotency_key",
                "Idempotency-Key 需为 8–128 位安全 ASCII 字符",
            )
        if trigger_type not in {"manual", "scheduled"}:
            raise AppError(422, "invalid_trigger_type", "不支持的雷达触发类型")
        request_payload = self._normalise_radar_request(payload)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        durable_key = f"radar:{trigger_type}:{key_hash}"
        run_identity_hash = hashlib.sha256(durable_key.encode("utf-8")).hexdigest()
        operation_payload = {
            "request": request_payload,
            "trigger_type": trigger_type,
            "email_idempotency_key": f"radar-email:{trigger_type}:{key_hash}",
            "radar_run_id": f"operation-{run_identity_hash}",
        }
        job, created = self.repository.enqueue_operation_job_once(
            RADAR_JOB_TYPE,
            operation_payload,
            idempotency_key=durable_key,
            max_attempts=3,
        )
        if not created and job.get("payload") != operation_payload:
            raise AppError(
                409,
                "idempotency_conflict",
                "该 Idempotency-Key 已用于不同的雷达请求",
            )
        return self._public_radar_operation(job, deduplicated=not created)

    def execute_radar_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PermanentWorkerError("持久任务载荷无效")
        request_payload = payload.get("request")
        trigger_type = payload.get("trigger_type")
        email_key = payload.get("email_idempotency_key")
        radar_run_id = payload.get("radar_run_id")
        if (
            not isinstance(request_payload, dict)
            or trigger_type not in {"manual", "scheduled"}
            or not isinstance(email_key, str)
            or not email_key
            or not isinstance(radar_run_id, str)
            or not re.fullmatch(r"operation-[a-f0-9]{64}", radar_run_id)
        ):
            raise PermanentWorkerError("持久任务载荷无效")
        try:
            return self.run_radar(
                request_payload,
                trigger_type=str(trigger_type),
                email_idempotency_key=email_key,
                radar_run_id=radar_run_id,
            )
        except AppError as error:
            if error.code == "radar_already_running":
                raise RetryWithoutAttemptError(
                    error.message,
                    retry_after_seconds=30,
                ) from error
            if error.code in PERMANENT_RADAR_JOB_ERRORS:
                raise PermanentWorkerError(error.message, code=error.code) from error
            raise

    def get_radar_operation(self, operation_id: int) -> dict[str, Any]:
        job = self.repository.get_operation_job(operation_id)
        if not job or job.get("job_type") != RADAR_JOB_TYPE:
            raise AppError(404, "radar_run_not_found", "雷达任务不存在")
        return self._public_radar_operation(job)

    def list_radar_operations(self, limit: int = 20) -> dict[str, Any]:
        jobs = self.repository.list_operation_jobs(
            job_type=RADAR_JOB_TYPE,
            limit=max(1, min(100, limit)),
        )
        return {"items": [self._public_radar_operation(job, include_result=False) for job in jobs]}

    def _public_radar_operation(
        self,
        job: dict[str, Any],
        *,
        deduplicated: bool | None = None,
        include_result: bool = True,
    ) -> dict[str, Any]:
        result = job.get("result")
        error: dict[str, Any] | None = None
        if job.get("status") == "failed":
            raw_error = str(job.get("error") or "")
            try:
                decoded_error = json.loads(raw_error)
            except json.JSONDecodeError:
                decoded_error = {}
            code = str(decoded_error.get("code") or "radar_run_failed")
            if code not in PUBLIC_RADAR_JOB_ERRORS:
                code = "radar_run_failed"
            message = str(decoded_error.get("message") or "")
            if not message or code == "radar_run_failed":
                message = "求职雷达执行失败，请检查配置后重试"
            error = {
                "code": code,
                "message": message[:300],
                "retryable": bool(decoded_error.get("retryable", True)),
            }
        output: dict[str, Any] = {
            "id": str(job["id"]),
            "status": str(job["status"]),
            "status_url": f"/api/v1/radar/runs/{job['id']}",
            "created_at": job.get("created_at"),
            "started_at": job.get("claimed_at") or job.get("heartbeat_at"),
            "finished_at": job.get("completed_at"),
            "attempts": int(job.get("attempts") or 0),
            "max_attempts": int(job.get("max_attempts") or 0),
            "result": (
                self._hydrate_email_delivery(result)
                if include_result and isinstance(result, dict)
                else None
            ),
            "error": error,
        }
        if deduplicated is not None:
            output["deduplicated"] = deduplicated
        return output

    def _hydrate_email_delivery(self, digest: Any) -> Any:
        if not isinstance(digest, dict):
            return digest
        delivery = digest.get("email_delivery")
        if not isinstance(delivery, dict):
            return digest
        message_id = delivery.get("message_id")
        if isinstance(message_id, bool):
            return digest
        try:
            parsed_id = int(message_id)
        except (TypeError, ValueError):
            return digest
        message = self.repository.get_outbox_message(parsed_id)
        if not message:
            return digest
        hydrated = dict(digest)
        hydrated["email_delivery"] = {
            "status": message["status"],
            "message_id": parsed_id,
        }
        if message["status"] == "delivery_unknown":
            hydrated["email_delivery"]["message"] = "邮件服务返回结果不明确，请核对收件箱"
        elif message["status"] == "failed":
            hydrated["email_delivery"]["message"] = "邮件发送失败，请检查 SMTP 配置"
        return hydrated

    def _resume_text(self, resume: dict[str, Any] | None) -> str:
        if not resume:
            return ""
        raw_text = resume.get("raw_text")
        if isinstance(raw_text, str) and raw_text:
            return raw_text
        resume_id = resume.get("id")
        if not isinstance(resume_id, str) or not resume_id:
            return ""
        return self.repository.get_resume_evidence_text(resume_id)

    def run_radar(
        self,
        payload: dict[str, Any] | None = None,
        trigger_type: str = "manual",
        *,
        email_idempotency_key: str | None = None,
        radar_run_id: str | None = None,
    ) -> dict[str, Any]:
        normalised_payload = self._normalise_radar_request(payload)
        if not self._radar_lock.acquire(blocking=False):
            raise AppError(409, "radar_already_running", "求职雷达正在运行，请稍后再试")
        try:
            return self._run_radar(
                normalised_payload,
                trigger_type,
                email_idempotency_key=email_idempotency_key,
                radar_run_id=radar_run_id,
            )
        finally:
            self._radar_lock.release()

    def _run_radar(
        self,
        payload: dict[str, Any],
        trigger_type: str,
        *,
        email_idempotency_key: str | None,
        radar_run_id: str | None,
    ) -> dict[str, Any]:
        run_id = radar_run_id or uuid.uuid4().hex
        if radar_run_id:
            existing = self.repository.get_radar_run(radar_run_id)
            if (
                existing
                and existing.get("status") == "completed"
                and isinstance(existing.get("result"), dict)
                and existing["result"].get("summary")
            ):
                return self._hydrate_email_delivery(existing["result"])
        self.repository.create_radar_run(run_id, trigger_type)
        source_count = 0
        candidate_count = 0
        try:
            radar_settings = self.get_radar_settings()
            sources = list(radar_settings["sources"])
            use_demo = (
                _require_bool(payload["use_demo"], field="演示数据开关")
                if "use_demo" in payload
                else False
            )
            send_email = (
                _require_bool(payload["send_email"], field="邮件发送开关")
                if "send_email" in payload
                else False
            )
            if use_demo and "demo" not in sources:
                sources = ["demo", *sources]
            if payload.get("sources") is not None:
                sources = _clean_string_list(
                    payload["sources"],
                    field="职位来源",
                    item_limit=20,
                    max_items=4,
                    error_code="invalid_sources",
                )
                if any(source not in {"latest", "serpapi", "51job", "demo"} for source in sources):
                    raise AppError(422, "invalid_sources", "包含不支持的职位来源")

            resume = self.repository.get_latest_resume()
            resume_text = self._resume_text(resume)
            if not resume or not resume_text:
                raise AppError(
                    409,
                    "resume_unavailable",
                    "求职雷达需要一份简历画像，请先在「简历画像」完成分析",
                )

            jobs: list[dict[str, Any]] = []
            source_notes: list[str] = []
            for source in sources:
                source_jobs: list[dict[str, Any]] = []
                if source == "demo":
                    if not self.settings.demo_enabled:
                        source_notes.append("演示来源已关闭")
                        continue
                    source_jobs = [dict(job) for job in DEMO_JOBS]
                elif source == "latest":
                    stored = self.repository.get_state("latest_jobs", [])
                    if isinstance(stored, list):
                        source_jobs = [
                            _normalise_job(item, index) for index, item in enumerate(stored, 1)
                        ]
                    if not source_jobs:
                        source_notes.append("本地岗位池暂无数据")
                elif source == "serpapi":
                    source_jobs = self._collect_serpapi_jobs(radar_settings)
                elif source == "51job":
                    source_jobs = self._collect_51job_jobs(radar_settings)
                else:
                    source_notes.append(f"忽略未知来源：{source}")
                if source_jobs:
                    source_count += 1
                    jobs.extend(source_jobs)

            if not jobs:
                raise AppError(
                    409,
                    "no_job_sources",
                    "没有获得职位。请先导入岗位、配置聚合 API，或勾选演示数据。",
                    {"notes": source_notes},
                )

            deduplicated: dict[str, dict[str, Any]] = {}
            for job in jobs:
                key = re.sub(r"\s+", "", f"{job.get('company', '')}|{job.get('title', '')}").lower()
                deduplicated.setdefault(key, job)
            jobs = list(deduplicated.values())
            candidate_count = len(jobs)

            filtered, rejected = self._filter_radar_jobs(jobs, radar_settings)
            ranked = self._rank_jobs(resume_text, filtered, int(radar_settings["max_results"]))
            for job in ranked:
                evidence = self.repository.retrieve_resume_evidence(
                    resume["id"],
                    f"{job.get('title', '')}\n{job.get('description', '')}",
                    limit=3,
                )
                job["resume_evidence"] = [
                    {
                        "text": _redact_contact_info(item["text"])[:360],
                        "relevance": item["score"],
                    }
                    for item in evidence
                    if item["score"] > 0
                ]
            shortlisted = [
                job for job in ranked if job["score"] >= int(radar_settings["min_score"])
            ]
            digest = {
                "run_id": run_id,
                "generated_at": self._now_local_iso(),
                "trigger": trigger_type,
                "summary": {
                    "collected": candidate_count,
                    "after_filter": len(filtered),
                    "shortlisted": len(shortlisted),
                    "filtered_out": len(rejected),
                },
                "items": shortlisted,
                "filter_breakdown": self._filter_breakdown(rejected),
                "source_notes": source_notes,
                "notice": "匹配度用于减少筛选成本，请在投递前核验岗位真实性与截止时间。",
                "email_delivery": {"status": "not_requested"},
            }

            should_email = bool(
                radar_settings.get("email_enabled")
                and radar_settings.get("email_to")
                and (trigger_type == "scheduled" or send_email)
            )
            if should_email:
                digest["email_delivery"] = {"status": "queued"}
            delivery_key = (
                email_idempotency_key
                if should_email and email_idempotency_key
                else f"radar-email:run:{run_id}"
            )
            completed_digest, _ = self.repository.complete_radar_run(
                run_id,
                source_count=source_count,
                candidate_count=candidate_count,
                shortlisted_count=len(shortlisted),
                result=digest,
                outbox_recipient=(str(radar_settings["email_to"]) if should_email else ""),
                outbox_idempotency_key=delivery_key if should_email else "",
                outbox_payload=(
                    {
                        "summary": dict(digest["summary"]),
                        "items": [
                            {
                                field: job.get(field)
                                for field in (
                                    "title",
                                    "score",
                                    "company",
                                    "location",
                                    "salary",
                                    "why_fit",
                                    "resume_tips",
                                    "url",
                                )
                            }
                            for job in digest["items"]
                        ],
                    }
                    if should_email
                    else None
                ),
            )
            return completed_digest
        except AppError as exc:
            self.repository.finish_radar_run(
                run_id,
                status="failed",
                source_count=source_count,
                candidate_count=candidate_count,
                error_message=exc.message,
            )
            raise
        except Exception as exc:
            LOGGER.exception("radar workflow failed run_id=%s", run_id)
            self.repository.finish_radar_run(
                run_id,
                status="failed",
                source_count=source_count,
                candidate_count=candidate_count,
                error_message="工作流执行失败",
            )
            raise AppError(500, "radar_failed", "求职雷达执行失败，请稍后重试") from exc

    def _collect_serpapi_jobs(self, radar_settings: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.settings.serpapi_key:
            raise AppError(409, "serpapi_unavailable", "选择了 Google Jobs，但未配置 SERPAPI_KEY")
        output: list[dict[str, Any]] = []
        combinations = [
            (keyword, city)
            for keyword in radar_settings["keywords"]
            for city in radar_settings["cities"]
        ][:6]
        for keyword, city in combinations:
            query = urlencode(
                {
                    "engine": "google_jobs",
                    "q": keyword,
                    "location": f"{city}, China",
                    "hl": "zh-cn",
                    "api_key": self.settings.serpapi_key,
                }
            )
            request = Request(
                f"https://serpapi.com/search.json?{query}",
                headers={"User-Agent": f"CareerEngine/{__version__}"},
            )
            try:
                with urlopen(request, timeout=25) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                LOGGER.warning("SerpAPI request failed type=%s", type(exc).__name__)
                raise AppError(502, "aggregator_failed", "Google Jobs 聚合请求失败") from exc
            for index, item in enumerate(payload.get("jobs_results", []), 1):
                apply_options = item.get("apply_options") or []
                apply_url = (
                    apply_options[0].get("link", "")
                    if apply_options
                    else item.get("share_link", "")
                )
                detected = item.get("detected_extensions") or {}
                output.append(
                    {
                        "id": item.get("job_id") or f"serp-{len(output) + index}",
                        "title": item.get("title", "未命名岗位"),
                        "company": item.get("company_name", "未知公司"),
                        "salary": detected.get("salary", "面议"),
                        "location": item.get("location", city),
                        "description": item.get("description", ""),
                        "source": "Google Jobs",
                        "url": _safe_external_url(apply_url),
                    }
                )
        return output

    def _collect_51job_jobs(self, radar_settings: dict[str, Any]) -> list[dict[str, Any]]:
        if (
            importlib.util.find_spec("selenium") is None
            or importlib.util.find_spec("webdriver_manager") is None
        ):
            raise AppError(409, "crawler_unavailable", "选择了 51job，但爬虫组件未安装")
        try:
            from .crawler.crawl_51job import fetch_51job

            raw = fetch_51job(
                keyword=radar_settings["keywords"][0],
                city=radar_settings["cities"][0],
                max_pages=1,
                headless=True,
            )
        except Exception as exc:
            raise AppError(502, "crawler_failed", "51job 采集失败") from exc
        return [_normalise_job(job, index) for index, job in enumerate(raw, 1)]

    @staticmethod
    def _filter_radar_jobs(
        jobs: list[dict[str, Any]], radar_settings: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        exclusions = [str(value).lower() for value in radar_settings["exclude_keywords"]]
        senior_terms = ("senior", "lead", "principal", "manager", "总监", "负责人", "专家", "资深")
        for job in jobs:
            haystack = f"{job.get('title', '')} {job.get('description', '')}".lower()
            reason = ""
            hit = next((term for term in exclusions if term and term in haystack), "")
            if hit:
                reason = f"命中排除词：{hit}"
            elif radar_settings.get("new_grad_only") and any(
                term in haystack for term in senior_terms
            ):
                reason = "疑似资深岗位"
            elif radar_settings.get("new_grad_only"):
                years = [
                    int(value)
                    for value in re.findall(r"(?<!\d)(\d{1,2})\s*年(?:以上|经验)", haystack)
                ]
                if years and max(years) >= 3:
                    reason = f"经验门槛 {max(years)} 年"
            if reason:
                rejected.append({**job, "filter_reason": reason})
            else:
                accepted.append(job)
        return accepted, rejected

    @staticmethod
    def _filter_breakdown(rejected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for job in rejected:
            reason = job.get("filter_reason", "其他")
            counts[reason] = counts.get(reason, 0) + 1
        return [
            {"reason": reason, "count": count}
            for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
        ][:8]

    @staticmethod
    def _rank_jobs(
        resume_text: str, jobs: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        resume_terms = _extract_keywords(resume_text)
        resume_skills = set(_flatten_skills(_extract_skills(resume_text)))
        results: list[dict[str, Any]] = []
        evidence_count = len(re.findall(r"项目|实习|负责|实现|优化|设计", resume_text))
        for job in jobs[:100]:
            job_text = f"{job.get('title', '')}\n{job.get('description', '')}"
            job_terms = _extract_keywords(job_text)
            job_skills = set(_flatten_skills(_extract_skills(job_text)))
            required = job_skills or {term for term in job_terms if len(term) <= 24}
            matched = sorted(required & (resume_skills | resume_terms), key=str.lower)
            missing = sorted(required - (resume_skills | resume_terms), key=str.lower)
            coverage = len(matched) / max(1, len(required))
            title_bonus = min(15, len(_extract_keywords(job.get("title", "")) & resume_terms) * 5)
            evidence_bonus = min(15, evidence_count * 2)
            score = min(98, round(30 + coverage * 40 + evidence_bonus + title_bonus))
            why = f"已匹配 {len(matched)} 个关键要求" + (
                f"：{'、'.join(matched[:5])}" if matched else "，需要进一步核对经历"
            )
            tips = []
            if matched:
                tips.append(f"把含有“{'、'.join(matched[:3])}”的项目经历前置")
            if missing:
                tips.append(f"JD 关注“{'、'.join(missing[:3])}”；仅在真实掌握时补充证据")
            tips.append("用一条真实的规模、效率或质量指标说明项目结果")
            results.append(
                {
                    **job,
                    "score": score,
                    "matched_keywords": matched[:12],
                    "missing_keywords": missing[:8],
                    "why_fit": why,
                    "resume_tips": tips[:4],
                    "recommendation": (
                        "优先投递" if score >= 80 else "补强后投递" if score >= 60 else "谨慎评估"
                    ),
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:limit]

    def send_outbox_message(self, message: dict[str, Any]) -> None:
        recipient = message.get("recipient")
        digest = message.get("payload")
        message_id = message.get("id")
        if (
            not isinstance(recipient, str)
            or not EMAIL_PATTERN.fullmatch(recipient)
            or not isinstance(digest, dict)
            or isinstance(message_id, bool)
            or not isinstance(message_id, int)
        ):
            raise PermanentWorkerError("邮件任务载荷无效")
        try:
            self._send_digest_email(
                recipient,
                digest,
                outbox_message_id=message_id,
                raise_errors=True,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PermanentWorkerError("邮件任务内容无效") from error

    def _send_digest_email(
        self,
        recipient: str,
        digest: dict[str, Any],
        *,
        outbox_message_id: int | None = None,
        raise_errors: bool = False,
    ) -> dict[str, Any]:
        if not self.capabilities()["email"]["enabled"]:
            if raise_errors:
                raise PermanentWorkerError("SMTP 未配置")
            return {"status": "skipped", "message": "SMTP 未配置"}
        items = digest["items"]
        cards = []
        for job in items:
            job_url = _safe_external_url(job.get("url"))
            cards.append(
                "<article style='border:1px solid #d9e2e8;padding:16px;margin:12px 0;border-radius:10px'>"
                f"<h3>{html.escape(job['title'])} · {job['score']}/100</h3>"
                f"<p>{html.escape(job['company'])} · {html.escape(job.get('location', ''))} · "
                f"{html.escape(job.get('salary', '面议'))}</p>"
                f"<p><strong>为什么适合：</strong>{html.escape(job['why_fit'])}</p>"
                f"<p><strong>简历动作：</strong>{html.escape('；'.join(job['resume_tips']))}</p>"
                + (
                    f"<p><a href='{html.escape(job_url, quote=True)}'>查看岗位</a></p>"
                    if job_url
                    else ""
                )
                + "</article>"
            )
        summary = digest["summary"]
        body = (
            "<h1>今日求职雷达</h1>"
            f"<p>共聚合 {summary['collected']} 个岗位，过滤后 {summary['after_filter']} 个，"
            f"推荐 {summary['shortlisted']} 个。</p>"
            + "".join(cards)
            + "<p style='color:#667085'>匹配分仅用于排序，投递前请核验岗位信息。</p>"
        )
        message = EmailMessage()
        message["Subject"] = f"今日 {summary['shortlisted']} 个值得投递的岗位｜智职引擎"
        message["From"] = self.settings.smtp_from or self.settings.smtp_user
        message["To"] = recipient
        if outbox_message_id is not None:
            sender_domain = message["From"].partition("@")[2] or "career-radar.local"
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", sender_domain):
                sender_domain = "career-radar.local"
            message["Message-ID"] = f"<career-radar-{outbox_message_id}@{sender_domain.lower()}>"
        message.set_content("请使用支持 HTML 的邮件客户端查看今日求职雷达。")
        message.add_alternative(body, subtype="html")
        delivery_started = False
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
                if self.settings.smtp_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.settings.smtp_user, self.settings.smtp_password)
                delivery_started = True
                refused = smtp.send_message(message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
            return {"status": "sent", "recipient": recipient}
        except smtplib.SMTPServerDisconnected as exc:
            LOGGER.warning("digest email disconnected during_delivery=%s", delivery_started)
            if raise_errors and delivery_started:
                raise DeliveryUnknownError(
                    "SMTP connection closed while delivery acknowledgement was pending"
                ) from exc
            if raise_errors:
                raise RuntimeError("SMTP connection failed") from exc
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPNotSupportedError,
            ssl.SSLCertVerificationError,
        ) as exc:
            LOGGER.warning("digest email permanently rejected type=%s", type(exc).__name__)
            if raise_errors:
                raise PermanentWorkerError("SMTP 配置或收件地址被拒绝") from exc
        except smtplib.SMTPResponseException as exc:
            LOGGER.warning(
                "digest email response failed code=%s type=%s",
                exc.smtp_code,
                type(exc).__name__,
            )
            if raise_errors:
                if int(exc.smtp_code) >= 500:
                    raise PermanentWorkerError("SMTP 服务永久拒绝邮件") from exc
                raise RuntimeError("SMTP 服务暂时拒绝邮件") from exc
        except (OSError, smtplib.SMTPException) as exc:
            LOGGER.warning("digest email failed type=%s", type(exc).__name__)
            if raise_errors:
                if delivery_started:
                    raise DeliveryUnknownError(
                        "SMTP transport failed after delivery began; acceptance is unknown"
                    ) from exc
                raise RuntimeError("SMTP 暂时不可用") from exc
        return {"status": "failed", "message": "邮件发送失败，请检查 SMTP 配置"}

    def _now_local_iso(self) -> str:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.settings.timezone)).isoformat(timespec="seconds")
        except Exception:
            return datetime.now().astimezone().isoformat(timespec="seconds")

    def crawl_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        keyword = _clean_text(payload.get("keyword"), limit=50, field="搜索关键词", required=True)
        city = _clean_text(payload.get("city"), limit=10, field="城市", required=True)
        if city not in CITIES:
            raise AppError(422, "unsupported_city", f"暂不支持城市：{city}")
        try:
            pages = int(payload.get("pages", 1))
        except (TypeError, ValueError) as exc:
            raise AppError(422, "invalid_pages", "页数必须是 1–5 的整数") from exc
        if not 1 <= pages <= 5:
            raise AppError(422, "invalid_pages", "页数必须在 1–5 之间")

        use_demo = (
            _require_bool(payload["use_demo"], field="演示数据开关")
            if "use_demo" in payload
            else False
        )
        if use_demo:
            if not self.settings.demo_enabled:
                raise AppError(403, "demo_disabled", "演示数据已关闭")
            jobs = [dict(job, location=city) for job in DEMO_JOBS]
            mode = "demo"
        else:
            if (
                importlib.util.find_spec("selenium") is None
                or importlib.util.find_spec("webdriver_manager") is None
            ):
                raise AppError(
                    503,
                    "crawler_unavailable",
                    "实时采集组件未安装。可先使用演示数据，或安装 crawler 可选依赖。",
                )
            try:
                from .crawler.crawl_51job import fetch_51job

                raw_jobs = fetch_51job(keyword=keyword, city=city, max_pages=pages, headless=True)
            except Exception as exc:
                LOGGER.exception("job crawler failed")
                raise AppError(
                    502,
                    "crawler_failed",
                    "招聘网站采集失败，请检查 Chrome、网络或站点验证状态",
                ) from exc
            jobs = [_normalise_job(job, index) for index, job in enumerate(raw_jobs, 1)]
            if not jobs:
                raise AppError(
                    502,
                    "crawler_empty",
                    "未采集到职位，站点可能要求验证；请稍后重试或导入岗位文本",
                )
            mode = "live"

        self.repository.set_state("latest_jobs", jobs)
        return {"items": jobs, "count": len(jobs), "mode": mode}

    def import_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = _clean_text(payload.get("job_text"), limit=150_000, field="岗位文本", required=True)
        jobs = _parse_job_text(text)
        if not jobs:
            raise AppError(422, "jobs_unparseable", "未能从文本中识别岗位")
        self.repository.set_state("latest_jobs", jobs)
        return {"items": jobs, "count": len(jobs), "mode": "imported"}

    def get_job_pool(self) -> dict[str, Any]:
        stored = self.repository.get_state("latest_jobs", [])
        jobs = (
            [_normalise_job(item, index) for index, item in enumerate(stored, 1)]
            if isinstance(stored, list)
            else []
        )
        return {"items": jobs, "count": len(jobs)}

    def analyse_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_name = "文本粘贴"
        if payload.get("file") is not None:
            text, source_name = _decode_resume_file(payload, self.settings.max_body_bytes)
        else:
            text = _clean_text(
                payload.get("text"),
                limit=MAX_RESUME_TEXT_CHARS,
                field="简历内容",
                required=True,
            )
            text = _validate_resume_text(text)
        if len(text) < 40:
            raise AppError(422, "resume_too_short", "简历内容过短，请至少提供 40 个字符")

        structured, score, summary = _analyse_resume(text)
        resume_id = uuid.uuid4().hex[:16]
        raw_for_storage = text if self.settings.store_raw_resume else ""
        evidence_text = _redact_contact_info(text)
        structured_for_storage = structured
        summary_for_storage = summary
        stored_source_name = source_name
        stored_name = structured["basic_info"]["name"]
        if not self.settings.store_raw_resume:
            structured_for_storage = _redact_nested_contact_info(structured)
            structured_for_storage["basic_info"]["email"] = ""
            structured_for_storage["basic_info"]["phone"] = ""
            summary_for_storage = _redact_contact_info(summary)
            stored_source_name = _redact_contact_info(source_name)
            stored_name = _redact_contact_info(stored_name)
        self.repository.save_resume(
            resume_id=resume_id,
            name=stored_name,
            source_name=stored_source_name,
            raw_text=raw_for_storage,
            evidence_text=evidence_text,
            summary=summary_for_storage,
            structured=structured_for_storage,
            score=score,
        )
        return {
            "id": resume_id,
            "name": structured["basic_info"]["name"],
            "source_name": source_name,
            "summary": summary,
            "structured": structured,
            "score": score,
            "mode": "local",
            "evidence_chunks": self.repository.count_resume_evidence(resume_id),
        }

    def match_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume_source = payload.get("resume_source", "latest")
        if resume_source == "latest":
            resume = self.repository.get_latest_resume()
            resume_text = self._resume_text(resume)
            if not resume_text:
                raise AppError(
                    409,
                    "resume_unavailable",
                    "暂无可用于匹配的简历，请先完成简历分析或粘贴简历",
                )
        else:
            resume_text = _clean_text(
                payload.get("resume_text"),
                limit=MAX_RESUME_TEXT_CHARS,
                field="简历内容",
                required=True,
            )
            resume_text = _validate_resume_text(resume_text)

        job_source = payload.get("job_source", "latest")
        if job_source == "latest":
            stored_jobs = self.repository.get_state("latest_jobs", [])
            if not stored_jobs:
                raise AppError(
                    409,
                    "jobs_unavailable",
                    "暂无职位数据，请先采集职位、使用演示数据或粘贴岗位描述",
                )
            jobs = [_normalise_job(item, index) for index, item in enumerate(stored_jobs, 1)]
        else:
            job_text = _clean_text(
                payload.get("job_text"), limit=100_000, field="岗位描述", required=True
            )
            jobs = _parse_job_text(job_text)

        resume_terms = _extract_keywords(resume_text)
        resume_skills = set(_flatten_skills(_extract_skills(resume_text)))
        results: list[dict[str, Any]] = []
        for job in jobs[:50]:
            job_text = f"{job['title']}\n{job['description']}"
            job_terms = _extract_keywords(job_text)
            job_skills = set(_flatten_skills(_extract_skills(job_text)))
            required = job_skills or {term for term in job_terms if len(term) <= 24}
            matched = sorted(required & (resume_skills | resume_terms), key=str.lower)
            missing = sorted(required - (resume_skills | resume_terms), key=str.lower)
            coverage = len(matched) / max(1, len(required))
            evidence_bonus = min(
                15,
                len(re.findall(r"项目|实习|负责|实现|优化|设计", resume_text)) * 2,
            )
            title_terms = _extract_keywords(job["title"])
            title_bonus = min(15, len(title_terms & resume_terms) * 5)
            score = min(98, round(30 + coverage * 40 + evidence_bonus + title_bonus))
            results.append(
                {
                    **job,
                    "score": score,
                    "matched_keywords": matched[:12],
                    "missing_keywords": missing[:8],
                    "recommendation": (
                        "优先投递" if score >= 80 else "补强后投递" if score >= 60 else "谨慎评估"
                    ),
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        top = results[:8]
        self.repository.set_state("latest_matches", top)
        return {
            "items": top,
            "count": len(top),
            "mode": "explainable_rules",
            "notice": "匹配分数用于排序参考，不代表招聘结果。",
        }

    def enhance_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        allow_ai = (
            _require_bool(payload["allow_ai"], field="AI 数据处理授权")
            if "allow_ai" in payload
            else False
        )
        source = payload.get("resume_source", "latest")
        if source == "latest":
            resume = self.repository.get_latest_resume()
            resume_text = self._resume_text(resume)
            if not resume_text:
                raise AppError(409, "resume_unavailable", "暂无简历，请先分析简历或选择粘贴")
        else:
            resume_text = _clean_text(
                payload.get("resume_text"),
                limit=MAX_RESUME_TEXT_CHARS,
                field="简历内容",
                required=True,
            )
            resume_text = _validate_resume_text(resume_text)
        jd = _clean_text(payload.get("jd"), limit=60_000, field="目标岗位 JD", required=True)
        if len(jd) < 20:
            raise AppError(422, "jd_too_short", "岗位 JD 过短，请提供职责与任职要求")
        template = _clean_text(payload.get("template", "standard"), limit=20, field="模板")
        if template not in TEMPLATES:
            raise AppError(422, "invalid_template", "不支持的简历模板")

        resume_skills = set(_flatten_skills(_extract_skills(resume_text)))
        jd_skills = set(_flatten_skills(_extract_skills(jd)))
        matched = sorted(resume_skills & jd_skills, key=str.lower)
        missing = sorted(jd_skills - resume_skills, key=str.lower)
        coverage = round(len(matched) / max(1, len(jd_skills)) * 100)

        latest_resume = self.repository.get_latest_resume() if source == "latest" else None
        evidence = (
            self.repository.retrieve_resume_evidence(latest_resume["id"], jd, limit=5)
            if latest_resume
            else []
        )
        ai_result = self._ai_enhance(resume_text, jd, template, evidence) if allow_ai else None
        if ai_result:
            optimized = ai_result
            mode = "ai"
            warning = ""
        else:
            structured, _, _ = _analyse_resume(resume_text)
            name = structured["basic_info"]["name"]
            target_match = re.search(r"(?:岗位(?:名称)?|职位)\s*[:：]\s*([^\n]{2,40})", jd)
            target = (
                target_match.group(1).strip() if target_match else jd.splitlines()[0].strip()[:40]
            )
            strengths = matched or list(resume_skills)[:8]
            heading = {
                "standard": "专业概览",
                "technical": "技术能力概览",
                "concise": "核心优势",
            }[template]
            optimized = (
                f"{name}\n求职目标：{target}\n\n"
                f"【{heading}】\n"
                + (
                    "、".join(strengths)
                    if strengths
                    else "请根据真实经历补充与目标岗位相关的核心能力"
                )
                + "\n\n【简历正文】\n"
                + _normalise_whitespace(resume_text)
            )
            mode = "local"
            warning = (
                "本次未授权外部 AI 处理，已使用本地规则生成。"
                if self.settings.ai_api_key and not allow_ai
                else "当前使用本地规则优化；配置 DeepSeek 后可生成更完整的定向版本。"
            )

        recommendations = []
        if missing:
            recommendations.append(
                "JD 提及但简历未体现：" + "、".join(missing[:8]) + "。仅补充真实掌握的能力。"
            )
        recommendations.extend(
            [
                "将最相关的项目放在前面，并用“行动 + 技术 + 可验证结果”描述。",
                "逐条核对量化数字，禁止为了匹配岗位而编造经历或指标。",
            ]
        )
        result = {
            "optimized_resume": optimized,
            "coverage": coverage,
            "matched_keywords": matched,
            "missing_keywords": missing,
            "recommendations": recommendations,
            "mode": mode,
            "warning": warning,
            "retrieved_evidence": [
                {"text": item["text"][:500], "relevance": item["score"]} for item in evidence
            ],
        }
        self.repository.set_state("latest_enhancement", result)
        return result

    def _ai_enhance(
        self,
        resume_text: str,
        jd: str,
        template: str,
        evidence: list[dict[str, Any]],
    ) -> str | None:
        if not self.settings.ai_api_key or importlib.util.find_spec("openai") is None:
            return None
        system = (
            "你是严谨的中文简历编辑。只能重组和改写用户已有事实，禁止编造技能、"
            "经历、学校、公司或数字。输出完整简历，不输出思考过程；信息不足时明确标注"
            "“待补充”，并优先适配岗位关键词与 ATS 可读性。"
        )
        evidence_text = "\n\n".join(
            f"[证据片段 {index + 1}]\n{item['text']}" for index, item in enumerate(evidence)
        )
        prompt = (
            f"模板风格：{template}\n\n【目标岗位 JD】\n{jd}\n\n"
            f"【RAG 检索到的高相关简历证据】\n{evidence_text or '未检索到'}\n\n"
            f"【原始简历（用于完整性核验）】\n{resume_text}\n\n"
            "请优先使用检索证据生成定向中文简历，且不得添加原文不存在的事实。"
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                from openai import OpenAI  # type: ignore

                client = OpenAI(
                    api_key=self.settings.ai_api_key,
                    base_url=self.settings.ai_base_url,
                    timeout=self.settings.ai_timeout_seconds,
                )
                response = client.chat.completions.create(
                    model=self.settings.ai_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=4000,
                )
                content = response.choices[0].message.content
                return content.strip() if content else None
            except Exception as exc:  # External provider failures must degrade safely.
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
        LOGGER.warning("AI enhancement degraded to local mode: %s", type(last_error).__name__)
        return None

    def list_applications(
        self, status: str = "", query: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        if status and status not in STATUS_TO_STORAGE:
            raise AppError(422, "invalid_status", "投递状态无效")
        query = _clean_text(query, limit=80, field="搜索词")
        limit = max(1, min(200, limit))
        offset = max(0, offset)
        rows = self.repository.list_applications(
            STATUS_TO_STORAGE.get(status), query, limit, offset
        )
        items = [self._public_application(row) for row in rows]
        return {
            "items": items,
            "count": len(items),
            "statistics": self._public_stats(self.repository.application_statistics()),
        }

    def add_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = payload.get("status", "submitted")
        if status not in STATUS_TO_STORAGE:
            raise AppError(422, "invalid_status", "投递状态无效")
        applied_at = _clean_text(payload.get("applied_at"), limit=30, field="投递日期")
        if applied_at:
            try:
                date.fromisoformat(applied_at[:10])
            except ValueError as exc:
                raise AppError(422, "invalid_date", "投递日期格式应为 YYYY-MM-DD") from exc
        values = {
            "company_name": _clean_text(
                payload.get("company_name"), limit=100, field="公司名称", required=True
            ),
            "job_title": _clean_text(
                payload.get("job_title"), limit=100, field="岗位名称", required=True
            ),
            "job_description": _clean_text(
                payload.get("job_description"), limit=20_000, field="岗位描述"
            ),
            "salary_range": _clean_text(payload.get("salary_range"), limit=60, field="薪资"),
            "location": _clean_text(payload.get("location"), limit=60, field="地点"),
            "resume_version": _clean_text(
                payload.get("resume_version"), limit=100, field="简历版本"
            ),
            "notes": _clean_text(payload.get("notes"), limit=2_000, field="备注"),
            "status": STATUS_TO_STORAGE[status],
            "applied_at": applied_at,
        }
        return self._public_application(self.repository.add_application(values))

    def update_application(self, application_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, str] = {}
        if "status" in payload:
            status = payload["status"]
            if status not in STATUS_TO_STORAGE:
                raise AppError(422, "invalid_status", "投递状态无效")
            fields["status"] = STATUS_TO_STORAGE[status]
        field_limits = {
            "notes": 2_000,
            "salary_range": 60,
            "location": 60,
            "job_description": 20_000,
        }
        for field, limit in field_limits.items():
            if field in payload:
                fields[field] = _clean_text(payload[field], limit=limit, field=field)
        if not fields:
            raise AppError(422, "empty_update", "没有可更新的字段")
        row = self.repository.update_application(application_id, fields)
        if row is None:
            raise AppError(404, "application_not_found", "未找到该投递记录")
        return self._public_application(row)

    def delete_application(self, application_id: int) -> dict[str, Any]:
        if not self.repository.delete_application(application_id):
            raise AppError(404, "application_not_found", "未找到该投递记录")
        return {"id": application_id, "deleted": True}

    @staticmethod
    def _public_application(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "company_name": row["company_name"],
            "job_title": row["job_title"],
            "job_description": row.get("job_description", ""),
            "salary_range": row.get("salary_range", ""),
            "location": row.get("location", ""),
            "resume_version": row.get("resume_version", ""),
            "status": STORAGE_TO_STATUS.get(row.get("status", ""), "submitted"),
            "notes": row.get("notes", ""),
            "applied_at": row.get("applied_at", ""),
            "updated_at": row.get("updated_at", ""),
        }

    @staticmethod
    def _public_stats(stats: dict[str, Any]) -> dict[str, Any]:
        output = {key: 0 for key in STATUS_TO_STORAGE}
        for storage_status, count in stats.get("by_status", {}).items():
            output[STORAGE_TO_STATUS.get(storage_status, "submitted")] += int(count)
        return {
            "total": int(stats.get("total", 0)),
            "recent_week": int(stats.get("recent_week", 0)),
            "by_status": output,
        }
