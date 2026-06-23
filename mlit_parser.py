"""
mlit_parser.py
==============
国土交通省が公開する「無人航空機に係る事故等報告一覧」PDFを解析し、
data.json と同じスキーマのレコードに変換するモジュール。

データソース（公式・信頼度最高）:
    https://www.mlit.go.jp/koku/accident_report.html
    PDF本体: https://www.mlit.go.jp/common/001585162.pdf
    （令和4年12月5日以降に報告のあったもの。国交省が随時更新）

このPDFは表形式のレイアウトのため、pdfplumber の extract_tables() を使い
列構造（発生日時 / 発生場所 / 飛行させた者 / 型式 / 事案の概要 / 人の死傷等 / ...）
を保持したまま抽出する。単純なテキスト抽出（extract_text）は列の対応が
ズレるリスクがあるため使用しない。

依存ライブラリ:
    pdfplumber>=0.10
    requests>=2.31
"""

import io
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests

logger = logging.getLogger(__name__)

MLIT_PDF_URL = "https://www.mlit.go.jp/common/001585162.pdf"
MLIT_SOURCE_PAGE = "https://www.mlit.go.jp/koku/accident_report.html"

# ─── 和暦 → 西暦 変換テーブル ─────────────────────────────────────────────

ERA_OFFSETS = {
    "令和": 2018,  # 令和1年 = 2019年 → 西暦 = 令和年 + 2018
    "平成": 1988,  # 平成1年 = 1989年
}

WAREKI_DATE_RE = re.compile(
    r"(令和|平成)(\d{1,2}|元)年\s*(\d{1,2})月\s*(\d{1,2})日"
)


def _wareki_to_seireki(text: str) -> str | None:
    """「令和5年7月10日」のような和暦文字列を YYYY-MM-DD に変換。"""
    m = WAREKI_DATE_RE.search(text)
    if not m:
        return None
    era, year_str, month_str, day_str = m.groups()
    year_num = 1 if year_str == "元" else int(year_str)
    year = year_num + ERA_OFFSETS[era]
    month = int(month_str)
    day = int(day_str)
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


# ─── 都道府県抽出 ────────────────────────────────────────────────────────

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]


def _extract_prefecture(text: str) -> str:
    """「発生場所」セルから都道府県名を抽出して location 文字列を作る。"""
    if not text:
        return "日本"
    for pref in PREFECTURES:
        if pref in text:
            short = pref.rstrip("県都府道")
            return f"日本 / {short}"
    return "日本"


# ─── 人の死傷等 → target 分類 ────────────────────────────────────────────

def _classify_target_from_casualty(casualty_text: str, summary_text: str) -> str:
    """
    「人の死傷等」列の内容から衝突対象を判定。
    国交省PDFは「なし」「右手指3本切創」のように明示的に書かれているため、
    タイトル推測より高精度に判定できる。
    """
    casualty_text = (casualty_text or "").strip()
    summary_text = summary_text or ""

    # 「なし」「－」「―」「無」など負傷なしを示す表記
    no_injury_markers = ["なし", "無し", "－", "―", "-", "無"]
    if casualty_text in no_injury_markers or casualty_text == "":
        # 死傷者なし → 物（モノ）への衝突か、墜落のみかを概要文から判定
        object_markers = [
            "建物", "屋根", "壁", "車", "電線", "電柱", "送電線", "鉄塔",
            "フェンス", "施設", "設備", "家屋", "倉庫", "看板", "外壁",
            "窓ガラス", "雨樋", "支線", "電話線", "通信線", "光ケーブル",
            "ソーラーパネル", "墓石", "標識",
        ]
        if any(kw in summary_text for kw in object_markers):
            return "object"
        return "unknown"  # 単なる制御不能・紛失等で対象物が明確でない場合

    # 「なし」以外の記述（負傷部位など）がある場合は人身被害
    return "person"


