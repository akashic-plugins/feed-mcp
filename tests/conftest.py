from __future__ import annotations

import os
import sys
from types import ModuleType
from pathlib import Path


repo_root = Path(__file__).resolve().parents[1]
agent_root = Path(os.environ["AKASHIC_AGENT_ROOT"])
for path in (repo_root, agent_root):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
if str(repo_root / "mcp") not in sys.path:
    sys.path.append(str(repo_root / "mcp"))

package = ModuleType("feed_test_plugin")
package.__path__ = [str(repo_root)]
package.__package__ = "feed_test_plugin"
sys.modules["feed_test_plugin"] = package
