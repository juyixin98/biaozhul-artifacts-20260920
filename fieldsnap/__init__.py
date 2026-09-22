# 使用纯 Python 的 PyMySQL 作为 MySQLdb 驱动，避免编译 mysqlclient。
try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # 本地测试（SQLite）未安装 pymysql 时忽略
    pass
