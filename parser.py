"""解析微信 / 支付宝账单导出文件（csv 或 xlsx 都支持）。

微信导出的文件前面有十几行说明文字，不是数据，且不同版本行数可能不一样，
所以不能写死跳过几行，而是动态找到包含"交易时间"的那一行作为表头行。
支付宝导出的文件开头是 "-----------------支付宝交易记录明细查询-----------------"，
表头列名和微信不同（交易分类 / 对方账号 / 商品说明 / 收/付款方式 / 交易订单号 等），
通过 detect_bill_format() 自动识别格式后走对应的解析器。
"""
import pandas as pd
from category_rules import auto_categorize

EXPECTED_HEADER = "交易时间"


def _norm_order_no(value):
    """把长订单号归一化成字符串，避免 pandas 把 19 位订单号读成浮点/科学计数法丢精度。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    if s in ("", "nan", "None", "N/A"):
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _clean_text(value):
    """把 pandas 空值(NaN/None)归一成空字符串，避免 NaN 写进 MySQL 报错；其它值转字符串并去空白。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


# 表头列名 -> 我们数据库字段名 的映射
COLUMN_MAP = {
    "交易时间": "trans_time",
    "交易类型": "trans_type",
    "交易对方": "merchant",
    "商品": "product",
    "收/支": "income_expense",
    "金额(元)": "amount",
    "支付方式": "pay_method",
    "当前状态": "status",
    "交易单号": "transaction_no",
    "商户单号": "merchant_no",
    "备注": "remark",
}


