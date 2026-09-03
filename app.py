import os
import json
import re
import uuid
import math
from datetime import datetime, date, timedelta

from zoneinfo import ZoneInfo

# 统一使用中国时区（北京时间），不依赖服务器时区，避免差 8 小时
_TZ_CN = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(_TZ_CN).replace(tzinfo=None)


def today():
    return now().date()

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file, abort

from auth import auth_bp, login_required
from db import get_conn
from parser import parse_wechat_bill, parse_alipay_bill, detect_bill_format

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24).hex()
app.register_blueprint(auth_bp)

# 子目录挂载支持：设置环境变量 URL_PREFIX（如 /ledger）后，程序挂到子目录，
# 不占用服务器根路径（根路径可继续给其它项目使用）。默认空 = 挂根路径，行为不变。
URL_PREFIX = os.environ.get("URL_PREFIX", "").strip().strip("/")
if URL_PREFIX:
    URL_PREFIX = "/" + URL_PREFIX
    app.config["APPLICATION_ROOT"] = URL_PREFIX
    app.config["SESSION_COOKIE_PATH"] = URL_PREFIX + "/"


class PrefixMiddleware:
    """把 URL_PREFIX 从 PATH_INFO 剥掉并写入 SCRIPT_NAME，
    这样 Flask 的 url_for 会自动给所有链接加上前缀。"""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        prefix = self.prefix
        if prefix and path == prefix:
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = "/"
        elif prefix and path.startswith(prefix + "/"):
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = path[len(prefix):]
        elif prefix:
            # 挂了子目录时，非前缀请求一律 404，确保不占用根路径/其它项目
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Not Found"]
        return self.wsgi_app(environ, start_response)


if URL_PREFIX:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

PENDING_DIR = os.path.join(os.path.dirname(__file__), "_pending_imports")
os.makedirs(PENDING_DIR, exist_ok=True)


def _pending_path(batch_id):
    safe_id = "".join(c for c in batch_id if c.isalnum())
    return os.path.join(PENDING_DIR, f"{safe_id}.json")


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat(sep=" ")
    if isinstance(o, float) and math.isnan(o):
        return ""
    return str(o)


def save_pending_records(records, filename, user_id):
    batch_id = uuid.uuid4().hex
    with open(_pending_path(batch_id), "w", encoding="utf-8") as f:
        json.dump({"user_id": user_id, "filename": filename, "records": records}, f, default=_json_default, ensure_ascii=False)
    return batch_id


def load_pending_records(batch_id):
    path = _pending_path(batch_id)
    if not batch_id or not os.path.exists(path):
        return None, None, None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("records"), data.get("filename"), data.get("user_id")


def clear_pending_records(batch_id):
    path = _pending_path(batch_id)
    if os.path.exists(path):
        os.remove(path)


# 分类视觉配置：图标 + 颜色
CATEGORY_STYLE = {
    "餐饮":   {"icon": "🍜", "color": "#f5793a", "bg_color": "#fef0e8"},
    "交通":   {"icon": "🚌", "color": "#4a90d9", "bg_color": "#e8f2fb"},
    "购物":   {"icon": "🛍️", "color": "#9b59b6", "bg_color": "#f3eaf8"},
    "娱乐":   {"icon": "🎮", "color": "#e74c3c", "bg_color": "#fdecea"},
    "通讯":   {"icon": "📱", "color": "#5c6bc0", "bg_color": "#eceff9"},
    "医疗":   {"icon": "💊", "color": "#1abc9c", "bg_color": "#e5f9f5"},
    "教育":   {"icon": "📚", "color": "#3498db", "bg_color": "#e8f4fd"},
    "社交":   {"icon": "💐", "color": "#ff6b9d", "bg_color": "#ffeef3"},
    "工资":   {"icon": "💰", "color": "#27ae60", "bg_color": "#e9f7ef"},
    "转账":   {"icon": "💸", "color": "#f39c12", "bg_color": "#fef9e7"},
    "收红包": {"icon": "🧧", "color": "#e74c3c", "bg_color": "#fdecea"},
    "收转账": {"icon": "↔️",  "color": "#2ecc71", "bg_color": "#e9f7ef"},
    "其他":   {"icon": "📂", "color": "#95a5a6", "bg_color": "#f4f6f6"},
    "未分类": {"icon": "❓", "color": "#bdc3c7", "bg_color": "#f8f9f9"},
}
_DEFAULT_STYLE = {"icon": "📌", "color": "#aaa", "bg_color": "#f5f5f5"}


def _enrich(rows):
    result = []
    for row in rows:
        style = CATEGORY_STYLE.get(row["category"], _DEFAULT_STYLE)
        result.append({
            "category":  row["category"],
            "total":     float(row["total"]),
            "icon":      style["icon"],
            "color":     style["color"],
            "bg_color":  style["bg_color"],
        })
    return result


def _build_tx_list(rows):
    result = []
    for row in rows:
        style = CATEGORY_STYLE.get(row["category"], _DEFAULT_STYLE)
        tt = row["trans_time"]
        time_str = tt.strftime("%m月%d日 %H:%M") if hasattr(tt, "strftime") else str(tt)[5:16]
        result.append({
            "trans_time": time_str,
            "merchant":   row["merchant"],
            "product":    row["product"],
            "amount":     float(row["amount"]),
            "category":   row["category"],
            "icon":       style["icon"],
            "bg_color":   style["bg_color"],
        })
    return result


def load_categories():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, type FROM categories ORDER BY id")
        return cur.fetchall()


