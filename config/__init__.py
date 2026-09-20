# PyMySQL 作为纯 Python 的 MySQL 驱动，兼容 Django 期望的 MySQLdb 接口。
try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # 允许在未安装 pymysql 时（如仅用 SQLite 跑单测）导入配置
    pass
