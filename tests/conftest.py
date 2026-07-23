import sys
from pathlib import Path

# 让 `import larkflow` 在任意 cwd 下可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
