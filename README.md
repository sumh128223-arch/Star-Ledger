# 繁星记账本

个人记账 Web 应用：导入微信/支付宝账单自动分类，支持预算、周期账、统计分析，浏览器（含手机）直接使用。

## 运行步骤

1. 安装 Python 3.10+ 和 MySQL 8。
2. 建库并导入表结构（空表，不含任何数据）：
   ```powershell
   mysql -u root -p -e "CREATE DATABASE wechat_finance DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
   mysql -u root -p wechat_finance < schema.sql
   ```
3. 配置数据库连接：设置环境变量 `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME`（默认 `127.0.0.1:3306`、用户 `root`、密码为空），或修改 `db.py` 顶部的 `DB_CONFIG`。
4. 安装依赖并启动：
   ```powershell
   pip install -r requirements.txt
   python app.py
   ```
5. 浏览器打开 http://127.0.0.1:5000 ，注册账号即可使用。

## 说明

- 会话密钥 `SECRET_KEY` 可用环境变量设置；未设置时每次启动随机生成（重启后需重新登录）。
- 数据库密码请用环境变量或本地修改 `db.py`，不要把真实密码提交到仓库。
