"""
devin-context-guard-mcp
=======================

An MCP server (stdio transport) that helps autonomous desktop agents:

  * watch their own resource headroom (memory / CPU / disk) so they can back off
    before the host OOM-killer or a cgroup limit terminates them, and
  * keep their context window lean by discovering bloated paths in a workspace
    (dependency folders, build output, giant logs, binary artifacts) that should
    be excluded from indexing and file reads.

Protocol hygiene
----------------
stdout is reserved exclusively for JSON-RPC frames. Every log line, warning and
traceback goes to stderr. Tool failures are returned as structured JSON payloads
rather than raised, so a bad path or a permission error never tears down the
session.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------------------
# Logging: stderr only. Never print() in this process.
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("CONTEXT_GUARD_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [context-guard] %(message)s",
)
log = logging.getLogger("context-guard")

# ---------------------------------------------------------------------------
# Configuration (environment-overridable)
# ---------------------------------------------------------------------------

MB = 1024 * 1024
GB = 1024 * MB

LOW_MEMORY_THRESHOLD_BYTES = int(os.environ.get("CONTEXT_GUARD_LOW_MEMORY_MB", "500")) * MB
LOW_DISK_THRESHOLD_BYTES = int(os.environ.get("CONTEXT_GUARD_LOW_DISK_MB", "1024")) * MB
HIGH_CPU_THRESHOLD_PCT = float(os.environ.get("CONTEXT_GUARD_HIGH_CPU_PCT", "90"))

# Optional allow-list of roots the scanner may touch (os.pathsep separated).
# When unset, any readable directory is allowed.
ALLOWED_ROOTS: list[Path] = [
    Path(p).expanduser().resolve()
    for p in os.environ.get("CONTEXT_GUARD_ALLOWED_ROOTS", "").split(os.pathsep)
    if p.strip()
]

# Rough heuristic used by most tokenizers on source/log text.
BYTES_PER_TOKEN = 4

# Directory names that are almost never worth putting in an LLM context.
BLOAT_DIR_NAMES: dict[str, str] = {
    "node_modules": "JavaScript dependencies",
    "bower_components": "JavaScript dependencies",
    ".pnpm-store": "pnpm store",
    ".yarn": "Yarn cache",
    "vendor": "vendored dependencies",
    ".venv": "Python virtualenv",
    "venv": "Python virtualenv",
    "env": "Python virtualenv",
    "site-packages": "Python packages",
    "__pycache__": "Python bytecode cache",
    ".pytest_cache": "pytest cache",
    ".mypy_cache": "mypy cache",
    ".ruff_cache": "ruff cache",
    ".tox": "tox environments",
    ".nox": "nox environments",
    "build": "build output",
    "dist": "distribution output",
    "out": "build output",
    "target": "Rust/Java build output",
    "bin": "compiled binaries",
    "obj": ".NET intermediate output",
    ".next": "Next.js build cache",
    ".nuxt": "Nuxt build cache",
    ".svelte-kit": "SvelteKit build cache",
    ".turbo": "Turborepo cache",
    ".parcel-cache": "Parcel cache",
    ".cache": "generic cache",
    "coverage": "coverage reports",
    "htmlcov": "coverage reports",
    ".gradle": "Gradle cache",
    ".terraform": "Terraform providers",
    "Pods": "CocoaPods dependencies",
    "DerivedData": "Xcode build output",
    ".git": "git object store",
    ".hg": "Mercurial store",
    ".svn": "Subversion store",
}

# Extensions for files that are binary / generated and useless as text context.
ARTIFACT_EXTENSIONS: dict[str, str] = {
    ".zip": "archive", ".tar": "archive", ".gz": "archive", ".tgz": "archive",
    ".bz2": "archive", ".xz": "archive", ".7z": "archive", ".rar": "archive",
    ".whl": "Python wheel", ".egg": "Python egg", ".jar": "Java archive",
    ".war": "Java archive", ".so": "shared library", ".dll": "shared library",
    ".dylib": "shared library", ".exe": "executable", ".bin": "binary blob",
    ".o": "object file", ".a": "static library", ".lib": "static library",
    ".pyc": "Python bytecode", ".class": "Java bytecode",
    ".pt": "model weights", ".pth": "model weights", ".ckpt": "model checkpoint",
    ".onnx": "model weights", ".safetensors": "model weights", ".h5": "model weights",
    ".pkl": "pickle", ".npy": "NumPy array", ".npz": "NumPy archive",
    ".parquet": "columnar data", ".sqlite": "database", ".sqlite3": "database",
    ".db": "database", ".iso": "disk image", ".dmg": "disk image",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".wav": "audio",
    ".map": "source map",
}

LOG_EXTENSIONS = {".log", ".out", ".err", ".trace"}
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".chunk.js")

# Hard ceilings so a hostile or enormous tree can't hang the server.
MAX_FILES_VISITED = int(os.environ.get("CONTEXT_GUARD_MAX_FILES", "200000"))
MAX_SCAN_SECONDS = float(os.environ.get("CONTEXT_GUARD_MAX_SCAN_SECONDS", "30"))
MAX_FINDINGS = 500

mcp = MCPServer("devin-context-guard")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False, default=str)


def _error(code: str, message: str, **extra: Any) -> str:
    log.warning("%s: %s", code, message)
    return _json({"status": "error", "error": {"code": code, "message": message, **extra}})


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _read_cgroup_memory_limit() -> int | None:
    """Return the cgroup memory limit in bytes when running in a container, else None."""
    candidates = (
        "/sys/fs/cgroup/memory.max",                        # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",      # cgroup v1
    )
    for path in candidates:
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 reports a huge sentinel when unlimited.
        if value >= 1 << 60:
            return None
        return value
    return None


def _read_cgroup_memory_usage() -> int | None:
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            return int(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def _validate_workspace(raw: str) -> Path:
    if not raw or not raw.strip():
        raise ValueError("workspace_path must be a non-empty path")
    path = Path(raw).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"workspace_path does not exist or cannot be resolved: {raw}") from exc
    if not path.is_dir():
        raise ValueError(f"workspace_path is not a directory: {path}")
    if ALLOWED_ROOTS and not any(path == root or root in path.parents for root in ALLOWED_ROOTS):
        raise PermissionError(
            f"workspace_path {path} is outside CONTEXT_GUARD_ALLOWED_ROOTS"
        )
    return path


def _load_ignore_patterns(root: Path) -> list[str]:
    """Collect patterns from common ignore files at the workspace root.

    This is a deliberately lightweight matcher (fnmatch on normalized patterns),
    not a full gitignore implementation; it is only used to flag findings as
    'already ignored' vs 'un-ignored'.
    """
    patterns: list[str] = []
    for name in (".gitignore", ".cursorignore", ".codeiumignore", ".aiignore", ".dockerignore"):
        f = root / name
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            patterns.append(line.rstrip("/").lstrip("/"))
    return patterns


def _is_ignored(rel_posix: str, patterns: list[str]) -> bool:
    name = rel_posix.rsplit("/", 1)[-1]
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel_posix, pat + "/*"):
                return True
            if pat.startswith("**/") and fnmatch.fnmatch(name, pat[3:]):
                return True
        elif fnmatch.fnmatch(name, pat):
            return True
        # Ancestor directory ignored => descendant ignored.
        if any(fnmatch.fnmatch(part, pat) for part in rel_posix.split("/")[:-1] if "/" not in pat):
            return True
    return False


@dataclass
class _ScanBudget:
    deadline: float
    files_left: int
    truncated: bool = False

    def spend(self) -> bool:
        self.files_left -= 1
        if self.files_left <= 0 or time.monotonic() > self.deadline:
            self.truncated = True
            return False
        return True


def _dir_size(path: Path, budget: _ScanBudget) -> tuple[int, int]:
    """Return (bytes, file_count) for a directory without following symlinks."""
    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if not budget.spend():
                        return total, count
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError as exc:
            log.debug("skip unreadable dir %s: %s", current, exc)
    return total, count


@dataclass
class _Finding:
    path: str
    kind: str
    reason: str
    size_bytes: int
    file_count: int = 1
    already_ignored: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "reason": self.reason,
            "size": _human(self.size_bytes),
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "estimated_tokens": self.size_bytes // BYTES_PER_TOKEN,
            "already_ignored": self.already_ignored,
        }


@dataclass
class _ScanResult:
    findings: list[_Finding] = field(default_factory=list)
    files_scanned: int = 0
    total_bytes: int = 0
    truncated: bool = False


def _scan_workspace(
    root: Path,
    large_file_bytes: int,
    large_log_bytes: int,
    max_depth: int,
    patterns: list[str],
) -> _ScanResult:
    result = _ScanResult()
    budget = _ScanBudget(deadline=time.monotonic() + MAX_SCAN_SECONDS, files_left=MAX_FILES_VISITED)
    stack: list[tuple[Path, int]] = [(root, 0)]

    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            log.debug("skip unreadable dir %s: %s", current, exc)
            continue

        for entry in entries:
            if not budget.spend():
                break
            try:
                if entry.is_symlink():
                    continue
                entry_path = Path(entry.path)
                rel = entry_path.relative_to(root).as_posix()

                if entry.is_dir(follow_symlinks=False):
                    if entry.name in BLOAT_DIR_NAMES:
                        size, count = _dir_size(entry_path, budget)
                        result.total_bytes += size
                        result.files_scanned += count
                        result.findings.append(_Finding(
                            path=rel + "/",
                            kind="bloat_directory",
                            reason=BLOAT_DIR_NAMES[entry.name],
                            size_bytes=size,
                            file_count=count,
                            already_ignored=_is_ignored(rel, patterns),
                        ))
                        continue  # never descend into bloat
                    if depth < max_depth:
                        stack.append((entry_path, depth + 1))
                    continue

                if not entry.is_file(follow_symlinks=False):
                    continue

                size = entry.stat(follow_symlinks=False).st_size
                result.files_scanned += 1
                result.total_bytes += size
                lower = entry.name.lower()
                suffix = entry_path.suffix.lower()

                kind = reason = None
                if (suffix in LOG_EXTENSIONS or ".log." in lower) and size >= large_log_bytes:
                    kind, reason = "large_log", "oversized log file"
                elif suffix in ARTIFACT_EXTENSIONS and size >= 256 * 1024:
                    kind, reason = "binary_artifact", ARTIFACT_EXTENSIONS[suffix]
                elif lower.endswith(MINIFIED_SUFFIXES) and size >= 100 * 1024:
                    kind, reason = "generated_bundle", "minified / bundled output"
                elif lower.endswith(".lock") or lower in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock"}:
                    if size >= 200 * 1024:
                        kind, reason = "lockfile", "large dependency lockfile"
                elif size >= large_file_bytes:
                    kind, reason = "large_file", "file exceeds size threshold"

                if kind:
                    result.findings.append(_Finding(
                        path=rel,
                        kind=kind,
                        reason=reason,
                        size_bytes=size,
                        already_ignored=_is_ignored(rel, patterns),
                    ))
            except OSError as exc:
                log.debug("skip unreadable entry %s: %s", entry.path, exc)
                continue

        if budget.truncated:
            break

    result.truncated = budget.truncated
    return result


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _collect_resources(disk_path: str) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    # interval=0.5 gives a real sample instead of the meaningless first-call 0.0
    cpu_pct = psutil.cpu_percent(interval=0.5)
    per_cpu = psutil.cpu_percent(interval=None, percpu=True)
    try:
        load_avg = [round(x, 2) for x in psutil.getloadavg()]
    except (AttributeError, OSError):
        load_avg = None

    disk_target = Path(disk_path).expanduser() if disk_path else Path.cwd()
    if not disk_target.exists():
        disk_target = Path(disk_target.anchor or "/")
    du = shutil.disk_usage(disk_target)

    cg_limit = _read_cgroup_memory_limit()
    cg_usage = _read_cgroup_memory_usage()
    effective_available = vm.available
    if cg_limit is not None and cg_usage is not None:
        effective_available = min(effective_available, max(cg_limit - cg_usage, 0))

    proc = psutil.Process()
    with proc.oneshot():
        self_rss = proc.memory_info().rss

    return {
        "vm": vm, "swap": swap, "cpu_pct": cpu_pct, "per_cpu": per_cpu,
        "load_avg": load_avg, "disk_target": disk_target, "du": du,
        "cg_limit": cg_limit, "cg_usage": cg_usage,
        "effective_available": effective_available, "self_rss": self_rss,
    }


@mcp.tool()
async def inspect_resource_limits(disk_path: str = "") -> str:
    """Report available memory, CPU load and free disk space for the host.

    Call this before memory-hungry work (large builds, test suites, indexing).
    If effective available memory drops below the low-memory threshold
    (default 500 MB, cgroup-aware), the response has status "warning" and an
    `alert` block with code NEAR_OOM_THRESHOLD and recommended actions.

    Args:
        disk_path: Path whose filesystem should be checked for free space.
                   Defaults to the server's working directory.
    """
    try:
        r = await asyncio.to_thread(_collect_resources, disk_path)
    except Exception as exc:  # noqa: BLE001 - must never crash the stdio session
        log.exception("inspect_resource_limits failed")
        return _error("RESOURCE_PROBE_FAILED", f"{type(exc).__name__}: {exc}")

    vm, du = r["vm"], r["du"]
    alerts: list[dict[str, Any]] = []

    if r["effective_available"] < LOW_MEMORY_THRESHOLD_BYTES:
        alerts.append({
            "code": "NEAR_OOM_THRESHOLD",
            "severity": "critical",
            "message": (
                f"Only {_human(r['effective_available'])} of memory is available "
                f"(threshold {_human(LOW_MEMORY_THRESHOLD_BYTES)}). The process is at risk "
                "of being killed by the OOM killer or a cgroup memory limit."
            ),
            "available_bytes": r["effective_available"],
            "threshold_bytes": LOW_MEMORY_THRESHOLD_BYTES,
            "cgroup_limited": r["cg_limit"] is not None,
            "recommended_actions": [
                "Pause spawning new subprocesses, builds, or test runners.",
                "Run memory-heavy commands serially with reduced parallelism (e.g. -j1, --runInBand, --maxWorkers=1).",
                "Close idle language servers, dev servers, and browser tabs.",
                "Avoid loading large files fully into memory; stream or page through them.",
                "Re-run inspect_resource_limits before resuming heavy work.",
            ],
        })
    if du.free < LOW_DISK_THRESHOLD_BYTES:
        alerts.append({
            "code": "LOW_DISK_SPACE",
            "severity": "high",
            "message": f"Only {_human(du.free)} free on {r['disk_target']}.",
            "free_bytes": du.free,
            "threshold_bytes": LOW_DISK_THRESHOLD_BYTES,
            "recommended_actions": [
                "Delete build caches and temporary artifacts you created.",
                "Avoid writing large logs or downloading large dependencies.",
            ],
        })
    if r["cpu_pct"] >= HIGH_CPU_THRESHOLD_PCT:
        alerts.append({
            "code": "HIGH_CPU_LOAD",
            "severity": "medium",
            "message": f"CPU utilisation is {r['cpu_pct']:.0f}%.",
            "recommended_actions": ["Reduce parallelism and expect slower command execution."],
        })

    status = "ok"
    if any(a["severity"] == "critical" for a in alerts):
        status = "warning"
    elif alerts:
        status = "degraded"

    payload: dict[str, Any] = {
        "status": status,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {
            "platform": platform.platform(),
            "logical_cpus": psutil.cpu_count(logical=True),
            "physical_cpus": psutil.cpu_count(logical=False),
        },
        "memory": {
            "total": _human(vm.total),
            "available": _human(vm.available),
            "effective_available": _human(r["effective_available"]),
            "effective_available_bytes": r["effective_available"],
            "used_percent": vm.percent,
            "swap_used_percent": r["swap"].percent,
            "cgroup_limit": _human(r["cg_limit"]) if r["cg_limit"] is not None else None,
            "cgroup_usage": _human(r["cg_usage"]) if r["cg_usage"] is not None else None,
            "this_server_rss": _human(r["self_rss"]),
        },
        "cpu": {
            "percent": r["cpu_pct"],
            "per_cpu_percent": r["per_cpu"],
            "load_average_1_5_15": r["load_avg"],
        },
        "disk": {
            "path": str(r["disk_target"]),
            "total": _human(du.total),
            "free": _human(du.free),
            "free_bytes": du.free,
            "used_percent": round(du.used / du.total * 100, 1) if du.total else None,
        },
        "alerts": alerts,
    }
    if status == "warning":
        payload["alert"] = alerts[0]  # the NEAR_OOM_THRESHOLD block, surfaced top-level
    return _json(payload)


@mcp.tool()
async def optimize_workspace_context(
    workspace_path: str,
    large_file_mb: float = 5.0,
    large_log_mb: float = 1.0,
    max_depth: int = 12,
    include_already_ignored: bool = False,
) -> str:
    """Find token-wasting paths in a workspace and return exclude globs.

    Recursively scans `workspace_path` (no symlink following, bounded by time
    and file-count budgets) for dependency folders (node_modules, .venv, ...),
    build output (build/, dist/, target/, .next/ ...), oversized logs, binary
    artifacts, minified bundles and large lockfiles. Returns findings ranked by
    estimated token cost plus ready-to-use `exclude_globs` for .cursorignore,
    .gitignore, or an agent's file-read deny list.

    Args:
        workspace_path: Absolute path to the workspace root.
        large_file_mb: Any file at or above this size is flagged.
        large_log_mb: Log files at or above this size are flagged.
        max_depth: Maximum directory depth to descend.
        include_already_ignored: Also report paths already covered by an
            ignore file (.gitignore, .cursorignore, .aiignore, ...).
    """
    try:
        root = _validate_workspace(workspace_path)
    except PermissionError as exc:
        return _error("WORKSPACE_NOT_ALLOWED", str(exc))
    except ValueError as exc:
        return _error("INVALID_WORKSPACE", str(exc))

    if not (0 < large_file_mb <= 10_240) or not (0 < large_log_mb <= 10_240):
        return _error("INVALID_ARGUMENT", "size thresholds must be between 0 and 10240 MB")
    max_depth = max(0, min(int(max_depth), 64))

    started = time.monotonic()
    try:
        patterns = await asyncio.to_thread(_load_ignore_patterns, root)
        scan = await asyncio.to_thread(
            _scan_workspace,
            root,
            int(large_file_mb * MB),
            int(large_log_mb * MB),
            max_depth,
            patterns,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("optimize_workspace_context failed for %s", root)
        return _error("SCAN_FAILED", f"{type(exc).__name__}: {exc}")

    findings = scan.findings
    if not include_already_ignored:
        findings = [f for f in findings if not f.already_ignored]
    findings.sort(key=lambda f: f.size_bytes, reverse=True)
    dropped = max(0, len(findings) - MAX_FINDINGS)
    findings = findings[:MAX_FINDINGS]

    excluded_bytes = sum(f.size_bytes for f in findings)

    # Collapse bloat directories by name into a single recursive glob each.
    globs: list[str] = []
    seen: set[str] = set()
    for f in findings:
        if f.kind == "bloat_directory":
            g = f"**/{Path(f.path.rstrip('/')).name}/"
        else:
            g = f.path
        if g not in seen:
            seen.add(g)
            globs.append(g)
    if any(f.kind == "large_log" for f in findings):
        globs.append("**/*.log")

    by_kind: dict[str, dict[str, int]] = {}
    for f in findings:
        k = by_kind.setdefault(f.kind, {"count": 0, "size_bytes": 0})
        k["count"] += 1
        k["size_bytes"] += f.size_bytes

    log.info(
        "scanned %s: %d files, %d findings, %.2fs%s",
        root, scan.files_scanned, len(findings), time.monotonic() - started,
        " (truncated)" if scan.truncated else "",
    )

    return _json({
        "status": "ok",
        "workspace": str(root),
        "scan": {
            "files_scanned": scan.files_scanned,
            "total_size": _human(scan.total_bytes),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "truncated": scan.truncated,
            "ignore_patterns_loaded": len(patterns),
        },
        "summary": {
            "findings": len(findings),
            "findings_omitted": dropped,
            "excludable_size": _human(excluded_bytes),
            "estimated_tokens_saved": excluded_bytes // BYTES_PER_TOKEN,
            "by_kind": by_kind,
        },
        "exclude_globs": globs,
        "findings": [f.as_dict() for f in findings],
        "guidance": (
            "Add exclude_globs to .cursorignore / .aiignore / your agent's read-deny "
            "list. Never read files listed here in full; use tail/grep for logs."
        ),
    })


def main() -> None:
    log.info("starting devin-context-guard MCP server (stdio)")
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        log.exception("fatal server error")
        sys.exit(1)


if __name__ == "__main__":
    main()
