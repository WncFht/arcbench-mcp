"""CLI：与 MCP 共用 api.py 的注册表。写操作仅在此暴露，人工确认。

用法：
  arcbench get <resource> [k=v ...]        # 查询 safe 资源，JSON 输出
  arcbench download <kind> <id> [-o DIR]   # 下载 zip
  arcbench compare <idA> <idB>             # 两次提交 markdown 对比
  arcbench submit <zip> --requirement ID [--confirm]   # 计费操作，默认 dry-run
  arcbench cancel <run_id>
  arcbench rerun <run_id> [--requirement ID]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .api import DOWNLOADS, RESOURCES, ArcBenchClient, ArcBenchError
from .server import render_compare_markdown


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ArcBenchError(f"参数应为 k=v 形式，收到: {pair}")
        params[key] = value
    return params


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _cmd_get(client: ArcBenchClient, args: argparse.Namespace) -> None:
    entry = RESOURCES.get(args.resource)
    if entry is None:
        raise ArcBenchError(f"未知资源 {args.resource!r}；可用: {', '.join(RESOURCES)}")
    if not entry.safe:
        raise ArcBenchError(f"{args.resource!r} 是写操作，请用对应子命令（submit/cancel/rerun）")
    _print_json(client.call(args.resource, _parse_params(args.params)))


def _cmd_download(client: ArcBenchClient, args: argparse.Namespace) -> None:
    result = client.download(args.kind, args.id, args.out_dir)
    print(f"已下载: {result['path']}（{result['size']} 字节，{len(result['entries'])} 个条目）")
    for name in result["entries"]:
        print(f"  {name}")


def _cmd_compare(client: ArcBenchClient, args: argparse.Namespace) -> None:
    found = client.find_submissions([args.a, args.b], args.competition)
    print(render_compare_markdown(found[args.a], found[args.b]))


def _cmd_submit(client: ArcBenchClient, args: argparse.Namespace) -> None:
    zip_path = Path(args.zip_path)
    if not zip_path.is_file():
        raise ArcBenchError(f"zip 不存在: {zip_path}")
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL")

    plan = {
        "zip": str(zip_path),
        "requirement_id": args.requirement,
        "competition_id": args.competition,
        "display_name": args.display_name,
        "model": args.model,
        "base_url": base_url,
        "api_key": f"{api_key[:6]}…" if api_key else None,
        "credential_mode": "self_funded",
        "runtime": "python",
        "catalog": "competition",
    }
    if not args.confirm:
        print("dry-run —— 将发送以下内容（加 --confirm 真正提交，计费操作）：")
        _print_json(plan)
        return
    if not api_key or not base_url:
        raise ArcBenchError("计费需要 OPENAI_API_KEY / OPENAI_BASE_URL（环境变量或参数）")

    result = client.submit(
        zip_path,
        args.requirement,
        competition_id=args.competition,
        display_name=args.display_name,
        model=args.model,
        base_url=base_url,
        api_key=api_key,
    )
    print("已提交并启动：")
    _print_json(result)


def _cmd_cancel(client: ArcBenchClient, args: argparse.Namespace) -> None:
    _print_json(client.cancel(args.run_id))


def _cmd_rerun(client: ArcBenchClient, args: argparse.Namespace) -> None:
    _print_json(client.rerun(args.run_id, args.requirement))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcbench",
        description="ARC-Bench 命令行：查分数、下载提交 zip、提交实验（注册表驱动）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    get = sub.add_parser("get", help="查询 safe 资源")
    get.add_argument("resource", help=" | ".join(n for n, r in RESOURCES.items() if r.safe))
    get.add_argument("params", nargs="*", help="k=v 形式的参数")

    dl = sub.add_parser("download", help="下载 zip")
    dl.add_argument("kind", help=" | ".join(DOWNLOADS))
    dl.add_argument("id")
    dl.add_argument("-o", "--out-dir")

    cmp_ = sub.add_parser("compare", help="两次提交对比")
    cmp_.add_argument("a")
    cmp_.add_argument("b")
    cmp_.add_argument("--competition", default=None, help="比赛 id，缺省自动定位")

    sb = sub.add_parser("submit", help="提交 zip 并开跑（计费，默认 dry-run）")
    sb.add_argument("zip_path")
    sb.add_argument("--requirement", required=True, help="任务 id，如 arc-bench-web--12306")
    sb.add_argument("--competition", default=None)
    sb.add_argument("--model", default=None)
    sb.add_argument("--display-name", default=None)
    sb.add_argument("--base-url", default=None)
    sb.add_argument("--api-key", default=None)
    sb.add_argument("--confirm", action="store_true", help="真正提交（计费）")

    cancel = sub.add_parser("cancel", help="取消 run")
    cancel.add_argument("run_id")

    rerun = sub.add_parser("rerun", help="重跑 run")
    rerun.add_argument("run_id")
    rerun.add_argument("--requirement", default=None)
    return parser


COMMANDS = {
    "get": _cmd_get,
    "download": _cmd_download,
    "compare": _cmd_compare,
    "submit": _cmd_submit,
    "cancel": _cmd_cancel,
    "rerun": _cmd_rerun,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        COMMANDS[args.cmd](ArcBenchClient(), args)
    except ArcBenchError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