def _classify_severity(casualty_text: str) -> str:
    """死傷区分: 'injury'（負傷報告あり） / 'none'（負傷なし）"""
    casualty_text = (casualty_text or "").strip()
    no_injury_markers = ["なし", "無し", "－", "―", "-", "無", ""]
    if casualty_text in no_injury_markers:
        return "none"
    return "injury"


# ─── PDFテーブル抽出 ──────────────────────────────────────────────────────

# 想定される列見出し（PDFの表ヘッダー行に出現する語）
EXPECTED_HEADERS = [
    "No", "発生日時", "発生場所", "飛行させた者", "型式", "出発地",
    "事案の概要", "人の死傷", "機体の損壊", "再発防止", "備考",
]


def _looks_like_header_row(row: list) -> bool:
    """行がヘッダー行（列見出し）かどうかを判定。"""
    joined = "".join(c or "" for c in row)
    hits = sum(1 for h in EXPECTED_HEADERS if h in joined)
    return hits >= 3


def fetch_mlit_pdf_bytes(timeout: int = 30) -> bytes:
    """国交省PDFをダウンロードしてバイト列を返す。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; DroneIncidentBot/1.0; "
            "+https://github.com/your-org/drone-incident-db)"
        )
    }
    resp = requests.get(MLIT_PDF_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def parse_mlit_pdf(pdf_bytes: bytes) -> list[dict]:
    """
    国交省PDFのバイト列を解析し、data.json 互換のレコードリストを返す。

    各レコードは以下のキーを持つ:
        date, location, type, title, link, publisher, target
    加えて国交省データ特有のフィールド:
        severity ("injury"/"none")

    抽出方法は2段階:
        1. pdfplumber.extract_tables() による表構造の抽出（最も正確）
        2. 1で十分な件数が取れない場合、extract_text() ベースの
           和暦日付アンカー方式でフォールバック抽出する
    """
    import io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        records = _parse_via_tables(pdf)
        if len(records) < 10:
            logger.info(
                "テーブル抽出で %d 件のみ取得。テキストベースのフォールバック抽出を試みます。",
                len(records),
            )
            fallback_records = _parse_via_text_fallback(pdf)
            if len(fallback_records) > len(records):
                records = fallback_records

    logger.info("国交省PDFから %d 件のレコードを抽出しました。", len(records))
    return records


def _parse_via_tables(pdf) -> list[dict]:
    """pdfplumber の extract_tables() を使った表構造ベースの抽出（メイン手法）。"""
    records: list[dict] = []
    seen_keys: set[tuple] = set()

    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            for row in table:
                if row is None:
                    continue
                cells = [(c or "").replace("\n", " ").strip() for c in row]
                if not any(cells):
                    continue
                if _looks_like_header_row(cells):
                    continue
                if len(cells) < 6:
                    continue

                row_text = " ".join(cells)
                date_str = _wareki_to_seireki(row_text)
                if not date_str:
                    continue

                location_cell = ""
                for c in cells:
                    if any(p in c for p in PREFECTURES):
                        location_cell = c
                        break
                location = _extract_prefecture(location_cell)

                summary_cell = max(cells, key=len) if cells else ""

                casualty_cell = ""
                for c in cells:
                    if c != summary_cell and (
                        "創" in c or "傷" in c or c == "なし" or "打撲" in c
                        or "骨折" in c or "挫" in c
                    ):
                        casualty_cell = c
                        break

                record = _build_record(date_str, location, summary_cell, casualty_cell)
                key = (record["date"], record["location"], record["title"][:30])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                records.append(record)

    return records


# 「事案の概要」文の典型的な開始パターン（テキストフォールバック用）
SUMMARY_START_PATTERNS = [
    "空撮", "農薬散布", "測量", "点検", "訓練", "試験飛行", "離陸",
    "飛行", "レジャー", "橋梁",
]


def _parse_via_text_fallback(pdf) -> list[dict]:
    """
    extract_tables() が機能しない場合のフォールバック。
    ページ全体のテキストを和暦日付でアンカーし、次の和暦日付が出現するまでを
    1事案分のブロックとして切り出して解析する。
    表構造を使わないため精度はやや劣るが、レイアウト崩れに対して頑健。
    """
    records: list[dict] = []
    seen_keys: set[tuple] = set()

    full_text = ""
    for page in pdf.pages:
        text = page.extract_text() or ""
        full_text += text + "\n"

    # 和暦日付の出現位置をすべて見つけ、隣接する日付間をブロックとして切り出す
    matches = list(WAREKI_DATE_RE.finditer(full_text))
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        block = full_text[start:end]

        date_str = _wareki_to_seireki(block)
        if not date_str:
            continue

        location_cell = ""
        for p in PREFECTURES:
            if p in block:
                location_cell = p
                break
        location = _extract_prefecture(location_cell)

        # 概要文: 「事案の概要」らしき開始語を含み、かつ十分な長さを持つ文を抽出
        # （再発防止策の文と混同しないよう、候補の中から最長のものを選ぶ）
        candidates = []
        for line in re.split(r"[、。\n]", block):
            line = line.strip()
            if len(line) > 20 and any(line.startswith(p) for p in SUMMARY_START_PATTERNS):
                candidates.append(line)
        if candidates:
            summary_cell = max(candidates, key=len)
        else:
            lines = [l.strip() for l in re.split(r"\n", block) if len(l.strip()) > 15]
            summary_cell = max(lines, key=len) if lines else ""

        casualty_cell = ""
        casualty_match = re.search(r"([右左]?[手腕足顔頭鼻]\S{0,10}(?:創|傷|打撲|骨折))", block)
        if casualty_match:
            casualty_cell = casualty_match.group(1)
        elif "なし" in block[:200]:
            casualty_cell = "なし"

        record = _build_record(date_str, location, summary_cell, casualty_cell)
        key = (record["date"], record["location"], record["title"][:30])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        records.append(record)

    return records


def _build_record(date_str: str, location: str, summary_cell: str, casualty_cell: str) -> dict:
    """共通のレコード構築処理。"""
    target = _classify_target_from_casualty(casualty_cell, summary_cell)
    severity = _classify_severity(casualty_cell)
    title_summary = summary_cell[:60].rstrip("、。 ") if summary_cell else "無人航空機の事故等報告"
    title = f"【国交省報告】{title_summary}"
    return {
        "date":      date_str,
        "location":  location,
        "type":      "domestic",
        "title":     title,
        "link":      MLIT_SOURCE_PAGE,
        "publisher": "国土交通省",
        "target":    target,
        "severity":  severity,
    }


def fetch_mlit_records() -> list[dict]:
    """国交省PDFを取得してパース済みレコードを返す（呼び出し用の一括関数）。"""
    try:
        pdf_bytes = fetch_mlit_pdf_bytes()
    except requests.RequestException as e:
        logger.warning("国交省PDFの取得に失敗しました: %s", e)
        return []

    try:
        records = parse_mlit_pdf(pdf_bytes)
    except Exception as e:
        logger.warning("国交省PDFの解析に失敗しました: %s", e)
        return []

    # 異常検知: PDFは令和4年12月以降、現時点で数百件規模の蓄積がある想定。
    # 極端に少ない（レイアウト解析失敗の疑い）場合は警告を出す。
    if len(records) < 10:
        logger.warning(
            "国交省PDFからの抽出件数が異常に少ない (%d件)。"
            "PDFのレイアウトが変更され、テーブル抽出に失敗している可能性があります。"
            "mlit_records_preview.json で内容を確認してください。",
            len(records),
        )

    return records


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    recs = fetch_mlit_records()
    out_path = Path(__file__).parent / "mlit_records_preview.json"
    out_path.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(recs)} 件を {out_path} に保存しました。")
