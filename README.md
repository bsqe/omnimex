# Omnimex

**Omnimex** 是一个本地优先、默认零 API 成本的跨 Coding Agent 会话检索、中央 Markdown 记忆库、MCP 工具服务与跨 Agent 任务交接（Handoff）平台。

- **混合检索**：基于 SQLite WAL + `FTS5` BM25 + 本地 `fastembed` ONNX 向量（`BAAI/bge-small-en-v1.5`，离线/未缓存自动回退至确定性本地哈希嵌入）与 Reciprocal Rank Fusion（RRF）融合检索。
- **自动脱敏**：入库、索引、API 返回与 Handoff 导出前自动清洗 `sk-`、`ghp_`、`AIza`、Bearer Token、JWT、含密码数据库 DSN 及 PEM 私钥等敏感凭据。
- **实时与增量采集**：基于 `ingest_cursors` 字节/行游标增量解析与 Watchdog 文件监听，优雅容错 Windows `WinError 5` 权限限制与 IDE SQLite WAL 文件锁。

---

## 1. 支持的 11 个主流 Coding Agent 适配器

| Agent Slug | 名称 | 默认本地发现路径 / 存储形式 | 原生记忆只读导入 |
|---|---|---|---|
| `antigravity` | **Antigravity** | `~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl`（含 `transcript_full.jsonl` 截断补全与权限容错） | — |
| `claude_code` | **Claude Code** | `~/.claude/projects/*/*.jsonl`、`~/.claude/history.jsonl` | `~/.claude/projects/*/memory/*.md` |
| `codex` | **Codex CLI** | `~/.codex/sessions/**/rollout-*.jsonl`、`archived_sessions/`、`sqlite/state_5.sqlite` | `~/.codex/memories/*.md`、`sqlite/memories_1.sqlite`（只读临时副本） |
| `cursor` | **Cursor** | `%APPDATA%/Cursor/User/(globalStorage\|workspaceStorage/*)/state.vscdb`（只读临时副本含 `-wal`/`-shm`） | — |
| `windsurf` | **Windsurf** | `%APPDATA%/Windsurf/User/.../state.vscdb`、`~/.codeium/windsurf/cascade/*.json(l)` | `~/.codeium/windsurf/memories/*.md` |
| `gemini_cli` | **Gemini CLI** | `~/.gemini/tmp/<project_hash>/chats/session-*.json(l)` | — |
| `cline` | **Cline** | `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/*/` | — |
| `roo_code` | **Roo Code** | `%APPDATA%/Code/User/globalStorage/rooveterinaryinc.roo-cline/tasks/*/` | — |
| `copilot` | **GitHub Copilot** | `%APPDATA%/Code/User/workspaceStorage/*/chatSessions/*.json(l)`、`state.vscdb`、`~/.copilot/session-state/` | — |
| `opencode` | **OpenCode** | `%LOCALAPPDATA%/opencode/opencode.db` / `~/.local/share/opencode/storage/` | — |
| `aider` | **Aider** | 工作区或扫描目录下的 `.aider.chat.history.md` | — |

> 完整字段归一化与路径发现细节见 [docs/ADAPTER_SCHEMAS.md](docs/ADAPTER_SCHEMAS.md)。

---

## 2. 中央 Markdown 记忆库规则

1. **单一真相源（`data/memories/`）**：
   - 所有人工创建、启发式/LLM 蒸馏、以及 Agent 通过 MCP `memory_save` / `memory_update` 写入的记忆，**统一且仅**落盘在中央目录 `data/memories/`（由 `omnimex.toml` 中 `[storage] memories_dir = "data/memories"` 指定）的 YAML Frontmatter + Markdown 文件中，并同步索引至 `data/omnimex.db`（FTS5 + 向量）。
   - 记忆分类支持：`preference`（偏好）、`decision`（决策）、`pitfall`（避坑）、`fact`（事实）；内置 SHA-256 去重、冲突检测与引用文件修改后的过期标记。