def load_user_rules(user_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.name AS category_name, r.keyword
            FROM category_rules r
            JOIN categories c ON c.id = r.category_id
            WHERE r.user_id = %s
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    rules = {}
    for row in rows:
        rules.setdefault(row["category_name"], []).append(row["keyword"])
    return rules


def _display_name():
    """页面右上角显示的名字：优先昵称，没有昵称时用用户名。"""
    return session.get("nickname") or session.get("username")


# ============================================================
# 预算 / 固定支出 / 统计扩展 辅助函数
# ============================================================
def _load_budgets(user_id, month_key):
    """返回 {category_id 或 'total': amount} 形式的月度预算字典。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT category_id, amount FROM budgets WHERE user_id = %s AND month_key = %s",
            (user_id, month_key),
        )
        result = {}
        for row in cur.fetchall():
            result[row["category_id"] if row["category_id"] is not None else "total"] = float(row["amount"])
        return result


def _month_spent(user_id, month_key):
    """本月支出：返回 (总支出, {category_id: 金额})。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id AS category_id, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND DATE_FORMAT(t.trans_time, '%%Y-%%m') = %s
            GROUP BY c.id
            """,
            (user_id, month_key),
        )
        rows = cur.fetchall()
    by_cat = {row["category_id"]: float(row["total"]) for row in rows if row["category_id"] is not None}
    return sum(by_cat.values()), by_cat


def _budget_status(user_id, month_key):
    """返回 (预算使用情况列表, 总预算金额)。未设置预算时返回 (None, 0)。"""
    budgets = _load_budgets(user_id, month_key)
    if not budgets:
        return None, 0.0
    total_spent, by_cat = _month_spent(user_id, month_key)
    items = []
    total_budget = budgets.get("total")
    if total_budget:
        items.append({
            "name": "总预算",
            "budget": total_budget,
            "spent": total_spent,
            "pct": round(total_spent / total_budget * 100, 1) if total_budget else 0.0,
            "over": total_spent > total_budget,
            "is_total": True,
        })
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM categories WHERE type = 'expense' ORDER BY id")
        cats = cur.fetchall()
    for cat in cats:
        b = budgets.get(cat["id"])
        if not b:
            continue
        spent = by_cat.get(cat["id"], 0.0)
        items.append({
            "name": cat["name"],
            "budget": b,
            "spent": spent,
            "pct": round(spent / b * 100, 1) if b else 0.0,
            "over": spent > b,
            "is_total": False,
        })
    items.sort(key=lambda x: (0 if x["is_total"] else 1, -x["pct"]))
    return items, total_budget or 0.0


def _add_months(month_key, delta):
    y, m = int(month_key[:4]), int(month_key[5:7])
    total = y * 12 + (m - 1) + delta
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def _pct_change(cur_v, prev_v):
    if not prev_v:
        return None
    return round((cur_v - prev_v) / prev_v * 100, 1)


def _insert_recurring_tx(cur, item, trans_time):
    """按固定支出项目插入一条交易记录（transaction_no 带项目 id，用于去重）。"""
    tx_type = "income" if item["income_expense"] == "收入" else "expense"
    transaction_no = "recurring-%s-%s" % (item["id"], uuid.uuid4().hex)
    cur.execute(
        """
        INSERT INTO transactions
            (user_id, trans_time, trans_type, merchant, product, income_expense,
             amount, pay_method, status, transaction_no, merchant_no, remark,
             type, category_id, is_auto_categorized)
        VALUES
            (%s, %s, '固定支出', %s, '', %s, %s, '', '', %s, '', %s, %s, %s, 0)
        """,
        (
            item["user_id"], trans_time, item["merchant"], item["income_expense"],
            item["amount"], transaction_no, item["name"], tx_type, item["category_id"],
        ),
    )


def run_recurring_checks(user_id):
    """把到期的固定收支自动记账；同一项目同一个月只记一次。"""
    today_dt = today()
    month_key = today_dt.strftime("%Y-%m")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM recurring_items WHERE user_id = %s AND active = 1 ORDER BY id",
            (user_id,),
        )
        for item in cur.fetchall():
            if item["last_run_month"] == month_key:
                continue
            if item["day_of_month"] > today_dt.day:
                continue
            cur.execute(
                """
                SELECT id FROM transactions
                WHERE user_id = %s AND transaction_no LIKE %s
                  AND DATE_FORMAT(trans_time, '%%Y-%%m') = %s
                LIMIT 1
                """,
                (user_id, "recurring-%s-%%" % item["id"], month_key),
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE recurring_items SET last_run_month = %s WHERE id = %s",
                    (month_key, item["id"]),
                )
                continue
            tx_time = datetime(today_dt.year, today_dt.month, min(item["day_of_month"], 28), 9, 0)
            if tx_time > now():
                tx_time = now()
            _insert_recurring_tx(cur, item, tx_time)
            cur.execute(
                "UPDATE recurring_items SET last_run_month = %s WHERE id = %s",
                (month_key, item["id"]),
            )
        conn.commit()


@app.before_request
def _maybe_run_recurring():
    """每天第一次访问时检查固定收支是否到期（只对已登录用户）。"""
    if "user_id" not in session:
        return
    today_key = today().isoformat()
    if session.get("recurring_checked") == today_key:
        return
    run_recurring_checks(session["user_id"])
    session["recurring_checked"] = today_key


@app.route("/manual-doc")
def manual():
    """用户使用说明手册：默认在线预览（inline），加 ?download=1 则下载。"""
    pdf_path = os.path.join(os.path.dirname(__file__), "用户使用说明手册.pdf")
    if not os.path.isfile(pdf_path):
        abort(404)
    download = request.args.get("download") == "1"
    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=download,
        download_name="用户使用说明手册.pdf",
    )


@app.route("/agreement")
def agreement():
    return render_template("agreement.html")


@app.route("/")
@login_required
def index():
    return render_template("upload.html", username=_display_name())


def find_similar_records(user_id, merchant, amount, income_expense, trans_time):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT trans_time, merchant, amount, income_expense
            FROM transactions
            WHERE user_id = %s
              AND merchant = %s
              AND amount = %s
              AND income_expense = %s
              AND trans_time BETWEEN %s AND %s
            ORDER BY trans_time DESC
            LIMIT 5
            """,
            (
                user_id,
                merchant,
                amount,
                income_expense,
                trans_time - timedelta(days=7),
                trans_time + timedelta(days=7),
            ),
        )
        return cur.fetchall()


