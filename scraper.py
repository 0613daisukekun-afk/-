"""
drone_scraper.py
================
Google News RSS からドローン事故ニュースを取得し、
data.json に追記・上書き保存するスクリプト。

依存ライブラリ (requirements.txt に記述すること):
    feedparser>=6.0
    requests>=2.31
"""

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

# ─── 設定 ──────────────────────────────────────────────────────────────────────

LOG_LEVEL = logging.INFO
OUTPUT_FILE = Path(__file__).parent / "data.json"
MAX_ITEMS_PER_QUERY = 30   # 1クエリあたりの最大取得件数
DEDUP_DAYS = 180           # この日数以内の記事のみ保持
REQUEST_INTERVAL = 2.0     # RSS取得間の待機秒数

QUERIES = [
    # --- 国内 ---
    {"q": "ドローン 事故",     "type": "domestic",       "lang": "ja", "region": "JP"},
    {"q": "ドローン 墜落",     "type": "domestic",       "lang": "ja", "region": "JP"},
    {"q": "ドローン 衝突",     "type": "domestic",       "lang": "ja", "region": "JP"},
    {"q": "無人機 事故",       "type": "domestic",       "lang": "ja", "region": "JP"},
    # --- 国外 ---
    {"q": "drone accident",    "type": "international",  "lang": "en", "region": "US"},
    {"q": "UAV crash",         "type": "international",  "lang": "en", "region": "US"},
    {"q": "drone incident",    "type": "international",  "lang": "en", "region": "US"},
    {"q": "drone collision",   "type": "international",  "lang": "en", "region": "US"},
]

GNEWS_RSS_TEMPLATE = (
    "https://news.google.com/rss/search"
    "?q={query}&hl={lang}&gl={region}&ceid={region}:{lang}"
)

# ─── ユーティリティ ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_date(entry) -> str:
    """feedparser の published_parsed を YYYY-MM-DD 文字列に変換。失敗時は今日。"""
    try:
        t = entry.get("published_parsed")
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        pass
    return date.today().isoformat()


def _extract_location(title: str, q_type: str) -> str:
    """タイトルから大まかな地域を抽出。ヒューリスティックな簡易実装。"""
    # 国内: 都道府県名を探す
    if q_type == "domestic":
        prefectures = [
            "北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島",
            "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川",
            "新潟", "富山", "石川", "福井", "山梨", "長野", "岐阜",
            "静岡", "愛知", "三重", "滋賀", "京都", "大阪", "兵庫",
            "奈良", "和歌山", "鳥取", "島根", "岡山", "広島", "山口",
            "徳島", "香川", "愛媛", "高知", "福岡", "佐賀", "長崎",
            "熊本", "大分", "宮崎", "鹿児島", "沖縄",
        ]
        for p in prefectures:
            if p in title:
                return f"日本 / {p}"
        return "日本"
    # 国外: 国名・都市名を簡易マッチ
    country_hints = {
        "US": "アメリカ", "USA": "アメリカ", "China": "中国", "UK": "イギリス",
        "Australia": "オーストラリア", "India": "インド", "Germany": "ドイツ",
        "France": "フランス", "Canada": "カナダ", "Brazil": "ブラジル",
        "Dubai": "UAE", "UAE": "UAE", "Pakistan": "パキスタン",
    }
    for keyword, country in country_hints.items():
        if keyword.lower() in title.lower():
            return country
    return "海外"


def _clean_title(raw: str) -> str:
    """Google News タイトルに付く「 - メディア名」を除去。"""
    return re.sub(r"\s*-\s*[^-]+$", "", raw).strip()


def fetch_feed(query_cfg: dict) -> list[dict]:
    """1クエリ分の Google News RSS を取得してリストを返す。"""
    url = GNEWS_RSS_TEMPLATE.format(
        query=quote(query_cfg["q"]),
        lang=query_cfg["lang"],
        region=query_cfg["region"],
    )
    logger.info("Fetching: %s", url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; DroneIncidentBot/1.0; "
            "+https://github.com/your-org/drone-incident-db)"
        )
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Request failed for query '%s': %s", query_cfg["q"], e)
        return []

    feed = feedparser.parse(resp.text)
    items = []
    for entry in feed.entries[:MAX_ITEMS_PER_QUERY]:
        title = _clean_title(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        items.append({
            "date":     _parse_date(entry),
            "location": _extract_location(title, query_cfg["type"]),
            "type":     query_cfg["type"],
            "title":    title,
            "link":     link,
        })
    logger.info("  → %d items", len(items))
    return items


# ─── メイン処理 ────────────────────────────────────────────────────────────────

def main():
    # 1. 既存データ読み込み
    existing: list[dict] = []
    if OUTPUT_FILE.exists():
        try:
            existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            logger.info("Loaded %d existing records.", len(existing))
        except json.JSONDecodeError:
            logger.warning("data.json is broken. Starting fresh.")

    existing_links: set[str] = {item["link"] for item in existing}

    # 2. 新規フェッチ
    new_items: list[dict] = []
    for cfg in QUERIES:
        fetched = fetch_feed(cfg)
        for item in fetched:
            if item["link"] not in existing_links:
                new_items.append(item)
                existing_links.add(item["link"])
        time.sleep(REQUEST_INTERVAL)

    logger.info("New items fetched: %d", len(new_items))

    # 3. マージ・ID 振り直し・古いデータ削除
    cutoff = datetime.now(timezone.utc).toordinal() - DEDUP_DAYS
    merged = existing + new_items
    merged = [
        item for item in merged
        if datetime.fromisoformat(item["date"]).toordinal() >= cutoff
    ]
    merged.sort(key=lambda x: x["date"], reverse=True)

    for idx, item in enumerate(merged, start=1):
        item["id"] = idx

    # 4. 保存
    OUTPUT_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Saved %d records to %s", len(merged), OUTPUT_FILE)


if __name__ == "__main__":
    main()
