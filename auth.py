"""用户注册 / 登录 / 登出 / 个人设置。

用 Flask session + werkzeug 密码哈希实现“一人一个账本账号”：
- 注册时把密码做单向哈希存进 users 表，不存明文；
- 昵称选填：不填时默认使用用户名；
- 登录后把 user_id / username / nickname 写进 session，后续请求都基于 session 里的用户；
- login_required 装饰器保护所有需要登录的页面。
"""
import re
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_conn

auth_bp = Blueprint("auth", __name__)

# 账号前缀：2-10 位，允许中文、字母、数字、下划线（注册后系统自动追加 @star）
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{2,10}$")
# 昵称：2-20 位，允许中文、字母、数字、下划线
NICKNAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{2,20}$")

# 统一账号后缀：注册时自动追加，登录时输入前缀或完整账号均可
ACCOUNT_SUFFIX = "@star"


def _account_prefix(raw):
    """把输入规整成账号前缀：去掉末尾的 @star（大小写不敏感），其余原样返回。"""
    name = (raw or "").strip()
    if name.lower().endswith(ACCOUNT_SUFFIX):
        name = name[: -len(ACCOUNT_SUFFIX)].strip()
    return name


def _full_account(raw):
    """把输入规整成完整账号：不带后缀自动补 @star，带后缀则去重。"""
    return _account_prefix(raw) + ACCOUNT_SUFFIX


def login_required(view):
    """装饰器：未登录就跳到登录页，登录后回到原来的页面。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("请先登录后再使用记账本")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _nickname_of(user):
    return user["nickname"] or user["username"]


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = _full_account(request.form.get("username"))
        password = request.form.get("password") or ""

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, nickname, password_hash FROM users WHERE username = %s",
                (username,),
            )
            user = cur.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()  # 换账号登录时清掉旧 session（包括旧的待导入批次）
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["nickname"] = _nickname_of(user)

            next_url = request.args.get("next") or url_for("index")
            if not next_url.startswith("/"):  # 防止开放重定向
                next_url = url_for("index")
            else:
                # 挂在子目录时，next 存的是去前缀后的路径，这里补回前缀
                root = request.script_root.rstrip("/")
                if root and not next_url.startswith(root + "/"):
                    next_url = root + next_url
            return redirect(next_url)

        flash("用户名或密码错误")
    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = _full_account(request.form.get("username"))
        prefix = _account_prefix(request.form.get("username"))
        nickname = (request.form.get("nickname") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if not USERNAME_RE.match(prefix):
            flash("请输入2~10个字符，该记账本账号会自动增加后缀@star")
        elif nickname and not NICKNAME_RE.match(nickname):
            flash("昵称需为 2-20 位中文、字母、数字或下划线（不填则默认用用户名）")
        elif len(password) < 4:
            flash("密码至少 4 位")
        elif password != confirm:
            flash("两次输入的密码不一致")
        else:
            nickname = nickname or prefix
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username = %s", (username,))
                if cur.fetchone():
                    flash("该用户名已被注册")
                else:
                    cur.execute(
                        "INSERT INTO users (username, nickname, password_hash) VALUES (%s, %s, %s)",
                        (username, nickname, generate_password_hash(password)),
                    )
                    conn.commit()
                    flash("注册成功，请登录")
                    return redirect(url_for("auth.login"))
    return render_template("register.html")


@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_id = session["user_id"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, nickname FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()

    if not user:
        session.clear()
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "nickname":
            nickname = (request.form.get("nickname") or "").strip()
            if not NICKNAME_RE.match(nickname):
                flash("昵称需为 2-20 位中文、字母、数字或下划线")
            else:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET nickname = %s WHERE id = %s",
                        (nickname, user_id),
                    )
                    conn.commit()
                session["nickname"] = nickname
                user["nickname"] = nickname
                flash("昵称已更新")
        elif action == "password":
            old_pwd = request.form.get("old_password") or ""
            new_pwd = request.form.get("new_password") or ""
            confirm_pwd = request.form.get("confirm_password") or ""

            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()

            if not row or not check_password_hash(row["password_hash"], old_pwd):
                flash("当前密码不正确")
            elif len(new_pwd) < 4:
                flash("新密码至少 4 位")
            elif new_pwd != confirm_pwd:
                flash("两次输入的新密码不一致")
            else:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET password_hash = %s WHERE id = %s",
                        (generate_password_hash(new_pwd), user_id),
                    )
                    conn.commit()
                flash("密码已更新")
        return redirect(url_for("auth.profile"))

    return render_template("profile.html", user=user, nickname=_nickname_of(user))


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("已退出登录")
    return redirect(url_for("auth.login"))
