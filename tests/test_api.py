"""注册表与纯函数的单元测试（无网络）。"""

from __future__ import annotations

import pytest

from arcbench_mcp.api import (
    DOWNLOADS,
    RESOURCES,
    ArcBenchClient,
    ArcBenchError,
    build_url,
    filename_from_disposition,
    load_env_file,
    parse_cookie,
)
from arcbench_mcp.server import render_compare_markdown


# ---------------------------------------------------------------------------
# build_url：路径占位符必填、查询占位符可选、字面量恒定
# ---------------------------------------------------------------------------
def test_build_url_missing_path_param_raises():
    with pytest.raises(ArcBenchError, match="缺少必填参数"):
        build_url("/runs/{id}", {})


def test_build_url_optional_query_dropped_when_absent():
    assert build_url("/runs?requirement_id={requirement_id}&active_only={active_only}", {}) == "/runs"


def test_build_url_literal_query_always_kept():
    url = build_url("/competitions/leaderboard?track=all&competition_id={competition_id}", {})
    assert url == "/competitions/leaderboard?track=all"


def test_build_url_full_substitution_and_quoting():
    url = build_url(
        "/runs/{id}/source?file_path={file_path}&kind={kind}",
        {"id": "abc123", "file_path": "backend/src/a b.py", "kind": "file"},
    )
    assert url == "/runs/abc123/source?file_path=backend%2Fsrc%2Fa%20b.py&kind=file"


def test_build_url_empty_param_treated_as_absent():
    url = build_url("/requirements?catalog={catalog}", {"catalog": ""})
    assert url == "/requirements"


# ---------------------------------------------------------------------------
# 注册表完整性：唯一事实源自身的不变量
# ---------------------------------------------------------------------------
def test_registry_unsafe_exactly_write_ops():
    unsafe = {name for name, r in RESOURCES.items() if not r.safe}
    assert unsafe == {"submit", "cancel", "rerun"}


def test_registry_safe_resources_are_get():
    assert all(r.method == "GET" for n, r in RESOURCES.items() if r.safe)


def test_registry_query_placeholders_match_param_names():
    """查询段 key={name} 的键名与参数名一致，避免两套口径。"""
    import re

    for r in RESOURCES.values():
        query = r.path.partition("?")[2]
        for piece in query.split("&"):
            key, eq, value = piece.partition("=")
            if eq and value.startswith("{") and value.endswith("}"):
                assert key == value[1:-1], f"{r.path}: {key} != {value}"


def test_downloads_all_single_id_templates():
    for tpl in DOWNLOADS.values():
        assert "{id}" in tpl


# ---------------------------------------------------------------------------
# cookie / 文件名 / .env
# ---------------------------------------------------------------------------
def test_parse_cookie_raw_header_passthrough():
    assert parse_cookie("a=1; b=2") == "a=1; b=2"


def test_parse_cookie_json_export():
    raw = '[{"name":"arcbench_session","value":"s1"},{"name":"arc_api_sticky","value":"s2"}]'
    assert parse_cookie(raw) == "arcbench_session=s1; arc_api_sticky=s2"


def test_parse_cookie_invalid_json_raises():
    with pytest.raises(Exception):
        parse_cookie("[{broken")


def test_filename_from_disposition():
    assert filename_from_disposition('attachment; filename="mini6-max-9382776.zip"', "x") == "mini6-max-9382776.zip"
    assert filename_from_disposition("attachment; filename=plain.zip", "x") == "plain.zip"
    assert filename_from_disposition(None, "fb.zip") == "fb.zip"
    assert filename_from_disposition("attachment", "fb.zip") == "fb.zip"


def test_load_env_file_quotes_and_comments(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释\nARCBENCH_COOKIE='a=1; b=2'\nEMPTY=\nBADLINE\n", encoding="utf-8")
    assert load_env_file(env_file) == {"ARCBENCH_COOKIE": "a=1; b=2"}


# ---------------------------------------------------------------------------
# client：无网络分支
# ---------------------------------------------------------------------------
def test_client_unknown_resource_raises():
    client = ArcBenchClient(cookie="a=1")
    with pytest.raises(ArcBenchError, match="未知资源"):
        client.call("nope")


def test_client_unsafe_resource_blocked_on_call():
    client = ArcBenchClient(cookie="a=1")
    with pytest.raises(ArcBenchError, match="写操作"):
        client.call("cancel", {"id": "x"})


def test_client_unknown_download_kind_raises():
    client = ArcBenchClient(cookie="a=1")
    with pytest.raises(ArcBenchError, match="未知下载类型"):
        client.download("nope", "x")


def test_client_requires_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCBENCH_COOKIE", "")
    monkeypatch.delenv("ARCBENCH_COOKIE", raising=False)
    monkeypatch.setattr("arcbench_mcp.api.PROJECT_ROOT", tmp_path)  # 隔离项目 .env 回退
    with pytest.raises(ArcBenchError, match="cookie"):
        ArcBenchClient(cookie=None)


# ---------------------------------------------------------------------------
# compare 渲染
# ---------------------------------------------------------------------------
SUB_A = {
    "id": "aaaa",
    "display_name": "v1",
    "created_at": "2026-11-09T16:15:31",
    "score": 10.0,
    "pass_rate": 28.6,
    "total_token_count": 1000000,
    "token_cost_usd": 2.0,
    "token_cost_currency": "CNY",
    "total_run_duration_seconds": 2400,
    "task_scores": [
        {"task_id": "t1", "task_title": "Alpha", "score": 10.0, "test_pass_rate": 28.6},
        {"task_id": "t2", "task_title": "Beta", "score": 0.0, "test_pass_rate": None},
    ],
}
SUB_B = {
    "id": "bbbb",
    "display_name": "v2",
    "created_at": "2026-11-10T10:00:00",
    "score": 20.29,
    "pass_rate": 57.7,
    "total_token_count": 17746000,
    "token_cost_usd": 2.911,
    "token_cost_currency": "CNY",
    "total_run_duration_seconds": 2461,
    "task_scores": [
        {"task_id": "t1", "task_title": "Alpha", "score": 20.29, "test_pass_rate": 57.7},
        {"task_id": "t2", "task_title": "Beta", "score": None, "test_pass_rate": None},
    ],
}


def test_render_compare_markdown():
    md = render_compare_markdown(SUB_A, SUB_B)
    assert "v1（2026-11-09T16:15） vs v2" in md
    assert "| 总分 | 10 | 20.29 | +10.29 |" in md
    assert "| Alpha | 10 | 20.29 | +10.29 | 28.6 | 57.7 |" in md
    assert "| Beta | 0 | — | — | — | — |" in md


def test_render_compare_markdown_sorts_by_delta():
    sub_a = {**SUB_A, "task_scores": [
        {"task_id": "t1", "task_title": "Small", "score": 1.0, "test_pass_rate": 5.0},
        {"task_id": "t2", "task_title": "Big", "score": 0.0, "test_pass_rate": 0.0},
    ]}
    sub_b = {**SUB_B, "task_scores": [
        {"task_id": "t1", "task_title": "Small", "score": 2.0, "test_pass_rate": 10.0},
        {"task_id": "t2", "task_title": "Big", "score": 50.0, "test_pass_rate": 90.0},
    ]}
    md = render_compare_markdown(sub_a, sub_b)
    assert md.index("Big") < md.index("Small")
