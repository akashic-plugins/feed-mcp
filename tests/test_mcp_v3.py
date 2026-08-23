from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


MCP_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "mcp" / "src" / "mcp_bridge.py"
RUN_MCP_PATH = Path(__file__).resolve().parents[1] / "mcp" / "run_mcp.py"


_ARTIFACT_PROBE = r"""
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import sys


def load_module(path):
    spec = importlib.util.spec_from_file_location("feed_artifact_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load probe module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


action = sys.argv[1]
module = load_module(Path(sys.argv[2]))
if action == "tools":
    os.environ["FEED_BACKEND"] = "recording"
    server = module.create_mcp_server()
    result = {
        "tools": sorted(tool.name for tool in server._tool_manager.list_tools())
    }
elif action == "recording-error":
    os.environ["FEED_BACKEND"] = "recording"
    try:
        module._live_backend()
    except RuntimeError as error:
        result = {"error_type": type(error).__name__, "message": str(error)}
    else:
        raise AssertionError("recording backend unexpectedly reached live backend")
elif action == "logging":
    runtime = Path(sys.argv[3])
    module._setup_logging(runtime)
    handlers = logging.getLogger().handlers
    rotating = [
        handler for handler in handlers if isinstance(handler, RotatingFileHandler)
    ]
    result = {
        "rotating_handlers": len(rotating),
        "backup_count": rotating[0].backupCount,
        "max_bytes": rotating[0].maxBytes,
    }
    for handler in handlers:
        handler.close()
    logging.getLogger().handlers.clear()
else:
    raise ValueError(f"unknown artifact probe: {action}")
print(json.dumps(result, sort_keys=True))
"""


def _run_artifact_probe(action: str, module: Path, *args: Path) -> dict[str, object]:
    """Run one module oracle inside the explicitly supplied service artifact."""

    # 1. Resolve the required artifact boundary without a pytest fallback.
    artifact_python = Path(os.environ["AKASHIC_PLUGIN_FIXTURE_PYTHON"])
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    # 2. Execute the exact module and decode its fixed observable result.
    completed = subprocess.run(
        [
            str(artifact_python),
            "-c",
            _ARTIFACT_PROBE,
            action,
            str(module),
            *(str(arg) for arg in args),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise TypeError("Feed artifact probe must return a JSON object")
    return result


def test_recording_mcp_exposes_only_user_driven_tools() -> None:
    assert _run_artifact_probe("tools", MCP_BRIDGE_PATH) == {
        "tools": ["feed_manage", "feed_query"]
    }


def test_recording_user_tool_fails_before_backend_access() -> None:
    result = _run_artifact_probe("recording-error", MCP_BRIDGE_PATH)

    assert result["error_type"] == "RuntimeError"
    assert "recording backend" in str(result["message"])


def test_runner_uses_three_bounded_log_rotations(
    tmp_path: Path,
) -> None:
    assert _run_artifact_probe("logging", RUN_MCP_PATH, tmp_path) == {
        "rotating_handlers": 1,
        "backup_count": 3,
        "max_bytes": 5 * 1024 * 1024,
    }


def test_artifact_probe_requires_fixture_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", raising=False)

    with pytest.raises(KeyError, match="AKASHIC_PLUGIN_FIXTURE_PYTHON"):
        _run_artifact_probe("tools", MCP_BRIDGE_PATH)


def test_artifact_probe_rejects_missing_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing-artifact" / "bin" / "python"
    monkeypatch.setenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", str(missing))

    with pytest.raises(FileNotFoundError):
        _run_artifact_probe("tools", MCP_BRIDGE_PATH)
