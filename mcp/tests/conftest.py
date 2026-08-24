from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType


repo_root = Path(__file__).resolve().parents[2]
agent_root = Path(
    os.environ.get("AKASHIC_AGENT_ROOT", "").strip()
    or repo_root.parents[1] / "akasic-agent"
)
for path in (repo_root, agent_root):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
if str(repo_root / "mcp") not in sys.path:
    sys.path.append(str(repo_root / "mcp"))

package = ModuleType("feed_test_plugin")
package.__path__ = [str(repo_root)]
package.__package__ = "feed_test_plugin"
sys.modules["feed_test_plugin"] = package

_test_data_dir = tempfile.TemporaryDirectory(prefix="feed-plugin-tests-")
if not os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip():
    os.environ["AKA_PLUGIN_DATA_DIR"] = _test_data_dir.name
