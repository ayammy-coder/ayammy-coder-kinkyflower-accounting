"""
KINKY FLOWER STAND — Square 自動同期スクリプト
GitHub Actions から毎日実行。
・売上データ（Payments）を取得
・Order 明細から「coffee」「flower」カテゴリを判定
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

# カテゴリ判定キーワード（小文字）
COFFEE_KW = ['coffee', 'コーヒー', 'cafe', 'カフェ', 'drink', 'ドリンク',
             'tea', 'ティー', 'espresso', 'latte', 'ラテ', 'cappuccino',
             'americano', 'アメリカーノ', 'matcha', '抹茶', 'beverage']
FLOWER_KW = ['flower', 'フラワー', '花', '花束', 'bouquet', 'ブーケ',
             'arrangement', 'アレンジ', 'plant', '植物', 'wreath', 'リース',
             'corsage', 'コサージュ', 'dried', 'ドライ', 'preserved', 'プリザ']

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


# ── Payments 全件取得 ─────────────────────────────────
def fetch_payments() -> list[dict]:
    begin = (jst_now() - timedelta(days=FETCH_DAYS)).strftime("%Y-%m-%dT00:00:00+09:00")
    payments, cursor, page = [], None, 0
    print(f"⬇ Square Payments 取得中（過去{FETCH_DAYS}日）…")

    while page < MAX_PAGES:
        params = {"begin_time": begin, "sort_order": "DESC", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE_URL}/payments", headers=HEADERS, params=params, timeout=30)
        if r.status_code == 401:
            print("❌ アクセストークンが無効です"); sys.exit(1)
        if r.status_code != 200:
            print(f"❌ Payments API エラー: {r.status_code}"); sys.exit(1)
        data = r.json()
        batch = data.get("payments", [])
        payments.extend(batch)
        print(f"  ページ{page+1}: {len(batch)}件（累計{len(payments)}件）")
        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1
    return payments


# ── Orders バッチ取得 ─────────────────────────────────
def fetch_orders(order_ids: list[str]) -> dict[str, dict]:
    """order_id → order オブジェクト のマップを返す"""
    orders = {}
    for i in range(0, len(order_ids), ORDER_BATCH):
        batch = order_ids[i:i + ORDER_BATCH]
        r = requests.post(
            f"{BASE_URL}/orders/batch-retrieve",
            headers=HEADERS,
            json={"order_ids": batch},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  ⚠ Orders API エラー: {r.status_code}（スキップ）")
            continue
        for order in r.json().get("orders", []):
            orders[order["id"]] = order
        print(f"  注文明細 {i//ORDER_BATCH+1}バッチ: {len(r.json().get('orders',[]))}件")
    return orders


# ── カテゴリ判定 ─────────────────────────────────────
def detect_sq_category(order: dict | None) -> str:
    """注文明細のアイテム名から coffee / flower / mixed / other を返す"""
    if not order:
        return "other"
    has_coffee = has_flower = False
    for item in order.get("line_items", []):
        name = (
            item.get("name", "") + " " + item.get("variation_name", "")
        ).lower()
        if any(k in name for k in COFFEE_KW):
            has_coffee = True
        if any(k in name for k in FLOWER_KW):
            has_flower = True
    if has_coffee and has_flower:
        return "mixed"
    if has_coffee:
        return "coffee"
    if has_flower:
        return "flower"
    return "other"


def get_item_names(order: dict | None) -> list[str]:
    """注文に含まれるアイテム名一覧"""
    if not order:
        return []
    return [
        item.get("name", "") for item in order.get("line_items", []) if item.get("name")
    ]


# ── 決済方法判定 ─────────────────────────────────────
def detect_payment_method(payment: dict) -> str:
    """現金 / その他（カード・電子マネー等）"""
    src = payment.get("source_type", "")
    if src == "CASH":
        return "cash"
    return "other"  # CARD, WALLET, EXTERNAL など


# ── Payment → アプリ用フォーマット ───────────────────
def parse_payment(p: dict, orders: dict) -> dict | None:
    if p.get("status") not in ("COMPLETED", "APPROVED"):
        return None

    # 金額
    amt_obj  = p.get("amount_money") or p.get("total_money") or {}
    currency = amt_obj.get("currency", "JPY")
    raw_amt  = amt_obj.get("amount", 0)
    amount   = raw_amt if currency == "JPY" else raw_amt / 100
    if amount <= 0:
        return None

    # 日付
    try:
        dt = datetime.fromisoformat(
            p.get("created_at", "").replace("Z", "+00:00")
        ).astimezone(JST)
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        date_str = p.get("created_at", "")[:10]

    # 注文明細
    order      = orders.get(p.get("order_id", ""))
    sq_cat     = detect_sq_category(order)
    pay_method = detect_payment_method(p)
    item_names = get_item_names(order)

    # 摘要
    note   = p.get("note", "").strip()
    desc   = note if note else ("、".join(item_names[:2]) if item_names else "Square売上")

    return {
        "squareId":      p["id"],
        "date":          date_str,
        "amount":        amount,
        "category":      "売上",
        "sq_category":   sq_cat,        # coffee / flower / mixed / other
        "payment_method": pay_method,   # cash / other
        "items":         item_names,
        "description":   desc[:60],
        "source":        "Square",
    }


# ── メイン ────────────────────────────────────────────
def main():
    raw_payments = fetch_payments()

    # Order ID を収集してバッチ取得
    order_ids = [p["order_id"] for p in raw_payments if p.get("order_id")]
    print(f"\n⬇ Order 明細を取得中（{len(order_ids)}件）…")
    orders = fetch_orders(list(set(order_ids)))

    # パース
    transactions = []
    for p in raw_payments:
        parsed = parse_payment(p, orders)
        if parsed:
            transactions.append(parsed)

    transactions.sort(key=lambda x: x["date"], reverse=True)

    # 集計サマリー（確認用）
    cats = {"coffee": 0, "flower": 0, "mixed": 0, "other": 0}
    pays = {"cash": 0, "other": 0}
    for t in transactions:
        cats[t["sq_category"]] = cats.get(t["sq_category"], 0) + t["amount"]
        pays[t["payment_method"]] = pays.get(t["payment_method"], 0) + t["amount"]

    print(f"\n── カテゴリ別売上 ──────────────────")
    for k, v in cats.items():
        print(f"  {k:8s}: ¥{v:,.0f}")
    print(f"── 決済方法別 ──────────────────────")
    for k, v in pays.items():
        print(f"  {k:8s}: ¥{v:,.0f}")

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
