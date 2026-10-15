# devin-resource-guard-mcp

A Model Context Protocol (MCP) server for autonomous coding agents such as Devin, Cursor, and Claude Code. It runs over `stdio` and targets two common failure modes in long agent sessions: **context bloat** and **out-of-memory (OOM) crashes**.

### Features

*   **Resource headroom checks:** On each call, it reports memory, CPU, and disk headroom. Inside a container, it reads the cgroup memory limit from `/sys/fs/cgroup` (v1 and v2), so the numbers reflect the container rather than the host. When effective free memory falls below a threshold (500 MB by default), it returns a structured `NEAR_OOM_THRESHOLD` warning with recommended actions. The agent can then back off before the OOM killer or a cgroup limit ends its process.
*   **Workspace context optimization:** It scans a workspace for token-heavy, low-value paths such as dependency trees, build output, large logs, binary artifacts, minified bundles, and lockfiles. It ranks them by estimated token cost and returns `exclude_globs` that keep them out of the agent's context.

*Works with any MCP client, including Devin, Claude Desktop, Claude Code, Cursor, and VS Code. This is an independent project, not affiliated with Cognition.*

---

## Tools

### `inspect_resource_limits`

| Argument    | Type   | Default         | Description                                   |
|-------------|--------|-----------------|-----------------------------------------------|
| `disk_path` | string | server's cwd    | Path whose filesystem is checked for free space |

It returns memory (total, available, and cgroup-aware *effective* available), CPU utilisation and load average, and disk usage.

`status` takes one of three values:

| `status`   | Meaning                                                           |
|------------|-------------------------------------------------------------------|
| `ok`       | Nothing is wrong.                                                 |
| `degraded` | Disk space is low or the CPU is saturated.                        |
| `warning`  | Effective available memory is **below 500 MB**. A top-level `alert` is included. |

Example of the low-memory warning:

```json
{
  "status": "warning",
  "alert": {
    "code": "NEAR_OOM_THRESHOLD",
    "severity": "critical",
    "message": "Only 412.3 MB of memory is available (threshold 500.0 MB). The process is at risk of being killed by the OOM killer or a cgroup memory limit.",
    "available_bytes": 432324608,
    "threshold_bytes": 524288000,
    "cgroup_limited": true,
    "recommended_actions": [
      "Pause spawning new subprocesses, builds, or test runners.",
      "Run memory-heavy commands serially with reduced parallelism (e.g. -j1, --runInBand, --maxWorkers=1).",
      "..."
    ]
  },
  "memory": { "...": "..." },
  "cpu":    { "...": "..." },
  "disk":   { "...": "..." },
  "alerts": [ "..." ]
}
```

Inside a container, the server reads `/sys/fs/cgroup` (v1 and v2), so the warning reflects the container's limit rather than the host's total RAM.

### `optimize_workspace_context`

| Argument                  | Type   | Default | Description                                         |
|---------------------------|--------|---------|-----------------------------------------------------|
| `workspace_path`          | string | —       | Absolute path to the workspace root (required)       |
| `large_file_mb`           | number | `5`     | Flag any file at or above this size                  |
| `large_log_mb`            | number | `1`     | Flag `.log` / `.out` / `.err` files at or above this size |
| `max_depth`               | int    | `12`    | Maximum recursion depth (capped at 64)               |
| `include_already_ignored` | bool   | `false` | Also report paths already covered by ignore files    |

The scan detects:

| Kind               | Examples                                                                 |
|--------------------|--------------------------------------------------------------------------|
| `bloat_directory`  | `node_modules/`, `.venv/`, `build/`, `dist/`, `target/`, `.next/`, `__pycache__/`, `.git/`, `coverage/` |
| `large_log`        | `server.log`, `app.log.1`, `build.out`                                   |
| `binary_artifact`  | `.zip`, `.whl`, `.so`, `.dll`, `.onnx`, `.safetensors`, `.sqlite`, `.map` |
| `generated_bundle` | `*.min.js`, `*.bundle.js`, `*.chunk.js`                                  |
| `lockfile`         | large `package-lock.json`, `yarn.lock`, `poetry.lock`                    |
| `large_file`       | any other file over `large_file_mb`                                      |

The response ranks findings by size and estimated token cost (about 4 bytes per token). It also includes `exclude_globs` that you can paste into `.cursorignore`, `.aiignore`, or an agent's deny list:

```json
{
  "status": "ok",
  "summary": {
    "findings": 7,
    "excludable_size": "1.8 GB",
    "estimated_tokens_saved": 483183820
  },
  "exclude_globs": ["**/node_modules/", "**/dist/", "logs/server.log", "**/*.log"],
  "findings": [
    { "path": "node_modules/", "kind": "bloat_directory", "size": "1.2 GB", "estimated_tokens": 322122547, "already_ignored": false }
  ]
}
```

The scanner reads `.gitignore`, `.cursorignore`, `.codeiumignore`, `.aiignore`, and `.dockerignore` at the root, so by default it reports only paths that are **not yet ignored**.

---

## Quick start

**Requires Python 3.10+.**

```bash
git clone https://github.com/crona16/devin-resource-guard-mcp.git
cd devin-resource-guard-mcp
python -m venv .venv
```

Activate the virtualenv:

