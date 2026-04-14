"""
KINKY FLOWER STAND — Square 自動同期スクリプト
GitHub Actions から毎日実行。
・売上データ（Payments）を取得
・Square カタログのカテゴリを参照して coffee / flower を判定
  （カタログ名に頼れない場合はアイテム名キーワードにフォールバック）
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

# カテゴリ判定キーワード（フォールバック用・小文字）
# ★ 実際の Square カタログのカテゴリ名がここのキーワードに含まれるよう調整してください
COFFEE_KW = [
    'coffee', 'コーヒー', 'cafe', 'カフェ', 'drink', 'ドリンク',
    'tea', 'ティー', 'espresso', 'latte', 'ラテ', 'cappuccino',
    'americano', 'アメリカーノ', 'matcha', '抹茶', 'beverage',
    'ビバレッジ', '飲み物', 'chai', 'チャイ', 'cocoa', 'ホット', 'アイス',
    'smoothie', 'スムージー', 'juice', 'ジュース',
]
FLOWER_KW = [
    'flower', 'フラワー', '花', '花束', 'bouquet', 'ブーケ',
    'arrangement', 'アレンジ', 'plant', '植物', 'wreath', 'リース',
    'corsage', 'コサージュ', 'dried', 'ドライ', 'preserved', 'プリザ',
    'green', 'グリーン', '観葉', 'バラ', 'rose', 'tulip', 'チューリップ',
    'carnation', 'カーネーション', 'lily', 'ユリ', 'sunflower', 'ひまわり',
    'pansy', 'パンジー', '切り花', '切花', 'スワッグ', 'swag',
]

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


# ── Square カタログ取得（カテゴリ & アイテム）────────────
def fetch_catalog() -> tuple[dict, dict]:
    """
    Returns:
        category_map : { category_id -> 'coffee'|'flower'|'other' }
        item_cat_map : { item_id -> category_id }
    """
    category_map = {}  # square category_id -> our label
    item_cat_map = {}  # item_id -> category_id
    cursor = None
    page = 0

    print("⬇ Square カタログ取得中…")
    while page < 10:
        params = {"types": "CATEGORY,ITEM"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE_URL}/catalog/list", headers=HEADERS, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠ Catalog API エラー: {r.status_code}（カタログ取得スキップ）")
            break
        data = r.json()
        for obj in data.get("objects", []):
            if obj["type"] == "CATEGORY":
                name = obj.get("category_data", {}).get("name", "").lower()
                label = _classify_name(name)
                category_map[obj["id"]] = label
                print(f"  カテゴリ: [{obj['id'][:8]}] '{obj.get('category_data',{}).get('name','')}' → {label}")
            elif obj["type"] == "ITEM":
                item_data = obj.get("item_data", {})
                cat_id = item_data.get("category_id", "")
                if cat_id:
                    item_cat_map[obj["id"]] = cat_id
        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1

    print(f"  カテゴリ {len(category_map)}件、アイテム→カテゴリマップ {len(item_cat_map)}件 取得完了")
    return category_map, item_cat_map


def _classify_name(name: str) -> str:
    """名前文字列から coffee / flower / other を判定"""
    if any(k in name for k in COFFEE_KW):
        return "coffee"
    if any(k in name for k in FLOWER_KW):
        return "flower"
    return "other"


# ── Payments 全件取得 ─────────────────────────────────
def fetch_payments() -> list[dict]:
    begin = (jst_now() - timedelta(days=FETCH_DAYS)).strftime("%Y-%m-%dT00:00:00+09:00")
    payments, cursor, page = [], None, 0
    print(f"\n⬇ Square Payments 取得中（過去{FETCH_DAYS}日）…")

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
def detect_sq_category(
    order: dict | None,
    category_map: dict,
    item_cat_map: dict,
) -> str:
    """
    優先順位:
    1. line_item の catalog_object_id → item_cat_map → category_map で判定
    2. line_item のカテゴリ名（Square新形式）で判定
    3. アイテム名・バリエーション名のキーワードマッチ（フォールバック）
    """
    if not order:
        return "other"

    has_coffee = has_flower = False

    for item in order.get("line_items", []):
        label = "other"

        # ① カタログID経由（最も正確）
        cat_obj_id = item.get("catalog_object_id", "")
        if cat_obj_id and cat_obj_id in item_cat_map:
            cat_id = item_cat_map[cat_obj_id]
            label = category_map.get(cat_id, "other")

        # ② line_item 直下の category_id（Square 新形式）
        if label == "other":
            line_cat_id = item.get("category_id", "")
            if line_cat_id and line_cat_id in category_map:
                label = category_map[line_cat_id]

        # ③ アイテム名・バリエーション名のキーワードマッチ（フォールバック）
        if label == "other":
            name = (
                item.get("name", "") + " " + item.get("variation_name", "")
            ).lower()
            label = _classify_name(name)

        if label == "coffee":
            has_coffee = True
        elif label == "flower":
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

    # CASH: Square POSで現金決済した場合
    if src == "CASH":
        return "cash"

    # cash_details が存在する場合も現金扱い（念のため）
    if payment.get("cash_details"):
        return "cash"

    # EXTERNAL: Square外部決済（現金を外部決済として記録した場合）
    # ※ 外部決済名に「現金」「cash」が含まれる場合は現金扱い
    if src == "EXTERNAL":
        ext = payment.get("external_details", {})
        ext_type = ext.get("type", "").lower()
        ext_src  = ext.get("source", "").lower()
        if "cash" in ext_type or "cash" in ext_src or "現金" in ext_src:
            return "cash"

    return "other"  # CARD, WALLET, BANK_ACCOUNT など


# ── Payment → アプリ用フォーマット ───────────────────
def parse_payment(
    p: dict,
    orders: dict,
    category_map: dict,
    item_cat_map: dict,
) -> dict | None:
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
    sq_cat     = detect_sq_category(order, category_map, item_cat_map)
    pay_method = detect_payment_method(p)
    item_names = get_item_names(order)

    # 摘要
    note   = p.get("note", "").strip()
    desc   = note if note else ("、".join(item_names[:2]) if item_names else "Square売上")

    return {
        "squareId":       p["id"],
        "date":           date_str,
        "amount":         amount,
        "category":       "売上",
        "sq_category":    sq_cat,        # coffee / flower / mixed / other
        "payment_method": pay_method,    # cash / other
        "items":          item_names,
        "description":    desc[:60],
        "source":         "Square",
    }


# ── メイン ────────────────────────────────────────────
def main():
    # ① カタログ取得（カテゴリマッピング）
    category_map, item_cat_map = fetch_catalog()

    # ② Payments 取得
    raw_payments = fetch_payments()

    # ③ Order ID を収集してバッチ取得
    order_ids = [p["order_id"] for p in raw_payments if p.get("order_id")]
    print(f"\n⬇ Order 明細を取得中（{len(order_ids)}件）…")
    orders = fetch_orders(list(set(order_ids)))

    # ④ パース
    transactions = []
    for p in raw_payments:
        parsed = parse_payment(p, orders, category_map, item_cat_map)
        if parsed:
            transactions.append(parsed)

    transactions.sort(key=lambda x: x["date"], reverse=True)

    # ⑤ デバッグ: 実際のアイテム名サンプルを表示
    print("\n── アイテム名サンプル（最初の20件）──────────────")
    shown = 0
    for t in transactions:
        if t.get("items") and shown < 20:
            print(f"  [{t['date']}] {t['items']} → sq_cat:{t['sq_category']} pay:{t['payment_method']}")
            shown += 1

    # ⑥ 集計サマリー
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

    # ⑦ もし全て other の場合、アドバイスを表示
    if cats["coffee"] == 0 and cats["flower"] == 0:
        print("\n⚠ カテゴリが全て 'other' です。")
        print("  Square のカタログでカテゴリ名に 'coffee/コーヒー/ドリンク' または")
        print("  'flower/フラワー/花' が含まれているか確認してください。")
        print("  または sync_square.py の COFFEE_KW / FLOWER_KW リストに")
        print("  実際のアイテム名を追加してください。")

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