@app.route("/manual", methods=["GET", "POST"])
@login_required
def manual_add():
    categories = load_categories()

    if request.method == "POST":
        user_id = session["user_id"]
        income_expense = request.form.get("income_expense", "支出")
        amount_str = request.form.get("amount", "").strip()
        category_id_str = request.form.get("category_id", "")
        merchant = request.form.get("merchant", "").strip() or "手工导入"
        trans_time_str = request.form.get("trans_time", "").strip()
        if trans_time_str:
            try:
                trans_time = datetime.strptime(trans_time_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("时间格式不正确")
                return redirect(url_for("manual_add"))
        else:
            trans_time = now()


        if income_expense not in ("收入", "支出"):
            flash("收/支 类型不正确")
            return redirect(url_for("manual_add"))

        try:
            amount = round(float(amount_str), 2)
        except (TypeError, ValueError):
            flash("金额格式不正确")
            return redirect(url_for("manual_add"))

        if amount <= 0:
            flash("金额必须大于 0")
            return redirect(url_for("manual_add"))

        try:
            category_id = int(category_id_str)
        except (TypeError, ValueError):
            category_id = 0

        category = next((c for c in categories if c["id"] == category_id), None)
        expect_type = "income" if income_expense == "收入" else "expense"
        if not category or category["type"] != expect_type:
            flash("请选择有效的分类")
            return redirect(url_for("manual_add"))

        confirm = request.form.get("confirm") == "1"
        if not confirm:
            duplicates = find_similar_records(user_id, merchant, amount, income_expense, trans_time)
            if duplicates:
                form_data = {
                    "income_expense": income_expense,
                    "amount": amount_str,
                    "category_id": category_id,
                    "merchant": merchant,
                    "trans_time": trans_time.strftime("%Y-%m-%dT%H:%M"),
                }
                return render_template(
                    "manual.html",
                    categories=categories,
                    username=_display_name(),
                    default_time=now().strftime("%Y-%m-%dT%H:%M"),
                    form_data=form_data,
                    duplicate={"records": duplicates},
                )

        tx_type = "income" if income_expense == "收入" else "expense"
        transaction_no = f"manual-{uuid.uuid4().hex}"

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transactions
                    (user_id, trans_time, trans_type, merchant, product, income_expense,
                     amount, pay_method, status, transaction_no, merchant_no, remark,
                     type, category_id, is_auto_categorized)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    trans_time,
                    "手工记账",
                    merchant,
                    "",
                    income_expense,
                    amount,
                    "",
                    "",
                    transaction_no,
                    "",
                    "",
                    tx_type,
                    category_id,
                    0,
                ),
            )
            conn.commit()

        if income_expense == "支出":
            budget_items, _ = _budget_status(user_id, trans_time.strftime("%Y-%m"))
            over = [b for b in (budget_items or []) if b["over"]]
            if over:
                names = "、".join(b["name"] for b in over)
                flash(f"⚠️ 注意：{names} 本月已超出预算")

        flash(f"已添加一笔{income_expense}：{merchant} {amount:.2f} 元")
        return redirect(url_for("manual_add"))

    return render_template(
        "manual.html",
        categories=categories,
        username=_display_name(),
        default_time=now().strftime("%Y-%m-%dT%H:%M"),
    )