def _find_header_row_csv(file_path):
    with open(file_path, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            if EXPECTED_HEADER in line:
                return i
    raise ValueError(f"没有在文件里找到表头（包含'{EXPECTED_HEADER}'的那一行）")


def _find_header_row_excel(file_path):
    # 只读前 30 行找表头就够了，不用整份加载
    preview = pd.read_excel(file_path, header=None, nrows=30)
    for i, row in preview.iterrows():
        if row.astype(str).str.contains(EXPECTED_HEADER).any():
            return i
    raise ValueError(f"没有在文件里找到表头（包含'{EXPECTED_HEADER}'的那一行）")


def parse_wechat_bill(file_path: str, user_id: int, extra_rules: dict | None = None):
    """返回一个 list[dict]，每个 dict 对应 transactions 表的一行（不含 id / user_id 会在存库时加上）。"""
    is_excel = file_path.lower().endswith((".xlsx", ".xls"))

    if is_excel:
        header_row = _find_header_row_excel(file_path)
        df = pd.read_excel(file_path, skiprows=header_row)
    else:
        header_row = _find_header_row_csv(file_path)
        df = pd.read_csv(file_path, skiprows=header_row, encoding="utf-8-sig")

    df = df.rename(columns=COLUMN_MAP)
    # 去掉可能存在的空行/汇总行
    df = df.dropna(subset=["transaction_no"])

    records = []
    for _, row in df.iterrows():
        if pd.isna(row.get("trans_time")):
            continue
        amount_str = str(row["amount"]).replace("¥", "").replace(",", "").strip()
        try:
            amount = float(amount_str)
        except ValueError:
            continue  # 跳过异常行

        income_expense_raw = _clean_text(row["income_expense"])

        # 微信导出文件里"收/支"这一列的原始取值实际只有三种：收入 / 支出 / "/"
        # （"/" 代表中性交易，比如充值、提现、理财通、信用卡还款等）。
        # 数据库 income_expense 字段是 ENUM('收入','支出','中性交易')，不认识"/"这个原始值，
        # 直接把 "/" 存进去会导致 MySQL 报错：Data truncated for column 'income_expense'。
        # 所以这里要把原始值归一化成这三个合法取值之一，而不是原样透传。
        if income_expense_raw == "收入":
            income_expense = "收入"
        elif income_expense_raw == "支出":
            income_expense = "支出"
        else:
            income_expense = "中性交易"

        # 收/支 -> income/expense：
        # 中性交易（充值、提现、理财通、信用卡还款等）按你的要求一律算 expense
        if income_expense == "收入":
            trans_type_dir = "income"
        else:
            trans_type_dir = "expense"

        merchant = _clean_text(row.get("merchant"))
        product = _clean_text(row.get("product"))

        # ★ 关键修复：把"交易类型"也传给 auto_categorize
        # 这样"转账"、"微信红包"、"商户消费"等交易类型也能参与分类匹配，
        # 大幅减少 product="/" 或 merchant 为工商全称导致的"未分类"
        trans_type_raw = _clean_text(row.get("trans_type"))

        transaction_no = _norm_order_no(row["transaction_no"])
        if not transaction_no:
            continue

        records.append({
            "trans_time":     row["trans_time"],
            "trans_type":     trans_type_raw,
            "merchant":       merchant,
            "product":        product,
            "income_expense": income_expense,
            "amount":         amount,
            "pay_method":     _clean_text(row.get("pay_method")),
            "status":         _clean_text(row.get("status")),
            "transaction_no": transaction_no,
            "merchant_no":    _norm_order_no(row.get("merchant_no", "") or ""),
            "remark":         _clean_text(row.get("remark")),
            "type":           trans_type_dir,
            # ★ 新增 trans_type 参数，让分类更准确
            "category":       auto_categorize(merchant, product, extra_rules, trans_type=trans_type_raw),
            "is_auto_categorized": True,
        })

    return records
# ============================================================
# 支付宝账单
# ============================================================

# 支付宝表头列名优先级 -> 数据库字段名（兼容电脑版 / 手机版两种导出格式）
# 注意：同一个字段可能出现在多个不同名字的列里（如时间有"交易时间/交易创建时间/付款时间"），
# 所以解析时按优先级"每个字段只选一列"，避免 pandas 重命名后出现重复列。
ALIPAY_COLUMN_PICKS = {
    "trans_time":      ["交易时间", "交易创建时间", "付款时间"],
    "trans_type":      ["交易类型", "交易分类", "消费分类", "类型"],
    "merchant":        ["交易对方"],
    "product":         ["商品说明", "商品名称"],
    "income_expense":  ["收/支"],
    "amount":          ["金额", "金额（元）", "金额(元)"],
    "pay_method":      ["收/付款方式", "付款方式"],
    "status":          ["交易状态", "当前状态"],
    "transaction_no":  ["交易订单号", "交易号"],
    "merchant_no":     ["商户订单号", "商家订单号"],
    "remark":          ["备注"],
}


def _build_alipay_rename_map(columns):
    """按优先级为每个目标字段选一列，返回 {原列名: 目标字段名}。"""
    rename_map = {}
    for target, sources in ALIPAY_COLUMN_PICKS.items():
        for s in sources:
            if s in columns:
                rename_map[s] = target
                break
    return rename_map

_ALIPAY_MARKERS = ("支付宝交易记录明细",)
_WECHAT_MARKERS = ("微信支付账单明细",)


def detect_bill_format(file_path: str) -> str:
    """根据文件内容判断是微信还是支付宝账单，返回 'wechat' / 'alipay'。"""
    head = ""
    # 严格编码依次尝试：utf-8-sig -> gbk -> gb18030。
    # 之前用 errors="replace" 会把 GBK 编码的支付宝文件读成乱码导致误判成微信，这里改严格模式逐级降级。
    for enc in ("utf-8-sig", "gbk", "gb18030"):
        try:
            with open(file_path, "r", encoding=enc) as f:
                head = f.read(4000)
            break
        except Exception:
            continue
    if head:
        if any(m in head for m in _ALIPAY_MARKERS):
            return "alipay"
        if any(m in head for m in _WECHAT_MARKERS):
            return "wechat"
        # 表头列名兜底判断
        if "交易分类" in head or "对方账号" in head or "商品说明" in head:
            return "alipay"
        if "交易单号" in head and "当前状态" in head:
            return "wechat"

    # Excel 是二进制，读不出文本时用 pandas 嗅探表头列名
    try:
        import pandas as pd
        preview = pd.read_excel(file_path, header=None, nrows=30)
        headers = set()
        for _, row in preview.iterrows():
            for cell in row.astype(str):
                headers.add(cell.strip())
        if "交易分类" in headers or "对方账号" in headers or "商品说明" in headers:
            return "alipay"
        if "交易单号" in headers or "支付方式" in headers:
            return "wechat"
    except Exception:
        pass
    # 默认按微信处理（保留旧行为），让原解析器报"找不到表头"
    return "wechat"


def _find_header_row_alipay_csv(file_path):
    """支付宝 CSV 表头行：包含"收/支"且包含"金额"的那一行。"""
    encodings = ["utf-8-sig", "gbk", "gb18030"]
    last_err = None
    for enc in encodings:
        try:
            with open(file_path, encoding=enc) as f:
                for i, line in enumerate(f):
                    if "收/支" in line and "金额" in line:
                        return i
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise ValueError("没有在文件里找到支付宝表头（包含'收/支'和'金额'的那一行）")


def _read_alipay_csv(file_path, skiprows):
    for enc in ["utf-8-sig", "gbk", "gb18030"]:
        try:
            # dtype=str：支付宝订单号是 19 位长数字，让 pandas 当文本读，避免转成浮点丢精度
            return pd.read_csv(file_path, skiprows=skiprows, encoding=enc, dtype=str)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(file_path, skiprows=skiprows, encoding="utf-8-sig", dtype=str)


def _find_header_row_alipay_excel(file_path):
    preview = pd.read_excel(file_path, header=None, nrows=30)
    for i, row in preview.iterrows():
        cells = [str(c) for c in row.astype(str)]
        if any("收/支" in c for c in cells) and any("金额" in c for c in cells):
            return i
    raise ValueError("没有在文件里找到支付宝表头（包含'收/支'和'金额'的那一行）")


def parse_alipay_bill(file_path: str, user_id: int, extra_rules: dict | None = None):
    """解析支付宝账单，返回与 parse_wechat_bill 相同结构的 list[dict]。"""
    is_excel = file_path.lower().endswith((".xlsx", ".xls"))

    if is_excel:
        header_row = _find_header_row_alipay_excel(file_path)
        df = pd.read_excel(file_path, skiprows=header_row)
    else:
        header_row = _find_header_row_alipay_csv(file_path)
        df = _read_alipay_csv(file_path, skiprows=header_row)

    df = df.rename(columns=_build_alipay_rename_map(df.columns))
    # 去掉空行 / 汇总行 / "查询结果共 xx 条" / 结束标记等杂行
    df = df.dropna(subset=["transaction_no"])

    records = []
    for _, row in df.iterrows():
        if pd.isna(row.get("trans_time")):
            continue
        amount_str = str(row.get("amount", "") or "").replace("¥", "").replace(",", "").strip()
        try:
            amount = float(amount_str)
        except ValueError:
            continue  # 跳过异常行

        income_expense_raw = _clean_text(row.get("income_expense"))
        if income_expense_raw in ("收入", "收入（+）", "+"):
            income_expense = "收入"
        elif income_expense_raw in ("支出", "支出（-）", "-"):
            income_expense = "支出"
        elif income_expense_raw in ("不计收支", "不计收支（0）", "0"):
            income_expense = "中性交易"
        else:
            # 个别导出没有收/支列，用金额正负判断
            income_expense = "支出" if amount < 0 else "收入"
        amount = abs(amount)

        if income_expense == "收入":
            trans_type_dir = "income"
        else:
            trans_type_dir = "expense"

        merchant = _clean_text(row.get("merchant"))
        product = _clean_text(row.get("product"))
        trans_type_raw = _clean_text(row.get("trans_type"))

        transaction_no = _norm_order_no(row["transaction_no"])
        if not transaction_no:
            continue

        records.append({
            "trans_time":     row["trans_time"],
            "trans_type":     trans_type_raw,
            "merchant":       merchant,
            "product":        product,
            "income_expense": income_expense,
            "amount":         amount,
            "pay_method":     _clean_text(row.get("pay_method")),
            "status":         _clean_text(row.get("status")),
            "transaction_no": transaction_no,
            "merchant_no":    _norm_order_no(row.get("merchant_no", "") or ""),
            "remark":         _clean_text(row.get("remark")),
            "type":           trans_type_dir,
            "category":       auto_categorize(merchant, product, extra_rules, trans_type=trans_type_raw),
            "is_auto_categorized": True,
        })

    return records
