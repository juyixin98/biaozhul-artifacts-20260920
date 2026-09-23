"""使 cbmc 成为可执行包：python -m cbmc ... 由 cli.main 接管。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
