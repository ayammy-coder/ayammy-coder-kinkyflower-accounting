"""
KINKY FLOWER STAND — Square 自動同期スクリプト
GitHub Actions から毎日実行。
・売上データ（Payments）を取得
・Square カタログのカテゴリ名（FLOWER / COFFEE / 未分類 等）を参照
・アイテム単位で金額をカテゴリに分類（会計ベースではなくアイテムベース）
・決済方法（現金 / その他）を判定
→ data/square_data.json に書き出します
"""

import os
import json
import sys
import requests
from datetime import datetime, timedelta, timezone

# ── 設定 ──────────────────────────────────────────────
ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "")
BASE_URL      = "https://connect.squareup.com/v2"
SQUARE_VER    = "2024-01-17"
FETCH_DAYS    = 180   # 過去何日分を取得するか
MAX_PAGES     = 20    # ページネーション上限
ORDER_BATCH   = 100   # Order バッチ取得の上限
# ──────────────────────────────────────────────────────

# カテゴリ名 → flower / coffee / other マッピング
# Square のカタログカテゴリ名（小文字）がこのキーワードに含まれるか判定
FLOWER_KW = ['flower', 'フラワー', '花']
COFFEE_KW = ['coffee', 'コーヒー', 'cafe', 'カフェ', 'drink', 'ドリンク', 'beverage']

if not ACCESS_TOKEN:
    print("❌ SQUARE_ACCESS_TOKEN が設定されていません")
    sys.exit(1)

HEADERS = {
    "Authorization":  f"Bearer {ACCESS_TOKEN}",
    "Content-Type":   "application/json",
    "Square-Version": SQUARE_VER,
}
JST = timezone(timedelta(hours=9))


def jst_now() -> datetime:
    return datetime.now(JST)


def classify_cat_name(name: str) -> str:
    """カテゴリ名から flower / coffee / other を判定（小文字で比較）"""
    n = name.lower()
    if any(k in n for k in FLOWER_KW):
        return "flower"
    if any(k in n for k in COFFEE_KW):
        return "coffee"
    return "other"


# ── Square カタログ取得 ───────────────────────────────
def fetch_catalog() -> tuple[dict, dict]:
    """
    Returns:
        cat_label  : { category_id  -> 'flower'|'coffee'|'other' }
        item_to_cat: { item_id      -> category_id }
    """
    cat_label   = {}  # category_id -> label
    item_to_cat = {}  # item_id     -> category_id
    cursor = None
    page   = 0

    print("⬇ Square カタログ取得中…")
    while page < 10:
        params = {"types": "CATEGORY,ITEM"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE_URL}/catalog/list", headers=HEADERS,
                         params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠ Catalog API エラー: {r.status_code}（スキップ）")
            break
        data = r.json()
        for obj in data.get("objects", []):
            if obj["type"] == "CATEGORY":
                raw_name = obj.get("category_data", {}).get("name", "")
                label    = classify_cat_name(raw_name)
                cat_label[obj["id"]] = label
                print(f"  カテゴリ: '{raw_name}' → {label}  [{obj['id'][:8]}]")
            elif obj["type"] == "ITEM":
                item_data = obj.get("item_data", {})
                cat_id    = item_data.get("category_id", "")
                if cat_id:
                    item_to_cat[obj["id"]] = cat_id
        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1

    print(f"  カテゴリ {len(cat_label)}件、アイテム→カテゴリ {len(item_to_cat)}件")
    return cat_label, item_to_cat


