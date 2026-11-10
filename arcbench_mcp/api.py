"""ARC-Bench API：注册表 + HTTP 客户端。

两张表是本包唯一的事实源，加能力 = 加一行，MCP 工具、CLI、工具文档随之更新：
- RESOURCES  JSON 端点（safe=True 的才暴露给 MCP；写操作仅 CLI 调用）
- DOWNLOADS  zip 二进制端点

路径模板约定：``{name}`` 为参数占位符；``?`` 之后的查询段里，字面量键值对
（如 ``track=all``）恒定发送，``key={name}`` 形式的对仅在传入该参数时发送。
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = "https://arc-bench.com/api"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOWNLOAD_TIMEOUT = 300.0


class ArcBenchError(Exception):
    """参数或配置错误。"""


class ArcBenchApiError(ArcBenchError):
    """API 返回非 2xx。"""

    def __init__(self, status: int, detail: str):
        hint = "；登录态可能已过期，请重新从浏览器复制 cookie" if status == 401 else ""
        super().__init__(f"HTTP {status}: {detail}{hint}")
        self.status = status


@dataclass(frozen=True)
class Resource:
    """一个 JSON 端点：方法 + 路径模板 + 是否安全（可暴露给 MCP）。"""

    method: str
    path: str
    safe: bool = True
    note: str = ""


# ---------------------------------------------------------------------------
# 注册表（唯一事实源）
# ---------------------------------------------------------------------------
RESOURCES: dict[str, Resource] = {
    # 账号与比赛现状
    "me":              Resource("GET", "/auth/me", note="当前登录用户"),
    "competitions":    Resource("GET", "/competitions", note="比赛列表"),
    "competition":     Resource("GET", "/competitions/{id}", note="比赛详情，含 tasks 数组"),
    "tasks":           Resource("GET", "/requirements?catalog={catalog}", note="任务库，catalog 常用 competition"),
    "task":            Resource("GET", "/requirements/{id}?catalog={catalog}", note="任务详情"),
    "task_tests":      Resource("GET", "/requirements/{id}/tests?catalog={catalog}", note="任务测试清单"),
    # 分数
    "submissions":     Resource("GET", "/competitions/{id}/submissions", note="历史提交及每任务分数（提交详情即从此列表取）"),
    "runs":            Resource("GET", "/runs?requirement_id={requirement_id}&submission_id={submission_id}&active_only={active_only}", note="run 列表，参数均可选"),
    "run":             Resource("GET", "/runs/{id}", note="run 详情：状态/分数/阶段"),
    "leaderboard":     Resource("GET", "/competitions/leaderboard?track=all&competition_id={competition_id}&task_id={task_id}&funding_source={funding_source}", note="公开榜单，参数均可选"),
    # 复盘
    "workspace_files": Resource("GET", "/runs/{id}/workspace/files", note="工作区文件树"),
    "file":            Resource("GET", "/runs/{id}/source?file_path={file_path}&kind={kind}", note="工作区单文件源码"),
    "logs":            Resource("GET", "/runs/{id}/logs?log_offset={log_offset}&after_event_id={after_event_id}", note="运行日志与事件，参数均可选"),
    # 写操作：MCP 不可见，仅 CLI（人工确认）
    "submit":          Resource("POST", "/submissions", safe=False, note="提交 zip 并开跑（三步链）"),
    "cancel":          Resource("POST", "/runs/{id}/cancel", safe=False, note="取消 run"),
    "rerun":           Resource("POST", "/runs/{id}/rerun", safe=False, note="重跑 run"),
}

DOWNLOADS: dict[str, str] = {
    "project_zip":             "/runs/{id}/workspace/template-bundle",
    "archive":                 "/submissions/{id}/archive",
    "competition_requirements": "/competitions/{id}/requirements-download",
    "initial_projects":        "/competitions/{id}/initial-projects-download",
}


# ---------------------------------------------------------------------------
# 模板渲染与解析工具
# ---------------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_FILENAME = re.compile(r'filename="?([^"]+)"?')


def build_url(template: str, params: dict[str, Any]) -> str:
    """模板 → 相对 URL。

    路径段占位符必填；查询段 ``key={name}`` 在参数缺失（或为 None/""）时整对丢弃。
    """
    path_tpl, _, query_tpl = template.partition("?")
    need = set(_PLACEHOLDER.findall(path_tpl))
    missing = need - {k for k, v in params.items() if v not in (None, "")}
    if missing:
        raise ArcBenchError(f"缺少必填参数: {', '.join(sorted(missing))}")

    path = _PLACEHOLDER.sub(lambda m: quote(str(params[m.group(1)]), safe=""), path_tpl)
    if not query_tpl:
        return path

    pairs = []
    for piece in query_tpl.split("&"):
        key, eq, value = piece.partition("=")
        m = _PLACEHOLDER.fullmatch(value)
        if m:
            v = params.get(m.group(1))
            if v not in (None, ""):
                pairs.append(f"{key}={quote(str(v), safe='')}")
        elif eq:
            pairs.append(piece)  # 字面量，如 track=all
    return f"{path}?{'&'.join(pairs)}" if pairs else path


def parse_cookie(raw: str) -> str:
    """裸 Cookie 头原样返回；Cookie 管理器导出的 JSON 数组转成头格式。"""
    raw = raw.strip()
    if not raw.startswith("["):
        return raw
    items = json.loads(raw)
    pairs = [f"{item['name']}={item['value']}" for item in items if item.get("name")]
    if not pairs:
        raise ArcBenchError("cookie JSON 里没有可用的 name/value")
    return "; ".join(pairs)


def filename_from_disposition(disposition: str | None, fallback: str) -> str:
    if disposition:
        m = _FILENAME.search(disposition)
        if m:
            return m.group(1)
    return fallback


def load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE 行，去掉包裹引号，# 注释跳过。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if key and value:
            values[key.strip()] = value
    return values


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
class ArcBenchClient:
    """只做四件事：cookie 注入、请求、错误包装、zip 下载。"""

    def __init__(
        self,
        cookie: str | None = None,
        *,
        base_url: str | None = None,
        download_dir: str | Path | None = None,
        timeout: float = 60.0,
    ):
        env = load_env_file(PROJECT_ROOT / ".env")
        raw_cookie = (
            cookie
            or os.environ.get("ARCBENCH_COOKIE")
            or env.get("ARCBENCH_COOKIE")
        )
        if not raw_cookie:
            raise ArcBenchError(
                "未配置登录 cookie：请设置 ARCBENCH_COOKIE 环境变量，"
                "或在 ARCBENCH_COOKIE_FILE / 项目 .env 里提供"
            )
        self.cookie = parse_cookie(raw_cookie)
        self.base_url = (
            base_url or os.environ.get("ARCBENCH_BASE_URL") or env.get("ARCBENCH_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.download_dir = Path(
            download_dir
            or os.environ.get("ARCBENCH_DOWNLOAD_DIR")
            or env.get("ARCBENCH_DOWNLOAD_DIR")
            or PROJECT_ROOT / "downloads"
        )
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Cookie": self.cookie, "Accept": "application/json"},
            timeout=timeout,
        )

    # -- 查表调用 -----------------------------------------------------------
    def call(self, resource: str, params: dict[str, Any] | None = None) -> Any:
        entry = RESOURCES.get(resource)
        if entry is None:
            known = ", ".join(RESOURCES)
            raise ArcBenchError(f"未知资源 {resource!r}；可用: {known}")
        if not entry.safe:
            raise ArcBenchError(f"{resource!r} 是写操作，仅在 CLI 提供（避免会话内误触发计费）")
        return self._request(entry.method, build_url(entry.path, params or {}))

    def find_submissions(
        self, submission_ids: list[str], competition_id: str | None = None
    ) -> dict[str, dict]:
        """按 id 取提交详情（站点无单条 GET，详情即列表项）。

        competition_id 缺省时遍历比赛定位，找到全部目标即停。
        """
        remaining = list(dict.fromkeys(submission_ids))
        found: dict[str, dict] = {}

        def scan(competition: str) -> None:
            try:
                items = self.call("submissions", {"id": competition})
            except ArcBenchApiError as e:
                if e.status in (403, 404):
                    return  # 无权访问的比赛（如需队伍确认）直接跳过
                raise
            for item in items:
                if item.get("id") in remaining:
                    found[item["id"]] = item
                    remaining.remove(item["id"])

        if competition_id:
            scan(competition_id)
        else:
            for competition in self.call("competitions"):
                if not remaining:
                    break
                scan(competition["id"])
        if remaining:
            raise ArcBenchError(f"未找到提交: {', '.join(remaining)}")
        return found

    def download(self, kind: str, id: str, out_dir: str | Path | None = None) -> dict:
        template = DOWNLOADS.get(kind)
        if template is None:
            known = ", ".join(DOWNLOADS)
            raise ArcBenchError(f"未知下载类型 {kind!r}；可用: {known}")
        url = build_url(template, {"id": id})
        response = self._http.get(url, timeout=_DOWNLOAD_TIMEOUT)
        if response.status_code >= 400:
            raise self._api_error(response)
        if not response.content.startswith(b"PK"):
            raise ArcBenchError(f"响应不是 zip（前 4 字节: {response.content[:4]!r}）")

        fallback = f"{id}-{kind}.zip"
        filename = filename_from_disposition(response.headers.get("content-disposition"), fallback)
        target_dir = Path(out_dir) if out_dir else self.download_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        target.write_bytes(response.content)
        with zipfile.ZipFile(target) as zf:
            entries = zf.namelist()
        return {"path": str(target), "size": len(response.content), "entries": entries}

    # -- 写操作（仅 CLI） ----------------------------------------------------
    def submit(
        self,
        zip_path: str | Path,
        requirement_id: str,
        *,
        competition_id: str | None = None,
        display_name: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        credential_mode: str = "self_funded",
        runtime: str = "python",
        catalog: str = "competition",
    ) -> dict:
        """三步链：创建提交（multipart）→ 创建 run → 启动。**计费操作**。"""
        zip_file = Path(zip_path)
        if not zip_file.is_file():
            raise ArcBenchError(f"zip 不存在: {zip_file}")
        data: dict[str, str] = {
            "runtime": runtime,
            "catalog": catalog,
            "agent_source": "upload",
            "credential_mode": credential_mode,
            "requirement_id": requirement_id,
        }
        for key, value in (
            ("competition_id", competition_id),
            ("display_name", display_name),
            ("model", model),
            ("base_url", base_url),
            ("api_key", api_key),
        ):
            if value:
                data[key] = value

        created = self._request("POST", "/submissions", data=data, files={"file": (zip_file.name, zip_file.read_bytes(), "application/zip")})
        submission_id = _unwrap_id(created, "submission")
        run = self._request("POST", "/runs", data={"submission_id": submission_id, "requirement_id": requirement_id})
        run_id = _unwrap_id(run, "run")
        started = self._request("POST", f"/runs/{run_id}/start")
        return {"submission_id": submission_id, "run_id": run_id, "start": started}

    def cancel(self, run_id: str) -> Any:
        return self._request("POST", build_url("/runs/{id}/cancel", {"id": run_id}))

    def rerun(self, run_id: str, requirement_id: str | None = None) -> Any:
        data = {"requirement_id": requirement_id} if requirement_id else None
        return self._request("POST", build_url("/runs/{id}/rerun", {"id": run_id}), data=data)

    # -- 内部 ----------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise self._api_error(response)
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            raise ArcBenchError(f"响应不是 JSON: {response.text[:200]!r}") from None

    @staticmethod
    def _api_error(response: httpx.Response) -> ArcBenchApiError:
        try:
            detail = response.json().get("detail") or response.text[:200]
        except json.JSONDecodeError:
            detail = response.text[:200]
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)[:200]
        return ArcBenchApiError(response.status_code, detail)


def _unwrap_id(payload: Any, key: str) -> str:
    """兼容 {key: {id: ..}} 与裸 {id: ..} 两种返回形状。"""
    node = payload.get(key) if isinstance(payload, dict) else None
    node = node if isinstance(node, dict) else payload
    rid = node.get("id") if isinstance(node, dict) else None
    if not rid:
        raise ArcBenchError(f"响应中找不到 {key}.id: {json.dumps(payload, ensure_ascii=False)[:200]}")
    return str(rid)