def _load_manual_record(record_id, user_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, trans_time, merchant, amount, income_expense, category_id, type, transaction_no
            FROM transactions
            WHERE id = %s AND user_id = %s AND transaction_no LIKE 'manual-%%'
            """,
            (record_id, user_id),
        )
        return cur.fetchone()


@app.route("/records")
@login_required
def records():
    user_id = session["user_id"]

    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    category_id = request.args.get("category_id", "").strip()
    merchant = request.args.get("merchant", "").strip()
    amount_min = request.args.get("amount_min", "").strip()
    amount_max = request.args.get("amount_max", "").strip()
    income_expense = request.args.get("income_expense", "").strip()

    searching = any([start, end, category_id, merchant, amount_min, amount_max, income_expense])

    sql = """
        SELECT t.id, t.trans_time, t.merchant, t.amount, t.income_expense, t.transaction_no,
               COALESCE(c.name, '未分类') AS category
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.user_id = %s
    """
    params = [user_id]
    if not searching:
        sql += " AND t.transaction_no LIKE %s"
        params.append("manual-%")
    else:
        if start:
            sql += " AND DATE(t.trans_time) >= %s"
            params.append(start)
        if end:
            sql += " AND DATE(t.trans_time) <= %s"
            params.append(end)
        if category_id.isdigit():
            sql += " AND t.category_id = %s"
            params.append(int(category_id))
        if merchant:
            sql += " AND t.merchant LIKE %s"
            params.append("%" + merchant + "%")
        if income_expense in ("收入", "支出"):
            sql += " AND t.income_expense = %s"
            params.append(income_expense)
        try:
            if amount_min != "" and float(amount_min) >= 0:
                sql += " AND t.amount >= %s"
                params.append(amount_min)
        except ValueError:
            pass
        try:
            if amount_max != "" and float(amount_max) >= 0:
                sql += " AND t.amount <= %s"
                params.append(amount_max)
        except ValueError:
            pass
    sql += " ORDER BY t.trans_time DESC, t.id DESC"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    for row in rows:
        row["is_manual"] = str(row["transaction_no"] or "").startswith("manual-")

    categories = load_categories()
    return render_template(
        "edit_records.html",
        records=rows,
        username=_display_name(),
        categories=categories,
        filters={
            "start": start, "end": end, "category_id": category_id,
            "merchant": merchant, "amount_min": amount_min, "amount_max": amount_max,
            "income_expense": income_expense, "searching": searching,
        },
    )


@app.route("/records/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
def record_edit(record_id):
    user_id = session["user_id"]
    record = _load_manual_record(record_id, user_id)
    if not record:
        flash("记录不存在或无权操作")
        return redirect(url_for("records"))
    categories = load_categories()

    if request.method == "POST":
        income_expense = request.form.get("income_expense", "支出")
        amount_str = request.form.get("amount", "").strip()
        category_id_str = request.form.get("category_id", "")
        merchant = request.form.get("merchant", "").strip() or "手工导入"
        trans_time_str = request.form.get("trans_time", "").strip()

        if income_expense not in ("收入", "支出"):
            flash("收/支 类型不正确")
            return redirect(url_for("record_edit", record_id=record_id))
        try:
            amount = round(float(amount_str), 2)
        except (TypeError, ValueError):
            flash("金额格式不正确")
            return redirect(url_for("record_edit", record_id=record_id))
        if amount <= 0:
            flash("金额必须大于 0")
            return redirect(url_for("record_edit", record_id=record_id))
        try:
            trans_time = datetime.strptime(trans_time_str, "%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            flash("时间格式不正确")
            return redirect(url_for("record_edit", record_id=record_id))
        try:
            category_id = int(category_id_str)
        except (TypeError, ValueError):
            category_id = 0
        category = next((c for c in categories if c["id"] == category_id), None)
        expect_type = "income" if income_expense == "收入" else "expense"
        if not category or category["type"] != expect_type:
            flash("请选择有效的分类")
            return redirect(url_for("record_edit", record_id=record_id))

        tx_type = "income" if income_expense == "收入" else "expense"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions
                SET trans_time = %s, income_expense = %s, amount = %s, merchant = %s,
                    category_id = %s, type = %s, is_auto_categorized = 0
                WHERE id = %s AND user_id = %s
                """,
                (trans_time, income_expense, amount, merchant, category_id, tx_type, record_id, user_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                flash("记录不存在或无权操作")
                return redirect(url_for("records"))
        flash("记录已更新")
        return redirect(url_for("records"))

    return render_template(
        "record_edit.html",
        record=record,
        categories=categories,
        username=_display_name(),
    )


@app.route("/records/<int:record_id>/delete", methods=["POST"])
@login_required
def record_delete(record_id):
    user_id = session["user_id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s AND transaction_no LIKE 'manual-%%'",
            (record_id, user_id),
        )
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        flash("记录已删除")
    else:
        flash("记录不存在或无权操作")
    return redirect(url_for("records"))


def _finalize_preview(records, filename, user_id):
    """上传解析完成后的公共收尾：存待确认批次 -> 跳转预览页。"""
    batch_id = save_pending_records(records, filename, user_id)
    session["pending_batch_id"] = batch_id

    categories = load_categories()
    return render_template("preview.html", records=records, categories=categories, filename=filename, username=_display_name())


def _handle_upload(kind_name, parser_fn):
    """微信 / 支付宝导入的公共流程：保存文件 -> 对应解析器解析 -> 预览确认。"""
    user_id = session["user_id"]
    file = request.files.get("bill_file")
    if not file or file.filename == "":
        flash("请选择一个文件")
        return redirect(url_for("index"))

    save_path = os.path.join(UPLOAD_DIR, file.filename)
    file.save(save_path)

    try:
        rules = load_user_rules(user_id)
        records = parser_fn(save_path, user_id, extra_rules=rules)
    except ValueError as e:
        flash(f"{kind_name}解析失败：{e}")
        return redirect(url_for("index"))

    if not records:
        flash("文件里没有解析到任何交易记录")
        return redirect(url_for("index"))

    return _finalize_preview(records, file.filename, user_id)


@app.route("/upload/wechat", methods=["POST"])
@login_required
def upload_wechat():
    """导入微信账单（走微信专用解析器，不再自动识别）。"""
    return _handle_upload("微信账单", parse_wechat_bill)


@app.route("/upload/alipay", methods=["POST"])
@login_required
def upload_alipay():
    """导入支付宝账单（走支付宝专用解析器，不再自动识别）。"""
    return _handle_upload("支付宝账单", parse_alipay_bill)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    """旧版自动识别入口：保留兼容，新页面请用 /upload/wechat 或 /upload/alipay。"""
    user_id = session["user_id"]
    file = request.files.get("bill_file")
    if not file or file.filename == "":
        flash("请选择一个文件")
        return redirect(url_for("index"))

    save_path = os.path.join(UPLOAD_DIR, file.filename)
    file.save(save_path)

    try:
        rules = load_user_rules(user_id)
        if detect_bill_format(save_path) == "alipay":
            records = parse_alipay_bill(save_path, user_id, extra_rules=rules)
        else:
            records = parse_wechat_bill(save_path, user_id, extra_rules=rules)
    except ValueError as e:
        flash(f"解析失败：{e}")
        return redirect(url_for("index"))

    if not records:
        flash("文件里没有解析到任何交易记录")
        return redirect(url_for("index"))

    return _finalize_preview(records, file.filename, user_id)


@app.route("/import/wechat")
@login_required
def import_wechat():
    # 微信账单导入页：选文件 -> 解析并预览（主页面只放入口按钮，不占大空间）
    return render_template("import_bill.html", kind="wechat", title="微信账单导入",
                           subtitle="微信支付 → 我 → 账单 → 导出账单（csv / xlsx）",
                           upload_ep="upload_wechat", emoji="💬",
                           username=_display_name())


@app.route("/import/alipay")
@login_required
def import_alipay():
    # 支付宝账单导入页：选文件 -> 解析并预览（主页面只放入口按钮，不占大空间）
    return render_template("import_bill.html", kind="alipay", title="支付宝账单导入",
                           subtitle="支付宝 → 我的 → 账单 → 开具交易流水证明 / 导出（csv / xlsx）",
                           upload_ep="upload_alipay", emoji="💠",
                           username=_display_name())


def _stats_year(user_id, tx_type):
    """年度统计视图：全年收支总览 + 12 个月趋势 + 同比 + 年度分类 + 年度 Top。"""
    year = str(request.args.get("year", now().year))
    if not re.match(r"^\d{4}$", year):
        year = str(now().year)
    prev_year = str(int(year) - 1)

    with get_conn() as conn, conn.cursor() as cur:
        # 年度收支总览
        cur.execute(
            """
            SELECT type, SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s AND YEAR(trans_time) = %s
            GROUP BY type
            """,
            (user_id, year),
        )
        summary = {"income": 0.0, "expense": 0.0}
        for row in cur.fetchall():
            summary[row["type"]] = float(row["total"])

        # 去年总览（同比）
        cur.execute(
            """
            SELECT type, SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s AND YEAR(trans_time) = %s
            GROUP BY type
            """,
            (user_id, prev_year),
        )
        prev_summary = {"income": 0.0, "expense": 0.0}
        for row in cur.fetchall():
            prev_summary[row["type"]] = float(row["total"])

        # 12 个月收支趋势
        cur.execute(
            """
            SELECT DATE_FORMAT(trans_time, '%%Y-%%m') AS m,
                   SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS expense,
                   SUM(CASE WHEN type='income' THEN amount ELSE 0 END) AS income
            FROM transactions
            WHERE user_id = %s AND YEAR(trans_time) = %s
            GROUP BY m ORDER BY m
            """,
            (user_id, year),
        )
        month_map = {row["m"]: row for row in cur.fetchall()}
        monthly_trend = []
        for i in range(1, 13):
            key = "%s-%02d" % (year, i)
            row = month_map.get(key)
            monthly_trend.append({
                "month": key[5:],
                "expense": float(row["expense"]) if row else 0.0,
                "income": float(row["income"]) if row else 0.0,
            })

        # 年度分类占比（按当前 tab）
        cur.execute(
            """
            SELECT COALESCE(c.name, '未分类') AS category, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = %s AND YEAR(t.trans_time) = %s
            GROUP BY COALESCE(c.name, '未分类')
            ORDER BY total DESC
            """,
            (user_id, tx_type, year),
        )
        by_category = _enrich(cur.fetchall())

        # 年度单笔 Top 10
        cur.execute(
            """
            SELECT t.trans_time, t.merchant, t.product, t.amount,
                   COALESCE(c.name, '未分类') AS category
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = %s AND YEAR(t.trans_time) = %s
            ORDER BY t.amount DESC
            LIMIT 10
            """,
            (user_id, tx_type, year),
        )
        top_raw = cur.fetchall()

        cur.execute(
            """
            SELECT t.trans_time, t.merchant, t.product, t.amount,
                   COALESCE(c.name, '未分类') AS category
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = %s AND YEAR(t.trans_time) = %s
            ORDER BY t.amount DESC
            """,
            (user_id, tx_type, year),
        )
        all_raw = cur.fetchall()

    top_transactions = _build_tx_list(top_raw)
    all_transactions = _build_tx_list(all_raw)
    extra_transactions = all_transactions[len(top_transactions):]

    return render_template(
        "stats.html",
        year_month=year + "-01",
        view="year",
        year=year,
        prev_year=prev_year,
        view_mode=tx_type,
        summary=summary,
        prev_summary=prev_summary,
        monthly_trend=monthly_trend,
        daily_trend=[],
        by_category=by_category,
        top_transactions=top_transactions,
        extra_transactions=extra_transactions,
        budget_items=[],
        total_budget=0.0,
        username=_display_name(),
    )


def _stats_day(user_id, tx_type):
    # 按天统计：当天收支构成 + 当天单笔排行
    day = request.args.get("day", now().strftime("%Y-%m-%d"))
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        day = now().strftime("%Y-%m-%d")

    with get_conn() as conn, conn.cursor() as cur:
        # 1. 当天收支总览
        cur.execute(
            """
            SELECT type, SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s AND DATE(trans_time) = %s
            GROUP BY type
            """,
            (user_id, day),
        )
        summary = {"income": 0.0, "expense": 0.0}
        for row in cur.fetchall():
            summary[row["type"]] = float(row["total"])

        # 2. 当天收支构成（按当前 mode 的收支方向）
        cur.execute(
            """
            SELECT COALESCE(c.name, '未分类') AS category, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = %s AND DATE(t.trans_time) = %s
            GROUP BY COALESCE(c.name, '未分类')
            ORDER BY total DESC
            """,
            (user_id, tx_type, day),
        )
        by_category = _enrich(cur.fetchall())

        # 3. 当天单笔明细（按金额倒序，前 10 显示，其余展开）
        cur.execute(
            """
            SELECT t.trans_time, t.merchant, t.product, t.amount,
                   COALESCE(c.name, '未分类') AS category
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = %s AND DATE(t.trans_time) = %s
            ORDER BY t.amount DESC
            """,
            (user_id, tx_type, day),
        )
        all_raw = cur.fetchall()

    all_transactions = _build_tx_list(all_raw)
    top_transactions = all_transactions[:10]
    extra_transactions = all_transactions[10:]

    return render_template(
        "stats.html",
        year_month=day[:7],
        view="day",
        day=day,
        view_mode=tx_type,
        summary=summary,
        prev_summary={"income": 0.0, "expense": 0.0},
        by_category=by_category,
        daily_trend=[],
        monthly_trend=[],
        top_transactions=top_transactions,
        extra_transactions=extra_transactions,
        budget_items=[],
        total_budget=0.0,
        username=_display_name(),
    )


@app.route("/stats")
@login_required
def stats():
    user_id = session["user_id"]
    view_mode  = request.args.get("mode", "expense")   # 'expense' | 'income'
    tx_type    = view_mode
    view       = request.args.get("view", "month")     # 'month' | 'year' | 'day'

    if view == "year":
        return _stats_year(user_id, tx_type)
    if view == "day":
        return _stats_day(user_id, tx_type)

    year_month = request.args.get("month", now().strftime("%Y-%m"))
    if not re.match(r"^\d{4}-\d{2}$", year_month):
        year_month = now().strftime("%Y-%m")

    with get_conn() as conn, conn.cursor() as cur:

        # 1. 收入 / 支出 总览
        cur.execute(
            """
            SELECT type, SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s
              AND DATE_FORMAT(trans_time, '%%Y-%%m') = %s
            GROUP BY type
            """,
            (user_id, year_month),
        )
        summary = {"income": 0.0, "expense": 0.0}
        for row in cur.fetchall():
            summary[row["type"]] = float(row["total"])

        # 1.5 上月总览（环比）
        prev_month = _add_months(year_month, -1)
        cur.execute(
            """
            SELECT type, SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s
              AND DATE_FORMAT(trans_time, '%%Y-%%m') = %s
            GROUP BY type
            """,
            (user_id, prev_month),
        )
        prev_summary = {"income": 0.0, "expense": 0.0}
        for row in cur.fetchall():
            prev_summary[row["type"]] = float(row["total"])

        # 2. 分类占比
        cur.execute(
            """
            SELECT COALESCE(c.name, '未分类') AS category, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s
              AND t.type = %s
              AND DATE_FORMAT(t.trans_time, '%%Y-%%m') = %s
            GROUP BY COALESCE(c.name, '未分类')
            ORDER BY total DESC
            """,
            (user_id, tx_type, year_month),
        )
        by_category = _enrich(cur.fetchall())

        # 3. 每日趋势
        cur.execute(
            """
            SELECT DATE(trans_time) AS day, SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = %s
              AND DATE_FORMAT(trans_time, '%%Y-%%m') = %s
            GROUP BY DATE(trans_time)
            ORDER BY day
            """,
            (user_id, tx_type, year_month),
        )
        daily_trend = [
            {"day": row["day"].strftime("%Y-%m-%d"), "total": float(row["total"])}
            for row in cur.fetchall()
        ]

        # 4. 当月单笔 Top 10
        cur.execute(
            """
            SELECT t.trans_time, t.merchant, t.product, t.amount,
                   COALESCE(c.name, '未分类') AS category
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s
              AND t.type = %s
              AND DATE_FORMAT(t.trans_time, '%%Y-%%m') = %s
            ORDER BY t.amount DESC
            LIMIT 10
            """,
            (user_id, tx_type, year_month),
        )
        top_raw = cur.fetchall()

        # 5. 当月全部单笔明细（用于"全部排行"展开）
        cur.execute(
            """
            SELECT t.trans_time, t.merchant, t.product, t.amount,
                   COALESCE(c.name, '未分类') AS category
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s
              AND t.type = %s
              AND DATE_FORMAT(t.trans_time, '%%Y-%%m') = %s
            ORDER BY t.amount DESC
            """,
            (user_id, tx_type, year_month),
        )
        all_raw = cur.fetchall()

    top_transactions = _build_tx_list(top_raw)
    all_transactions = _build_tx_list(all_raw)
    # 展开区只需要展示 Top 10 之后的部分，避免重复渲染前 10 条
    extra_transactions = all_transactions[len(top_transactions):]

    budget_items, total_budget = _budget_status(user_id, year_month)

    return render_template(
        "stats.html",
        year_month=year_month,
        view="month",
        view_mode=view_mode,
        summary=summary,
        prev_summary=prev_summary,
        by_category=by_category,
        daily_trend=daily_trend,
        monthly_trend=[],
        top_transactions=top_transactions,
        extra_transactions=extra_transactions,
        budget_items=budget_items or [],
        total_budget=total_budget,
        username=_display_name(),
    )


@app.route("/stats/category_detail")
@login_required
def stats_category_detail():
    """分类钻取接口：返回某月/某年某分类的明细记录（JSON）。"""
    user_id = session["user_id"]
    month_key = request.args.get("month", now().strftime("%Y-%m"))
    mode = request.args.get("mode", "expense")
    category = request.args.get("category", "").strip()
    tx_type = "expense" if mode == "expense" else "income"
    year_view = request.args.get("view", "month") == "year"
    day_view = request.args.get("view", "month") == "day"
    day = request.args.get("day", now().strftime("%Y-%m-%d"))
    year = str(request.args.get("year", now().year))

    if year_view:
        date_cond, date_param = "YEAR(t.trans_time) = %s", year
    elif day_view:
        date_cond, date_param = "DATE(t.trans_time) = %s", day
    else:
        date_cond, date_param = "DATE_FORMAT(t.trans_time, '%%Y-%%m') = %s", month_key

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.trans_time, t.merchant, t.product, t.amount
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.type = %s
              AND COALESCE(c.name, '未分类') = %s
              AND """ + date_cond + """
            ORDER BY t.trans_time DESC
            LIMIT 500
            """,
            (user_id, tx_type, category, date_param),
        )
        rows = cur.fetchall()
    result = [
        {
            "time": r["trans_time"].strftime("%Y-%m-%d %H:%M"),
            "merchant": r["merchant"],
            "product": r["product"],
            "amount": float(r["amount"]),
        }
        for r in rows
    ]
    return jsonify(result)


@app.route("/budgets", methods=["GET", "POST"])
@login_required
def budgets():
    user_id = session["user_id"]
    month_key = request.args.get("month", now().strftime("%Y-%m"))
    if not re.match(r"^\d{4}-\d{2}$", month_key):
        month_key = now().strftime("%Y-%m")

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "delete":
            cat_id = request.form.get("category_id", "")
            with get_conn() as conn, conn.cursor() as cur:
                if cat_id == "total":
                    cur.execute(
                        "DELETE FROM budgets WHERE user_id=%s AND category_id IS NULL AND month_key=%s",
                        (user_id, month_key),
                    )
                elif cat_id.isdigit():
                    cur.execute(
                        "DELETE FROM budgets WHERE user_id=%s AND category_id=%s AND month_key=%s",
                        (user_id, int(cat_id), month_key),
                    )
                conn.commit()
            flash("预算已删除")
            return redirect(url_for("budgets", month=month_key))

        total_str = request.form.get("total_budget", "").strip()
        total = None
        if total_str:
            try:
                total = round(float(total_str), 2)
                if total <= 0:
                    raise ValueError
            except ValueError:
                flash("总预算金额不正确")
                return redirect(url_for("budgets", month=month_key))

        with get_conn() as conn, conn.cursor() as cur:
            if total is not None:
                cur.execute(
                    """
                    INSERT INTO budgets (user_id, category_id, amount, month_key)
                    VALUES (%s, NULL, %s, %s)
                    ON DUPLICATE KEY UPDATE amount = VALUES(amount)
                    """,
                    (user_id, total, month_key),
                )
            for cat_id in request.form.getlist("category_id"):
                amount_str = request.form.get("amount_" + cat_id, "").strip()
                if not amount_str:
                    continue
                try:
                    amount = round(float(amount_str), 2)
                    if amount <= 0:
                        raise ValueError
                except ValueError:
                    flash("分类预算金额不正确")
                    return redirect(url_for("budgets", month=month_key))
                cur.execute(
                    """
                    INSERT INTO budgets (user_id, category_id, amount, month_key)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE amount = VALUES(amount)
                    """,
                    (user_id, int(cat_id), amount, month_key),
                )
            conn.commit()
        flash("预算已保存")
        return redirect(url_for("budgets", month=month_key))

    budgets_map = _load_budgets(user_id, month_key)
    status, _ = _budget_status(user_id, month_key)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM categories WHERE type='expense' ORDER BY id")
        expense_cats = cur.fetchall()
    return render_template(
        "budgets.html",
        username=_display_name(),
        month_key=month_key,
        budgets=budgets_map,
        expense_cats=expense_cats,
        status=status or [],
    )


@app.route("/recurring", methods=["GET", "POST"])
@login_required
def recurring():
    user_id = session["user_id"]
    categories = load_categories()

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "delete":
            item_id = request.form.get("item_id", "0")
            if item_id.isdigit():
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM recurring_items WHERE id=%s AND user_id=%s",
                        (int(item_id), user_id),
                    )
                    conn.commit()
                flash("已删除固定收支")
            return redirect(url_for("recurring"))

        name = (request.form.get("name") or "").strip()
        income_expense = request.form.get("income_expense", "支出")
        amount_str = request.form.get("amount", "").strip()
        category_id_str = request.form.get("category_id", "").strip()
        merchant = (request.form.get("merchant") or "").strip() or "固定支出"
        day_str = request.form.get("day_of_month", "").strip()

        if not name:
            flash("请填写名称（如：房租）")
            return redirect(url_for("recurring"))
        if income_expense not in ("收入", "支出"):
            flash("收/支类型不正确")
            return redirect(url_for("recurring"))
        try:
            amount = round(float(amount_str), 2)
            if amount <= 0:
                raise ValueError
        except ValueError:
            flash("金额不正确")
            return redirect(url_for("recurring"))
        try:
            category_id = int(category_id_str)
        except ValueError:
            category_id = 0
        category = next((c for c in categories if c["id"] == category_id), None)
        expect_type = "income" if income_expense == "收入" else "expense"
        if not category or category["type"] != expect_type:
            flash("请选择有效的分类")
            return redirect(url_for("recurring"))
        try:
            day_of_month = int(day_str)
            if day_of_month < 1 or day_of_month > 28:
                raise ValueError
        except ValueError:
            flash("记账日需为 1-28 的整数")
            return redirect(url_for("recurring"))

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recurring_items
                    (user_id, name, income_expense, amount, category_id, merchant, day_of_month)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, name, income_expense, amount, category_id, merchant, day_of_month),
            )
            conn.commit()
        flash("已添加固定%s：%s %.2f 元（每月 %d 日）" % (
            "收入" if income_expense == "收入" else "支出", name, amount, day_of_month))
        run_recurring_checks(user_id)
        return redirect(url_for("recurring"))

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*, COALESCE(c.name, '未分类') AS category_name
            FROM recurring_items r
            LEFT JOIN categories c ON c.id = r.category_id
            WHERE r.user_id = %s
            ORDER BY r.active DESC, r.day_of_month, r.id
            """,
            (user_id,),
        )
        items = cur.fetchall()
    return render_template(
        "recurring.html",
        username=_display_name(),
        categories=categories,
        items=items,
    )


@app.route("/recurring/<int:item_id>/toggle", methods=["POST"])
@login_required
def recurring_toggle(item_id):
    user_id = session["user_id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE recurring_items SET active = 1 - active WHERE id = %s AND user_id = %s",
            (item_id, user_id),
        )
        conn.commit()
    return redirect(url_for("recurring"))


@app.route("/save", methods=["POST"])
@login_required
def save():
    user_id = session["user_id"]
    batch_id = session.get("pending_batch_id")
    records, filename, pending_user_id = load_pending_records(batch_id)
    if not records or pending_user_id != user_id:
        flash("没有待保存的数据，请重新上传")
        return redirect(url_for("index"))

    categories = {c["name"]: c["id"] for c in load_categories()}

    saved_count = 0
    skipped_count = 0

    with get_conn() as conn, conn.cursor() as cur:
        for i, rec in enumerate(records):
            chosen_category = request.form.get(f"category_{i}", rec["category"])
            category_id = categories.get(chosen_category)
            is_auto = chosen_category == rec["category"]

            try:
                cur.execute(
                    """
                    INSERT INTO transactions
                        (user_id, trans_time, trans_type, merchant, product, income_expense,
                         amount, pay_method, status, transaction_no, merchant_no, remark,
                         type, category_id, is_auto_categorized)
                    VALUES
                        (%(user_id)s, %(trans_time)s, %(trans_type)s, %(merchant)s, %(product)s,
                         %(income_expense)s, %(amount)s, %(pay_method)s, %(status)s,
                         %(transaction_no)s, %(merchant_no)s, %(remark)s,
                         %(type)s, %(category_id)s, %(is_auto)s)
                    """,
                    {
                        "user_id":        user_id,
                        "trans_time":     rec["trans_time"],
                        "trans_type":     rec["trans_type"],
                        "merchant":       rec["merchant"],
                        "product":        rec["product"],
                        "income_expense": rec["income_expense"],
                        "amount":         rec["amount"],
                        "pay_method":     rec["pay_method"],
                        "status":         rec["status"],
                        "transaction_no": rec["transaction_no"],
                        "merchant_no":    rec["merchant_no"],
                        "remark":         rec["remark"],
                        "type":           rec["type"],
                        "category_id":    category_id,
                        "is_auto":        is_auto,
                    },
                )
                saved_count += 1
            except Exception as e:
                if "Duplicate entry" in str(e):
                    skipped_count += 1
                    continue
                raise

        cur.execute(
            """
            INSERT INTO import_batches (user_id, filename, imported_count, imported_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, filename, saved_count, now()),
        )
        conn.commit()

    clear_pending_records(batch_id)
    session.pop("pending_batch_id", None)

    flash(f"导入完成：新增 {saved_count} 笔，跳过重复 {skipped_count} 笔")
    return redirect(url_for("index"))


if __name__ == "__main__":
    # 公网部署：优先用 waitress 生产服务器；没装 waitress 时退回 Flask 自带服务器（debug 关闭）
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "5000"))

    # 启动前先探一下 MySQL：连不上只提示、不退出（避免页面打不开时不知道往哪查）
    try:
        with get_conn() as conn:
            conn.ping()
        print("[OK] MySQL 连接正常")
    except Exception as e:
        print(f"[警告] MySQL 连接失败：{e}")
        print("       请确认 MySQL 服务已启动，且 wechat_finance 数据库已创建（见 db.py 配置）")

    # 启动 1 秒后自动打开浏览器（本机使用方便；手机访问请走内网穿透）
    import threading
    import webbrowser

    def _open_browser():
        webbrowser.open(f"http://127.0.0.1:{port}{URL_PREFIX}/")

    threading.Timer(1.0, _open_browser).start()

    print("=" * 52)
    print("  繁星记账本已启动")
    print(f"  本机访问:  http://127.0.0.1:{port}{URL_PREFIX}/")
    print("  停止服务:  在终端按 Ctrl+C")
    print("=" * 52)

    try:
        from waitress import serve
        serve(app, host=host, port=port)
    except ImportError:
        app.run(host=host, port=port, debug=False)