# ── Payments 全件取得 ─────────────────────────────────
def fetch_payments() -> list[dict]:
    begin = (jst_now() - timedelta(days=FETCH_DAYS)).strftime(
        "%Y-%m-%dT00:00:00+09:00")
    payments, cursor, page = [], None, 0
    print(f"\n⬇ Square Payments 取得中（過去{FETCH_DAYS}日）…")

    while page < MAX_PAGES:
        params = {"begin_time": begin, "sort_order": "DESC", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE_URL}/payments", headers=HEADERS,
                         params=params, timeout=30)
        if r.status_code == 401:
            print("❌ アクセストークンが無効です"); sys.exit(1)
        if r.status_code != 200:
            print(f"❌ Payments API エラー: {r.status_code}"); sys.exit(1)
        data   = r.json()
        batch  = data.get("payments", [])
        payments.extend(batch)
        print(f"  ページ{page+1}: {len(batch)}件（累計{len(payments)}件）")
        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1
    return payments


# ── Orders バッチ取得 ─────────────────────────────────
def fetch_orders(order_ids: list[str]) -> dict[str, dict]:
    orders = {}
    for i in range(0, len(order_ids), ORDER_BATCH):
        batch = order_ids[i:i + ORDER_BATCH]
        r = requests.post(f"{BASE_URL}/orders/batch-retrieve", headers=HEADERS,
                          json={"order_ids": batch}, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠ Orders API エラー: {r.status_code}（スキップ）")
            continue
        for order in r.json().get("orders", []):
            orders[order["id"]] = order
        print(f"  注文明細 {i//ORDER_BATCH+1}バッチ: {len(r.json().get('orders',[]))}件")
    return orders


# ── アイテム単位カテゴリ集計 ─────────────────────────
def calc_item_breakdown(
    order: dict | None,
    cat_label: dict,
    item_to_cat: dict,
) -> dict:
    """
    order の line_items を 1件ずつ見て、
    { flower: ¥xxx, coffee: ¥xxx, other: ¥xxx } を返す（アイテム金額ベース）
    """
    breakdown = {"flower": 0, "coffee": 0, "other": 0}
    if not order:
        return breakdown

    for item in order.get("line_items", []):
        # アイテム金額（unit_price × quantity）
        price_obj  = item.get("base_price_money") or item.get("gross_sales_money") or {}
        currency   = price_obj.get("currency", "JPY")
        unit_price = price_obj.get("amount", 0)
        unit_price = unit_price if currency == "JPY" else unit_price / 100

        try:
            qty = float(item.get("quantity", "1"))
        except (ValueError, TypeError):
            qty = 1.0

        item_total = unit_price * qty

        # カテゴリ判定 ①: catalog_object_id → item_to_cat → cat_label
        label = "other"
        cat_obj_id = item.get("catalog_object_id", "")
        if cat_obj_id and cat_obj_id in item_to_cat:
            cat_id = item_to_cat[cat_obj_id]
            label  = cat_label.get(cat_id, "other")

        # カテゴリ判定 ②: line_item 直下の category_id（新形式）
        if label == "other":
            line_cat_id = item.get("category_id", "")
            if line_cat_id and line_cat_id in cat_label:
                label = cat_label[line_cat_id]

        # カテゴリ判定 ③: アイテム名でフォールバック
        if label == "other":
            name  = (item.get("name", "") + " " + item.get("variation_name", "")).lower()
            label = classify_cat_name(name)

        breakdown[label] = breakdown.get(label, 0) + item_total

    # 小数点丸め
    return {k: round(v) for k, v in breakdown.items()}


def dominant_cat(breakdown: dict) -> str:
    """内訳で最大金額のカテゴリを返す（後方互換用 sq_category）"""
    if not any(breakdown.values()):
        return "other"
    return max(breakdown, key=lambda k: breakdown[k])


def get_item_names(order: dict | None) -> list[str]:
    if not order:
        return []
    return [item.get("name", "") for item in order.get("line_items", [])
            if item.get("name")]


# ── 決済方法判定 ─────────────────────────────────────
def detect_payment_method(payment: dict) -> str:
    src = payment.get("source_type", "")
    if src == "CASH" or payment.get("cash_details"):
        return "cash"
    # EXTERNAL で「現金」扱いの場合
    if src == "EXTERNAL":
        ext     = payment.get("external_details", {})
        ext_src = ext.get("source", "").lower()
        if "cash" in ext_src or "現金" in ext_src:
            return "cash"
    return "other"


# ── Payment → アプリ用フォーマット ───────────────────
def parse_payment(p: dict, orders: dict, cat_label: dict, item_to_cat: dict) -> dict | None:
    if p.get("status") not in ("COMPLETED", "APPROVED"):
        return None

    amt_obj  = p.get("amount_money") or p.get("total_money") or {}
    currency = amt_obj.get("currency", "JPY")
    raw_amt  = amt_obj.get("amount", 0)
    amount   = raw_amt if currency == "JPY" else raw_amt / 100
    if amount <= 0:
        return None

    try:
        dt = datetime.fromisoformat(
            p.get("created_at", "").replace("Z", "+00:00")).astimezone(JST)
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        date_str = p.get("created_at", "")[:10]

    order      = orders.get(p.get("order_id", ""))
    breakdown  = calc_item_breakdown(order, cat_label, item_to_cat)
    sq_cat     = dominant_cat(breakdown)
    pay_method = detect_payment_method(p)
    item_names = get_item_names(order)

    note = p.get("note", "").strip()
    desc = note if note else ("、".join(item_names[:2]) if item_names else "Square売上")

    return {
        "squareId":        p["id"],
        "date":            date_str,
        "amount":          amount,
        "category":        "売上",
        "sq_category":     sq_cat,        # 後方互換（最大カテゴリ）
        "sq_breakdown":    breakdown,      # ★ アイテム単位内訳 {flower, coffee, other}
        "payment_method":  pay_method,
        "items":           item_names,
        "description":     desc[:60],
        "source":          "Square",
    }


# ── メイン ────────────────────────────────────────────
def main():
    cat_label, item_to_cat = fetch_catalog()
    raw_payments = fetch_payments()

    order_ids = [p["order_id"] for p in raw_payments if p.get("order_id")]
    print(f"\n⬇ Order 明細を取得中（{len(order_ids)}件）…")
    orders = fetch_orders(list(set(order_ids)))

    transactions = []
    for p in raw_payments:
        parsed = parse_payment(p, orders, cat_label, item_to_cat)
        if parsed:
            transactions.append(parsed)

    transactions.sort(key=lambda x: x["date"], reverse=True)

    # ── デバッグ: アイテム名サンプル ──
    print("\n── アイテム名サンプル（最初の20件）──────────────")
    for t in transactions[:20]:
        if t.get("items"):
            print(f"  [{t['date']}] {t['items']} → breakdown:{t['sq_breakdown']} pay:{t['payment_method']}")

    # ── 月次集計サマリー ──
    totals = {"flower": 0, "coffee": 0, "other": 0}
    pays   = {"cash": 0, "other": 0}
    for t in transactions:
        for k, v in t.get("sq_breakdown", {}).items():
            totals[k] = totals.get(k, 0) + v
        pays[t["payment_method"]] = pays.get(t["payment_method"], 0) + t["amount"]

    print("\n── カテゴリ別売上（アイテムベース）──────────────")
    for k, v in totals.items():
        print(f"  {k:8s}: ¥{v:,.0f}")
    print("── 決済方法別 ──────────────────────────────────")
    for k, v in pays.items():
        print(f"  {k:8s}: ¥{v:,.0f}")

    if totals["flower"] == 0 and totals["coffee"] == 0:
        print("\n⚠ Flower / Coffee の売上が 0 です。")
        print("  Square カタログのカテゴリ名を確認してください。")
        print("  カテゴリ名に 'flower/FLOWER/花' または 'coffee/COFFEE' が含まれていれば自動認識されます。")

    output = {
        "updated_at":   jst_now().isoformat(),
        "count":        len(transactions),
        "transactions": transactions,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/square_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ {len(transactions)}件 → data/square_data.json に保存完了")


if __name__ == "__main__":
    main()
