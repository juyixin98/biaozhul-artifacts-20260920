# pytest 根目录 conftest：保证 `import ba` / `import main` 可用
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
