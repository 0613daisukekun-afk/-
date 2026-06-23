"""
drone_scraper.py
================
Google News RSS からドローン事故ニュースを取得し、
data.json に追記・上書き保存するスクリプト。

新フィールド:
    publisher : 新聞社名（朝日新聞/読売新聞/毎日新聞など。指定なしは null）
    target    : 衝突対象の自動分類 "person"(人) / "object"(モノ) / "unknown"(不明)

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
    # --- 国内（一般キーワード） ---
    {"q": "ドローン 事故",     "type": "domestic",       "lang": "ja", "region": "JP", "publisher": None},
    {"q": "ドローン 墜落",     "type": "domestic",       "lang": "ja", "region": "JP", "publisher": None},
    {"q": "ドローン 衝突",     "type": "domestic",       "lang": "ja", "region": "JP", "publisher": None},
    {"q": "無人機 事故",       "type": "domestic",       "lang": "ja", "region": "JP", "publisher": None},
    # --- 国内（新聞社指定） ---
    # site:演算子 + スペース区切りキーワードで複数パターンをカバー
    {"q": "site:asahi.com ドローン 事故",     "type": "domestic", "lang": "ja", "region": "JP", "publisher": "朝日新聞"},
    {"q": "site:asahi.com ドローン 墜落",     "type": "domestic", "lang": "ja", "region": "JP", "publisher": "朝日新聞"},
    {"q": "site:yomiuri.co.jp ドローン 事故", "type": "domestic", "lang": "ja", "region": "JP", "publisher": "読売新聞"},
    {"q": "site:yomiuri.co.jp ドローン 墜落", "type": "domestic", "lang": "ja", "region": "JP", "publisher": "読売新聞"},
    {"q": "site:mainichi.jp ドローン 事故",   "type": "domestic", "lang": "ja", "region": "JP", "publisher": "毎日新聞"},
    {"q": "site:mainichi.jp ドローン 墜落",   "type": "domestic", "lang": "ja", "region": "JP", "publisher": "毎日新聞"},
    # --- 国外 ---
    {"q": "drone accident",    "type": "international",  "lang": "en", "region": "US", "publisher": None},
    {"q": "UAV crash",         "type": "international",  "lang": "en", "region": "US", "publisher": None},
    {"q": "drone incident",    "type": "international",  "lang": "en", "region": "US", "publisher": None},
    {"q": "drone collision",   "type": "international",  "lang": "en", "region": "US", "publisher": None},
]

# 新聞社指定クエリ（site:）は緩くマッチしがちなため、
# 取得後にタイトルへドローン関連語を含むかを再チェックして絞り込む。
DRONE_KEYWORDS_JA = ["ドローン", "無人機", "UAV", "マルチコプター"]
DRONE_KEYWORDS_EN = ["drone", "uav", "quadcopter"]


def _is_drone_related(title: str) -> bool:
    """新聞社指定クエリの結果から、ドローンと無関係な記事を除外するための判定。"""
    t = title.lower()
    if any(kw in title for kw in DRONE_KEYWORDS_JA):
        return True
    if any(kw in t for kw in DRONE_KEYWORDS_EN):
        return True
    return False

# ─── 衝突対象（人 / モノ / 不明）判定キーワード ───────────────────────────────

# 人体・人身被害を示すキーワード（日英）
PERSON_KEYWORDS = [
    "人に", "人へ", "頭部", "顔", "頭", "腕", "足", "けが", "ケガ", "負傷",
    "重傷", "軽傷", "死亡", "死傷", "搬送", "観客", "歩行者", "通行人",
    "子供", "女性", "男性", "児童", "園児", "怪我",
    "person", "people", "injur", "wound", "hurt", "hit a man", "hit a woman",
    "struck a", "bystander", "spectator", "child", "pedestrian", "hospitalized",
    "killed", "death", "fatal",
]

# 物・構造物・設備への衝突を示すキーワード（日英）
OBJECT_KEYWORDS = [
    "電線", "電柱", "建物", "屋根", "壁", "車", "自動車", "送電線", "鉄塔",
    "フェンス", "ビル", "屋上", "橋", "施設", "設備", "タービン", "風車",
    "家屋", "民家", "工場", "倉庫", "物損", "全損", "破損",
    "power line", "pole", "building", "roof", "wall", "car", "vehicle",
    "tower", "fence", "structure", "facility", "turbine", "warehouse",
    "wire", "infrastructure", "property damage", "crashed into",
]


def _classify_target(title: str, desc: str = "") -> str:
    """タイトル（と概要）から衝突対象を 'person' / 'object' / 'unknown' に分類。"""
    text = f"{title} {desc}".lower()

    person_hit = any(kw.lower() in text for kw in PERSON_KEYWORDS)
    object_hit = any(kw.lower() in text for kw in OBJECT_KEYWORDS)

    if person_hit and not object_hit:
        return "person"
    if object_hit and not person_hit:
        return "object"
    if person_hit and object_hit:
        # 両方ヒットした場合は人身被害を優先（安全側に倒す）
        return "person"
    return "unknown"


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
    is_publisher_query = query_cfg.get("publisher") is not None
    items = []
    skipped_unrelated = 0
    for entry in feed.entries[:MAX_ITEMS_PER_QUERY]:
        title = _clean_title(entry.get("title", ""))
        link = entry.get("link", "")
        summary = entry.get("summary", "")
        if not title or not link:
            continue
        # 新聞社指定クエリ（site:）は site の全記事をかなり緩く拾うことがあるため、
        # タイトルにドローン関連語を含まない記事は除外する
        if is_publisher_query and not _is_drone_related(title):
            skipped_unrelated += 1
            continue
        items.append({
            "date":      _parse_date(entry),
            "location":  _extract_location(title, query_cfg["type"]),
            "type":      query_cfg["type"],
            "title":     title,
            "link":      link,
            "publisher": query_cfg.get("publisher"),
            "target":    _classify_target(title, summary),
        })
    if skipped_unrelated:
        logger.info("  （ドローン非関連のため %d 件を除外）", skipped_unrelated)
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

    # 既存データに新フィールド（publisher / target）がない場合は補完
    for item in existing:
        item.setdefault("publisher", None)
        if "target" not in item:
            item["target"] = _classify_target(item.get("title", ""))

    # クリーンアップ: 新聞社指定で取得されたがドローンと無関係な過去データを除去
    # （以前の site:検索クエリが緩くマッチしていたことへの対処）
    before_count = len(existing)
    existing = [
        item for item in existing
        if not (item.get("publisher") and not _is_drone_related(item.get("title", "")))
    ]
    removed = before_count - len(existing)
    if removed:
        logger.info("既存データからドローン非関連の新聞社記事 %d 件を削除しました。", removed)
        existing_links = {item["link"] for item in existing}

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
