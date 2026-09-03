"""数据库连接封装。用之前请把下面的配置改成你自己 Navicat 里连接用的账号密码。"""
import os

import pymysql
from pymysql.cursors import DictCursor

DB_CONFIG = {
    # 支持用环境变量覆盖（服务器部署用）：DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "wechat_finance"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
}


def get_conn():
    return pymysql.connect(**DB_CONFIG)