```bash
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

Confirm that the server starts. It will wait silently on stdin, and you can stop it with Ctrl+C:

```bash
python server.py
```

To exercise the tools interactively, use the official MCP Inspector. It needs Node.js:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python server.py
```

---

## Connecting to an agent

Every client below takes the same two things:

1. **command:** the **absolute path** to the virtualenv's Python interpreter.
   - macOS / Linux: `/abs/path/devin-resource-guard-mcp/.venv/bin/python`
   - Windows: `C:\\abs\\path\\devin-resource-guard-mcp\\.venv\\Scripts\\python.exe`
2. **args:** the absolute path to `server.py`.

> Use absolute paths. GUI apps don't inherit your shell's `PATH` or working directory, so a bare `python` often resolves to the wrong interpreter, one without `mcp` installed.

### Cursor

Edit `~/.cursor/mcp.json` to make the server available globally, or `.cursor/mcp.json` in a project to scope it to that project:

```json
{
  "mcpServers": {
    "context-guard": {
      "command": "/abs/path/devin-resource-guard-mcp/.venv/bin/python",
      "args": ["/abs/path/devin-resource-guard-mcp/server.py"],
      "env": {
        "CONTEXT_GUARD_LOW_MEMORY_MB": "500"
      }
    }
  }
}
```

Then open **Settings → MCP** and confirm that `context-guard` shows a green dot with 2 tools.

### Devin (Desktop / local MCP)

Add the server through Devin's MCP settings (**Settings → MCP servers → Add custom server**) as a **stdio** server:

| Field     | Value                                                     |
|-----------|-----------------------------------------------------------|
| Name      | `context-guard`                                           |
| Transport | `stdio`                                                   |
| Command   | `/abs/path/devin-resource-guard-mcp/.venv/bin/python`      |
| Arguments | `/abs/path/devin-resource-guard-mcp/server.py`             |
| Env       | `CONTEXT_GUARD_ALLOWED_ROOTS=/abs/path/to/your/repos` (optional) |

If your Devin build accepts a JSON config instead, use the same `mcpServers` block as in the Cursor example. The exact menu path varies between Devin releases, so check Devin's MCP documentation if the menu items above don't match.

### Claude Desktop

Edit the config file for your platform:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the same `mcpServers` block as in the Cursor example, then restart the app.

### Claude Code

```bash
claude mcp add context-guard -- /abs/path/devin-resource-guard-mcp/.venv/bin/python /abs/path/devin-resource-guard-mcp/server.py
```

### VS Code (Copilot agent mode)

Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "context-guard": {
      "type": "stdio",
      "command": "${workspaceFolder}/../devin-resource-guard-mcp/.venv/bin/python",
      "args": ["${workspaceFolder}/../devin-resource-guard-mcp/server.py"]
    }
  }
}
```

### Suggested agent rule

Add a rule like this to your agent's rules or system prompt so it actually uses the tools:

> Before starting a build, test suite, or large refactor, call `inspect_resource_limits`. If `status` is `warning`, follow `alert.recommended_actions` before continuing. When you open a new workspace, call `optimize_workspace_context` once, and never read paths that match its `exclude_globs`.

---

## Configuration

All settings are optional environment variables:

| Variable                          | Default   | Purpose                                                 |
|-----------------------------------|-----------|---------------------------------------------------------|
| `CONTEXT_GUARD_LOW_MEMORY_MB`     | `500`     | Memory threshold for the `NEAR_OOM_THRESHOLD` warning    |
| `CONTEXT_GUARD_LOW_DISK_MB`       | `1024`    | Free-disk threshold for `LOW_DISK_SPACE`                 |
| `CONTEXT_GUARD_HIGH_CPU_PCT`      | `90`      | CPU threshold for `HIGH_CPU_LOAD`                        |
| `CONTEXT_GUARD_ALLOWED_ROOTS`     | *(unset)* | `os.pathsep`-separated list of directories the scanner may read. When set, other paths are rejected. |
| `CONTEXT_GUARD_MAX_FILES`         | `200000`  | Maximum number of filesystem entries visited per scan    |
| `CONTEXT_GUARD_MAX_SCAN_SECONDS`  | `30`      | Maximum time per scan (results are marked `truncated`)   |
| `CONTEXT_GUARD_LOG_LEVEL`         | `INFO`    | stderr log verbosity                                     |

---

## Design notes

- **stdout is used only for the protocol.** All logging goes to stderr, and the code never calls `print()`. Errors are returned as structured `{"status": "error", "error": {...}}` payloads instead of being raised, so one bad path doesn't kill the session.
- **The event loop stays free.** psutil sampling and filesystem walks run in `asyncio.to_thread`.
- **The scanner is safe by default.** It never follows symlinks, it never descends into bloat directories (it only measures their size), it is bounded by time and file-count budgets, and it can be confined with `CONTEXT_GUARD_ALLOWED_ROOTS`.
- **The scanner is read-only.** It deletes or modifies nothing. It only returns recommendations.

### Limitations

- Ignore-file matching is a lightweight `fnmatch` approximation, not a full gitignore engine. Negation (`!pattern`) is skipped. The matching only affects the `already_ignored` flag, never what gets scanned.
- Token estimates use a fixed rate of about 4 bytes per token. Treat them as an order of magnitude.
- Generic directory names such as `bin/`, `out/`, `env/`, and `vendor/` are flagged by name alone. Review findings before you exclude them.

## License

MIT