2. **原生 `MEMORY.md` 只读导入、绝不反向污染**：
   - Claude Code、Codex、Windsurf 的原生记忆文件仅作只读扫描导入（标记为 `origin = "native_import"`），Omnimex **绝不修改或写回**任何 `~/.claude/`、`~/.codex/`、`~/.codeium/` 原生记忆目录。

---

## 3. 快速上手

### 3.1 环境准备与状态检查

```powershell
# 在仓库根目录激活 Python 3.11 虚拟环境（也可直接调用 .venv\Scripts\omnimex.exe）
.\.venv\Scripts\Activate.ps1

# 查看数据库、记忆目录、本地嵌入模式、蒸馏器及 11 个适配器发现状态
omnimex status
```

### 3.2 扫描会话、实时监听与检索

```powershell
# 增量扫描所有已启用适配器（默认启用自动启发式蒸馏与原生记忆只读导入）
omnimex scan

# 仅扫描指定适配器或忽略游标全量重扫
omnimex scan --agent antigravity --agent claude_code --full

# 启动 Watchdog 实时监听会话目录与 data/memories/*.md 变更
omnimex watch

# 跨 Agent 混合检索（支持 hybrid | fts | vector）
omnimex search "sqlite wal" --mode hybrid --limit 10
omnimex search "redaction" --agent antigravity --workspace "E:/AAAAA project test/omnimex"
```

### 3.3 启动 Web UI 与 REST/SSE API 服务

```powershell
# 1) 如需最新前端静态资源，先构建 frontend/dist/
cd frontend
npm install
npm run build
cd ..

# 2) 启动 FastAPI（默认 127.0.0.1:8765，自动托管 frontend/dist 与 /api/* 接口，启动扫描在后台异步执行）
omnimex serve

# 3) 或使用 Windows 无黑框托盘 + 独立应用窗口启动（推荐日常使用）：
#    双击项目根目录 Launch Omnimex.vbs，或运行：
omnimex tray
#    如需在桌面生成带图标快捷方式：
powershell -ExecutionPolicy Bypass -File .\scripts\create_desktop_shortcut.ps1
```

- 仅前端开发调试时，可在 `frontend/` 运行 `npm run dev`（`http://localhost:5173` 自动代理 `/api/*` 到 `127.0.0.1:8765`；后端未启动时前端会自动切换到可交互的 **Demo Fallback** 模式）。
- API 规范详见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)。

### 3.4 生成跨 Agent 交接简报（Handoff）

```powershell
# 根据任意历史会话生成脱敏、针对目标 Agent 裁剪的 Markdown 交接简报
omnimex handoff antigravity:3521e894 --target claude_code --output handoff.md
```

### 3.5 启动 MCP Server 与一键安全配置客户端

Omnimex 提供 FastMCP 服务（`stdio` 与 `streamable-http`），暴露 7 个渐进式低 Token 工具：`history_search`、`history_read`、`memory_recall`、`memory_read`、`memory_save`、`memory_update`、`session_handoff`。

```powershell
# 预览将写入各客户端的 MCP 配置（默认 --dry-run，不修改任何文件）
omnimex setup

# 应用配置到指定客户端（写入前自动生成 .bak 备份）
omnimex setup --client antigravity --client claude_code --client codex --apply

# 同时导出工作区 AGENTS.md / CLAUDE.md 规则片段，并安装 omnimex-memory SKILL.md
omnimex setup --apply --export-rules --install-skill
```

`--client` 支持：`antigravity`、`claude_code`（`~/.claude.json` 或 `--project` 下 `.mcp.json`）、`codex`（`~/.codex/config.toml`）、`cursor`、`windsurf`、`gemini_cli`。  
`--install-skill` 会将 [skills/omnimex-memory/SKILL.md](skills/omnimex-memory/SKILL.md) 安装至 `~/.codex/skills/omnimex-memory/SKILL.md`、`~/.claude/skills/omnimex-memory/SKILL.md` 和 `~/.gemini/antigravity/skills/omnimex-memory/SKILL.md`。

