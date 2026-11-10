# arcbench-mcp

ARC-Bench（[arc-bench.com](https://arc-bench.com)）的 MCP server + CLI：**拉取提交 zip、查历史分数与榜单、浏览 run 工作区与日志、对比两次提交的每任务得分**。写操作（提交/取消/重跑）只在 CLI 提供，人工确认。

架构：`arcbench_mcp/api.py` 里的两张注册表（`RESOURCES` / `DOWNLOADS`）是唯一事实源——MCP 工具、CLI 子命令、工具文档全部由它生成，加能力 = 加一行。

## 安装

```bash
git clone https://github.com/WncFht/arcbench-mcp.git
cd arcbench-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # 填入 ARCBENCH_COOKIE（获取方法见下）
```

### 一行安装 prompt（直接丢给 agent）

```text
请帮我安装 https://github.com/WncFht/arcbench-mcp 这个 ARC-Bench MCP server：clone 到 ~/arcbench-mcp，在其目录下创建 venv 并执行 .venv/bin/pip install -e .，然后在我的 MCP 客户端配置（mcpServers）里注册 {"command": "<项目绝对路径>/.venv/bin/python", "args": ["-m", "arcbench_mcp.server"]}；完成后告诉我需要把浏览器 Cookie 填进 ~/arcbench-mcp/.env 的哪个字段、怎么获取。
```

## 配置登录 cookie

站点无 API token，全部接口走浏览器会话 cookie（有效期约两周，过期重新复制即可）：

1. 浏览器打开 arc-bench.com 并登录
2. DevTools → 网络（Network）→ 任一 `/api/` 请求 → 请求标头 → 复制整段 `Cookie:` 的值
3. 写入项目根目录 `.env`（参考 `.env.example`）：

```bash
ARCBENCH_COOKIE='arcbench_session=...; arc_api_sticky=...'
```

也支持 Cookie 管理器导出的 JSON 数组格式（`[{"name":...,"value":...}]`），或存到单独文件用 `ARCBENCH_COOKIE_FILE` 指向。注意：cookie 即账号凭证，不要提交进 git（已在 `.gitignore`）。

## MCP 工具（3 个，全部只读）

| 工具 | 作用 |
|---|---|
| `arc_api(resource, params)` | 查询全部只读资源，JSON 原样返回。resource 由注册表枚举：`me` / `competitions` / `competition` / `tasks` / `task` / `task_tests` / `submissions` / `runs` / `run` / `leaderboard` / `workspace_files` / `file` / `logs` |
| `arc_download(kind, id)` | 下载 zip 并列出内含文件清单：`project_zip`（run 工作区包）/ `archive`（提交评测快照）/ `competition_requirements` / `initial_projects` |
| `arc_compare(idA, idB)` | 两次提交的每任务得分对比 → markdown 报告 |

## ZCode / Claude 接入

在 MCP 配置（如 ZCode 的 `mcpServers`）里加：

```json
{
  "mcpServers": {
    "arcbench": {
      "command": "/Users/fanghaotian/Downloads/arcbench-mcp/.venv/bin/python",
      "args": ["-m", "arcbench_mcp.server"]
    }
  }
}
```

cookie 从 `arcbench-mcp/.env` 读取，改 cookie 不用改配置。

## CLI

```bash
arcbench get submissions id=arc-bench-web          # 历史提交及每任务分数
arcbench get run id=f08fa8bc9e5e                   # 单次 run 详情
arcbench get leaderboard competition_id=arc-bench-web
arcbench download project_zip f08fa8bc9e5e         # 拉 project.zip
arcbench download archive edfea39b7f92             # 拉提交评测快照
arcbench compare edfea39b7f92 b8a14125fe33         # markdown 对比（自动定位所属比赛）
arcbench submit dist/app.zip --requirement arc-bench-web--12306   # dry-run
arcbench submit dist/app.zip --requirement arc-bench-web--12306 --confirm   # 计费提交
```

## 常用对照

- 比赛详情（含任务清单与测试数）：`get competition id=arc-bench-web`
- 某任务榜单：`get leaderboard task_id=arc-bench-web--12306`
- run 过程复盘：`get run id=...`（含 `steps`/`tests`）、`get logs id=...`（事件+stdout/stderr）
- 只看单文件：`get file id=<run_id> file_path=backend/src/xxx.py`
