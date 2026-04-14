"""
KINKY FLOWER STAND — Square 自動同期スクリプト
GitHub Actions から毎日実行され、Square の売上データを
data/square_data.json に書き出します。
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
FETCH_DAYS    = 180   # 過去何日分を取得するか（最大）
MAX_PAGES     = 20    # ページネーション上限（安全弁）
# ──────────────────────────────────────────────────────

if not ACCESS_TOKEN:
    print("❌ SQUARE_ACCESS_TOKEN が設定されていません")
    print("   GitHub → Settings → Secrets → SQUARE_ACCESS_TOKEN を追加してください")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type":  "application/json",
    "Square-Version": SQUARE_VER,
}

JST = timezone(timedelta(hours=9))


def jst_now() -> datetime:
    return datetime.now(JST)


def fetch_payments() -> list[dict]:
    """Square /v2/payments を全ページ取得して返す"""
    begin_time = (jst_now() - timedelta(days=FETCH_DAYS)).strftime("%Y-%m-%dT00:00:00+09:00")
    payments   = []
    cursor     = None
    page       = 0

    print(f"Square から {FETCH_DAYS} 日分の売上を取得中...")

    while page < MAX_PAGES:
        params: dict = {
            "begin_time": begin_time,
            "sort_order": "DESC",
            "limit":      100,
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(f"{BASE_URL}/payments", headers=HEADERS, params=params, timeout=30)

        if resp.status_code == 401:
            print("❌ アクセストークンが無効です。Square Developer ページで確認してください")
            sys.exit(1)
        if resp.status_code != 200:
            print(f"❌ Square API エラー: {resp.status_code} {resp.text[:200]}")
            sys.exit(1)

        data = resp.json()
        batch = data.get("payments", [])
        payments.extend(batch)
        print(f"  ページ {page + 1}: {len(batch)} 件取得（累計 {len(payments)} 件）")

        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1

    return payments


def parse_payment(p: dict) -> dict | None:
    """Square の payment オブジェクトをアプリ用フォーマットに変換"""
    # 完了済みのみ対象
    if p.get("status") not in ("COMPLETED", "APPROVED"):
        return None

    # 金額（JPY は amount がそのまま円）
    amount_obj = p.get("amount_money") or p.get("total_money") or {}
    currency   = amount_obj.get("currency", "JPY")
    raw_amount = amount_obj.get("amount", 0)

    # JPY 以外の通貨は100で割る（円ベースに統一）
    amount = raw_amount if currency == "JPY" else raw_amount / 100

    if amount <= 0:
        return None

    # 日付（JST に変換）
    created_at = p.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(JST)
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        date_str = created_at[:10]

    # 摘要
    note       = p.get("note", "").strip()
    order_id   = p.get("order_id", "")
    card_brand = p.get("card_details", {}).get("card", {}).get("card_brand", "")
    desc_parts = [x for x in [note, card_brand] if x]
    description = "、".join(desc_parts) if desc_parts else "Square売上"

    return {
        "squareId":   p["id"],
        "date":       date_str,
        "amount":     amount,        # 正数 = 収入
        "category":   "売上",
        "description": description,
        "source":     "Square",
    }


def main():
    raw_payments = fetch_payments()
    transactions = []

    for p in raw_payments:
        parsed = parse_payment(p)
        if parsed:
            transactions.append(parsed)

    # 日付降順でソート
    transactions.sort(key=lambda x: x["date"], reverse=True)

    output = {
        "updated_at":   jst_now().isoformat(),
        "count":        len(transactions),
        "transactions": transactions,
    }

    os.makedirs("data", exist_ok=True)
    out_path = "data/square_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ {len(transactions)} 件を {out_path} に保存しました")
    print(f"   最終同期: {output['updated_at']}")


if __name__ == "__main__":
    main()