手动启动 MCP Server：

```powershell
omnimex mcp --transport stdio
# 或 HTTP 模式：
omnimex mcp --transport streamable-http --host 127.0.0.1 --port 8766
```

---

## 4. 从默认零成本模式切换为 Ollama / OpenAI 兼容 API（含历史补蒸馏）

Omnimex 默认采用**“零成本规则提取 + Agent 主动写入”**工作流：
- `omnimex.toml` 中默认配置 `[distiller] provider = "heuristic"` 与 `auto_distill_on_scan = true`（零网络请求、零 API 费用），自动提取报错修复、关键文件、命令与决策为 `heuristic` 草稿；
- 配合 MCP + `SKILL.md`，Coding Agent 在排查疑难问题或确立架构决策后会调用 `memory_save` 主动沉淀高价值记忆；
- `[embedding] provider = "auto"` 且 `allow_download = false`，本地已有 `data/fastembed_cache` 时启用 ONNX `BAAI/bge-small-en-v1.5`，否则离线确定性运行。

日后随时可无缝切换为本地 **Ollama** 或 **OpenAI 兼容 API**，无需重新采集原始会话：

### 方式 A：通过配置文件 / 环境变量切换

建议将个人本地覆盖写入已被 `.gitignore` 忽略的 `omnimex.local.toml`（或直接修改 `omnimex.toml`）：

```toml
# 切换为本地 Ollama（依然零 API 费用）
[distiller]
provider = "ollama"
model = "qwen2.5:7b-instruct"
base_url = "http://127.0.0.1:11434/v1"
auto_distill_on_scan = true
timeout_seconds = 30.0
```

或切换为 OpenAI 兼容服务（支持通过环境变量 `OMNIMEX_DISTILLER_PROVIDER`、`OMNIMEX_DISTILLER_MODEL`、`OMNIMEX_DISTILLER_BASE_URL`、`OMNIMEX_DISTILLER_API_KEY` 注入，避免密钥入库）：

```toml
[distiller]
provider = "openai_compatible"
model = "gpt-4o-mini"
base_url = "https://api.openai.com/v1"
auto_distill_on_scan = true
```

也可在 Web UI 的 **Adapters & Stats → Memory Distiller Configuration** 面板直接切换并保存（会持久化到 `omnimex.local.toml`，`api_key` 仅写不回显）。

### 方式 B：执行历史会话补蒸馏与草稿升级

```powershell
# 使用当前配置对尚未蒸馏的会话执行补蒸馏
# （切换到 ollama / openai_compatible 后，即使不加 --force 也会自动升级旧 heuristic 会话）
omnimex distill --backfill --limit 25

# 使用命令行临时指定 Ollama；如需连同已 llm_distilled 的会话强制重跑，可加 --force
omnimex distill --backfill --provider ollama --model qwen2.5:7b-instruct --base-url http://127.0.0.1:11434/v1 --limit 50

# 对单个指定会话强制重新蒸馏
omnimex distill --session antigravity:3521e894 --force
```

---

## 5. 测试与质量检查命令

### Python 后端（仓库根目录）

```powershell
# Lint 检查
.venv\Scripts\python.exe -m ruff check src tests

# 完整 pytest 套件（在 Windows 沙盒外运行，允许 FastAPI TestClient Proactor socketpair）
.venv\Scripts\python.exe -m pytest -v

# 若在受限 Windows 沙盒内运行，可跳过 FastAPI TestClient 异步事件循环测试：
.venv\Scripts\python.exe -m pytest -k "not test_api_rest_endpoints and not test_api_sse_streams" -v
```

### 前端（`frontend/` 目录）

```powershell
cd frontend
npx tsc --noEmit
npm test
npm run build
```
