"""支持 ``python -m sam ...``。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
