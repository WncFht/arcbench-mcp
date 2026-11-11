"""MCP server：查询与提交工具，共用 api.py 客户端。

运行：``python -m arcbench_mcp.server``（stdio transport）。
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .api import DOWNLOADS, RESOURCES, ArcBenchClient, ArcBenchError

mcp = FastMCP(
    "arcbench",
    instructions=(
        "ARC-Bench（arc-bench.com）访问：查比赛/提交分数、下载提交 zip、"
        "浏览 run 工作区与日志、对比两次提交的每任务得分。arc_submit 可在用户明确要求上传并运行时创建提交、run 并启动；取消仍通过 CLI。"
    ),
)


def _client() -> ArcBenchClient:
    return ArcBenchClient()


_RESOURCES_DOC = "\n".join(
    f"  - {name} — {r.path}" + (f"（{r.note}）" if r.note else "")
    for name, r in RESOURCES.items()
    if r.safe
)
_DOWNLOADS_DOC = "\n".join(f"  - {kind} — {tpl}" for kind, tpl in DOWNLOADS.items())

# 注意：f-string 作为函数体首行不会生成 __doc__，故用 description= 显式传入
_ARC_API_DOC = f"""查询 ARC-Bench 只读资源，返回站点原始 JSON。

resource 取值（params 为其参数字典；路径参数必填，查询参数可选）：
{_RESOURCES_DOC}

示例：
- 历史提交及每任务分数：arc_api(resource="submissions", params={{"id": "arc-bench-web"}})
- 单次 run 详情：arc_api(resource="run", params={{"id": "f08fa8bc9e5e"}})
- 榜单：arc_api(resource="leaderboard", params={{"competition_id": "arc-bench-web"}})
- 工作区单文件：arc_api(resource="file", params={{"id": "…", "file_path": "package.json"}})"""

_ARC_DOWNLOAD_DOC = f"""下载 ARC-Bench 的 zip 到本地，返回路径与 zip 内文件清单（不解压、不执行）。

kind 取值：
{_DOWNLOADS_DOC}

id 为对应资源的标识（run id / submission id / competition id）。
out_dir 缺省时用 ARCBENCH_DOWNLOAD_DIR 或 <项目>/downloads。"""


@mcp.tool(description=_ARC_API_DOC)
def arc_api(resource: str, params: dict[str, Any] | None = None) -> Any:
    return _client().call(resource, params or {})


@mcp.tool(description=_ARC_DOWNLOAD_DOC)
def arc_download(kind: str, id: str, out_dir: str | None = None) -> dict:
    return _client().download(kind, id, out_dir)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def arc_submit(
    zip_path: str,
    requirement_id: str,
    api_key: str,
    base_url: str = "https://api.arc-bench.com/v1",
    competition_id: str | None = None,
    display_name: str | None = None,
    model: str | None = None,
) -> dict:
    """上传本地 agent ZIP、创建 run 并启动（计费写操作）。

    仅在用户明确要求提交/启动时调用。Python ZIP 根需 main.py 和 requirements.txt。
    先用 arc_api 查询比赛和任务 ID。api_key 由用户提供，不写入源码或返回值。
    不自动重试写请求；如中途失败，使用错误中的 submission_id/run_id 查询并恢复，
    勿重新上传以免重复创建运行。默认模型由 agent 代码选择；model 可省略。
    """
    return _client().submit(
        zip_path, requirement_id, api_key=api_key, base_url=base_url,
        competition_id=competition_id, display_name=display_name, model=model,
    )


@mcp.tool()
def arc_compare(submission_id_a: str, submission_id_b: str, competition_id: str | None = None) -> str:
    """对比两次提交的每任务得分，返回 markdown 报告（按分差从大到小排序）。

    提交 id 形如 "edfea39b7f92"，可先用 arc_api(resource="submissions",
    params={"id": "<比赛id>"}) 查到历史提交列表。competition_id 可选，缺省时自动遍历比赛定位。
    """
    client = _client()
    found = client.find_submissions([submission_id_a, submission_id_b], competition_id)
    return render_compare_markdown(found[submission_id_a], found[submission_id_b])


def render_compare_markdown(a: dict, b: dict) -> str:
    """两次提交 JSON → markdown 对比报告。纯函数，便于单测。"""

    def label(s: dict) -> str:
        return f"{s.get('display_name') or s.get('id')}（{s.get('created_at', '')[:16]}）"

    def totals(s: dict) -> dict:
        return {
            "总分": s.get("score"),
            "通过率%": s.get("pass_rate"),
            "Tokens": s.get("total_token_count"),
            f"费用({s.get('token_cost_currency') or 'CNY'})": s.get("token_cost_usd"),
            "运行(s)": s.get("total_run_duration_seconds"),
        }

    lines = [f"# 提交对比：{label(a)} vs {label(b)}", ""]

    ta, tb = totals(a), totals(b)
    lines += ["| 指标 | A | B | Δ |", "|---|---|---|---|"]
    for key in ta:
        va, vb = ta[key], tb[key]
        delta = _fmt_delta(va, vb)
        lines.append(f"| {key} | {_fmt(va)} | {_fmt(vb)} | {delta} |")
    lines.append("")

    by_task: dict[str, dict[str, dict | None]] = {}
    for s, tag in ((a, "a"), (b, "b")):
        for item in s.get("task_scores") or []:
            by_task.setdefault(item.get("task_id") or "?", {})[tag] = item

    def sort_key(entry: tuple[str, dict[str, dict | None]]) -> float:
        sa = _num((entry[1].get("a") or {}).get("score"))
        sb = _num((entry[1].get("b") or {}).get("score"))
        return -abs(sb - sa)

    lines += ["| 任务 | 得分 A | 得分 B | Δ | 通过率 A | 通过率 B |", "|---|---|---|---|---|---|"]
    for task_id, pair in sorted(by_task.items(), key=sort_key):
        item_a, item_b = pair.get("a"), pair.get("b")
        title = (item_a or item_b or {}).get("task_title") or task_id
        sa = (item_a or {}).get("score")
        sb = (item_b or {}).get("score")
        ra = (item_a or {}).get("test_pass_rate")
        rb = (item_b or {}).get("test_pass_rate")
        lines.append(
            f"| {title} | {_fmt(sa)} | {_fmt(sb)} | {_fmt_delta(sa, sb)} | {_fmt(ra)} | {_fmt(rb)} |"
        )
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _num(value: Any) -> float:
    return value if isinstance(value, (int, float)) else 0.0


def _fmt_delta(va: Any, vb: Any) -> str:
    if va is None or vb is None:
        return "—"
    delta = _num(vb) - _num(va)
    sign = "+" if delta > 0 else ""
    return f"{sign}{_fmt(delta)}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
