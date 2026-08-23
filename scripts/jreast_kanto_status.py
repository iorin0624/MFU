#!/usr/bin/env python3
import urllib.request
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime
from bs4 import BeautifulSoup

AREA_URL = "https://transit.yahoo.co.jp/diainfo/area/4"
OUTPUT_DIR = "/home/admin/jreast_kanto_status"
RAW_FILE = os.path.join(OUTPUT_DIR, "kanto_status.json")
SUMMARY_FILE = os.path.join(OUTPUT_DIR, "kanto_status_summary.json")
CATALOG_FILE = os.path.join(OUTPUT_DIR, "kanto_line_catalog.json")

# 個別に監視する新幹線路線
SHINKANSEN_LINES = [
    ("北海道新幹線", "https://transit.yahoo.co.jp/diainfo/637/0"),
    ("東北新幹線", "https://transit.yahoo.co.jp/diainfo/1/0"),
    ("秋田新幹線", "https://transit.yahoo.co.jp/diainfo/6/0"),
    ("山形新幹線", "https://transit.yahoo.co.jp/diainfo/5/0"),
    ("上越新幹線", "https://transit.yahoo.co.jp/diainfo/3/0"),
    ("北陸新幹線", "https://transit.yahoo.co.jp/diainfo/624/0"),
    ("東海道新幹線", "https://transit.yahoo.co.jp/diainfo/7/0"),
]

EARTHQUAKE_DELAY_MESSAGE = "地震の影響で、一部列車に遅れや運休が出ています。"
EARTHQUAKE_GROUP_TITLE = "地震による一部列車に遅れや運休がある路線"
EARTHQUAKE_DELAY_ONLY_MESSAGE = "地震の影響で、一部列車に遅れが出ています。"
EARTHQUAKE_DELAY_ONLY_GROUP_TITLE = "地震による一部列車に遅れがある路線"


def normalize_shared_message(value: str) -> str:
    """HTML実体参照と空白差を除き、共通運行情報の完全一致判定に使う。"""
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def shared_message_group_title(value: str) -> str:
    return {
        EARTHQUAKE_DELAY_MESSAGE: EARTHQUAKE_GROUP_TITLE,
        EARTHQUAKE_DELAY_ONLY_MESSAGE: EARTHQUAKE_DELAY_ONLY_GROUP_TITLE,
    }.get(normalize_shared_message(value), "")


def shared_update_time_sort_key(value: str):
    match = re.search(
        r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s*"
        r"(?P<hour>\d{1,2})時(?P<minute>\d{1,2})分",
        str(value or ""),
    )
    if not match:
        return float("-inf")
    now = datetime.now()
    parts = {name: int(match.group(name)) for name in ("month", "day", "hour", "minute")}
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year=year, **parts))
        except ValueError:
            continue
    if not candidates:
        return float("-inf")
    nearest = min(candidates, key=lambda candidate: abs((candidate - now).total_seconds()))
    return nearest.timestamp()


def group_shared_messages(summary_lines: list[dict]) -> list[dict]:
    """大量に並ぶ同一の地震情報を、影響路線付きの1行へまとめる。"""
    line_counts = {}
    for row in summary_lines:
        name = str(row.get("line_name") or "").strip()
        if name:
            line_counts[name] = line_counts.get(name, 0) + 1

    candidates = {}
    for index, row in enumerate(summary_lines):
        message = normalize_shared_message(row.get("message"))
        title = shared_message_group_title(message)
        status = str(row.get("status") or "").strip()
        line_name = str(row.get("line_name") or "").strip()
        if (
            not title
            or not line_name
            or "運転見合わせ" in status
            or row.get("affected_lines")
            or line_counts.get(line_name, 0) != 1
        ):
            continue
        candidates.setdefault((status, message, title), []).append((index, row))

    grouped_at = {}
    consumed = set()
    for (status, message, title), items in candidates.items():
        if len(items) < 2:
            continue
        first_index = items[0][0]
        source_lines = [str(row.get("line_name") or "").strip() for _, row in items]
        latest = max(
            items,
            key=lambda item: shared_update_time_sort_key(item[1].get("update_time_text")),
        )[1]
        digest = hashlib.sha256(f"{status}\n{message}".encode("utf-8")).hexdigest()[:16]
        grouped_at[first_index] = {
            "line_name": title,
            "notice_id": f"shared-{digest}",
            "status": status or "運行情報",
            "message": message,
            "affected_lines": source_lines,
            "affected_count": len(source_lines),
            "source_lines": source_lines,
            "group_type": "shared_message",
            "update_time_text": latest.get("update_time_text") or "",
            "publish_time": latest.get("publish_time") or "",
        }
        consumed.update(index for index, _ in items)

    result = []
    for index, row in enumerate(summary_lines):
        if index in grouped_at:
            result.append(grouped_at[index])
        elif index not in consumed:
            clean = dict(row)
            clean["message"] = normalize_shared_message(clean.get("message"))
            result.append(clean)
    return result


def fetch_html(url: str) -> bytes:
    """指定URLのHTMLを取得"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def parse_area(html: bytes) -> dict:
    """
    エリアページ（関東）のHTMLから
    「現在運行情報のある路線」の “路線名 + 詳細URL” を取得
    """
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    no_trouble = bool(
        re.search(
            r"(?:運行情報|事故・遅延(?:に関する)?情報)[^。]{0,24}(?:ありません|ない)",
            page_text,
        )
    )

    # エリアページ自体の更新時刻（あれば）
    updated_text = None
    main = soup.find("div", id="main")
    if main:
        label_large = main.find("div", class_="labelLarge")
        if label_large:
            sub = label_large.find("span", class_="subText")
            if sub:
                updated_text = sub.get_text(strip=True)

    # 「現在運行情報のある路線」のテーブル
    section = soup.find("div", id="mdStatusTroubleLine")
    if not section:
        return {
            "source": AREA_URL,
            "updated": updated_text,
            "area_parse_ok": no_trouble,
            "lines": [],
        }

    tbl_wrapper = section.find("div", class_="elmTblLstLine")
    if not tbl_wrapper:
        return {
            "source": AREA_URL,
            "updated": updated_text,
            "area_parse_ok": no_trouble,
            "lines": [],
        }

    table = tbl_wrapper.find("table")
    tbody = table.find("tbody") if table else None
    rows = tbody.find_all("tr") if tbody else []

    lines = []

    # 先頭行はヘッダ（路線 / 状況 / 詳細）なのでスキップ
    for tr in rows[1:]:
        tds = tr.find_all("td")
        if len(tds) < 1:
            continue

        a = tds[0].find("a")
        if not a:
            continue

        line_name = a.get_text(strip=True)
        line_url = a.get("href", "").strip()

        # 絶対URLに補正（念のため）
        if line_url.startswith("/"):
            line_url = "https://transit.yahoo.co.jp" + line_url

        lines.append({"line_name": line_name, "url": line_url})

    return {
        "source": AREA_URL,
        "updated": updated_text,
        "area_parse_ok": bool(rows) or no_trouble,
        "lines": lines,
    }


def parse_line_catalog(html: bytes) -> dict:
    """関東エリアの平常時を含む全路線と、固定の新幹線一覧を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find("div", id="mdAreaMajorLine")
    if not section:
        raise ValueError("関東エリアの全路線一覧が見つかりません。")

    lines = []
    seen_urls = set()

    def append_line(name: str, url: str, group: str) -> None:
        name = convert_brackets(str(name or "").strip())
        url = str(url or "").strip()
        if url.startswith("/"):
            url = "https://transit.yahoo.co.jp" + url
        if not name or not url.startswith("https://transit.yahoo.co.jp/diainfo/"):
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        lines.append({"line_name": name, "url": url, "group": group})

    for anchor in section.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/diainfo/" not in href:
            continue
        append_line(anchor.get_text(strip=True), href, "kanto")

    if len(lines) < 100:
        raise ValueError(f"関東エリアの路線一覧が少なすぎます: {len(lines)}件")

    for line_name, line_url in SHINKANSEN_LINES:
        append_line(line_name, line_url, "shinkansen")

    return {
        "source": AREA_URL,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "lines": lines,
    }


def parse_detail(html: bytes) -> dict:
    """
    各路線の詳細ページ HTML から詳細情報を取得
    - __NEXT_DATA__ の JSON から status / message / publishTime などを抜く
    """
    soup = BeautifulSoup(html, "html.parser")

    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return {}

    try:
        data = json.loads(script.string)
    except Exception:
        return {}

    page_props = data.get("props", {}).get("pageProps", {})
    update_time_text = page_props.get("updateTimeText")  # 例: "11月26日 13時5分"

    feature = page_props.get("diainfoTrainFeature")
    route_info = feature.get("routeInfo") if isinstance(feature, dict) else None
    property_info = route_info.get("property") if isinstance(route_info, dict) else None
    if (
        not isinstance(property_info, dict)
        or not property_info.get("railCode")
        or not property_info.get("displayName")
    ):
        return {}
    # 平常時は diainfo キー自体が存在しない。
    diainfo = property_info.get("diainfo", [])
    if not isinstance(diainfo, list):
        return {}

    notices = []
    for index, raw_notice in enumerate(diainfo):
        if not isinstance(raw_notice, dict):
            continue
        status = str(raw_notice.get("status") or "").strip()
        message = str(raw_notice.get("message") or "").strip()
        if not status and not message:
            continue
        notices.append(
            {
                "notice_id": str(raw_notice.get("infoId") or f"index-{index}"),
                "status": status,
                "message": message,
                "publish_time": str(raw_notice.get("publishTime") or "").strip(),
                "update_time_text": update_time_text,
            }
        )

    # Python's stable sort keeps Yahoo!'s order within the same severity.
    notices.sort(key=lambda notice: -status_severity(notice.get("status")))
    primary = notices[0] if notices else {}

    return {
        "status": primary.get("status"),
        "message": primary.get("message"),
        "publish_time": primary.get("publish_time"),
        "update_time_text": update_time_text,
        "notices": notices,
    }


def save_json(data: dict, path: str) -> None:
    """dict を UTF-8 の JSON として保存"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: str):
    """JSONファイルを読み込み。なければ None"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def convert_brackets(name: str) -> str:
    """
    東海道本線[東京～熱海] → 東海道本線（東京～熱海）
    のように、角括弧を全角丸括弧に変換する。
    あわせて (快速) / (各停) も全角カッコに揃える。
    """
    s = str(name) if name is not None else ""

    # 区間 [〜] → 全角カッコ
    if "[" in s and "]" in s:
        s = s.replace("[", "（").replace("]", "）")

    # 種別 (快速) / (各停) → 全角カッコ
    s = s.replace("(快速)", "（快速）").replace("(各停)", "（各停）")

    return s


def normalize_line_name(name: str) -> str:
    """
    路線名の表記揺れを吸収するための正規化:
      - 先頭の JR / ＪＲ を削除
      - [東京～宇都宮] や （○○） 以降を削る
      - 前後の空白を削る
    """
    if name is None:
        return ""
    s = str(name).strip()

    # 全角→半角 JR
    s = s.replace("ＪＲ", "JR")

    # 先頭の JR を落とす
    if s.startswith("JR"):
        s = s[2:].lstrip()

    # [ や （ 以降は補足とみなして落とす
    for ch in ("[", "（"):
        if ch in s:
            s = s.split(ch, 1)[0].strip()

    return s


def is_trouble_status(status: str) -> bool:
    """
    Yahoo!運行情報のステータスから「トラブルありかどうか」を判定する。
    平常運転・通常運転などは False、それ以外（遅延・運転見合わせ等）は True。
    """
    if not status:
        return False

    s = str(status)
    # ざっくり「平常」「通常」「情報はありません」が入っていたら通常運転扱い
    if any(word in s for word in ("平常", "通常", "情報はありません")):
        return False

    return True


def status_severity(status: str) -> int:
    value = str(status or "")
    if "運転見合わせ" in value:
        return 4
    if "運転再開" in value:
        return 3
    if "遅延" in value:
        return 2
    return 1 if value else 0


def build_summary(raw_data: dict) -> dict:
    """
    kanto_status.json を元に、内部処理だけで要約 JSON を作成する。
    """
    # トラブルのある路線だけを対象にする（平常運転などは除外）
    lines = [
        dict(line)
        for line in raw_data.get("lines", [])
        if isinstance(line, dict) and is_trouble_status(line.get("status"))
    ]
    if not lines:
        return {
            "updated": raw_data.get("updated"),
            "source": raw_data.get("source"),
            "lines": [],
        }

    # まず line_name / message の括弧を全角に統一しておく
    for l in lines:
        if "line_name" in l:
            l["line_name"] = convert_brackets(l["line_name"])
        if "message" in l and l["message"] is not None:
            l["message"] = convert_brackets(l["message"])

    # line_name -> line 本体
    name_to_line = {l.get("line_name"): l for l in lines if l.get("line_name")}
    all_names = list(name_to_line.keys())

    # 正規化名 -> 元の line_name のリスト
    norm_to_names = {}
    for name in all_names:
        norm = normalize_line_name(name)
        if not norm:
            continue
        norm_to_names.setdefault(norm, []).append(name)

    # ★直通運転の“名称”は影響路線に含めない（ただし当人の incident は出す）
    EXCLUDE_AFFECTED = {"上野東京ライン", "湘南新宿ライン"}
    EXCLUDE_AFFECTED_NORMS = {normalize_line_name(n) for n in EXCLUDE_AFFECTED}

    def is_excluded_affected(line_name: str) -> bool:
        if not line_name:
            return False
        return normalize_line_name(line_name) in EXCLUDE_AFFECTED_NORMS

    def is_suspended(status: str) -> bool:
        return "運転見合わせ" in str(status or "")

    def update_time_sort_key(value: str):
        match = re.search(
            r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s*"
            r"(?P<hour>\d{1,2})時(?P<minute>\d{1,2})分",
            str(value or ""),
        )
        if not match:
            return float("-inf")
        now = datetime.now()
        parts = {
            name: int(match.group(name))
            for name in ("month", "day", "hour", "minute")
        }
        candidates = []
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                candidates.append(datetime(year=year, **parts))
            except ValueError:
                continue
        if not candidates:
            return float("-inf")
        nearest = min(candidates, key=lambda candidate: abs((candidate - now).total_seconds()))
        return nearest.timestamp()

    def merge_update_time(info: dict, value):
        candidate = str(value or "").strip()
        if not candidate:
            return
        current = str(info.get("update_time_text") or "").strip()
        if not current or update_time_sort_key(candidate) > update_time_sort_key(current):
            info["update_time_text"] = candidate

    # main_line_name -> {line_name, status, message, affected_lines(set), update_time_text}
    incidents = {}

    def ensure_incident(main_name: str):
        if main_name not in incidents:
            base = name_to_line.get(main_name, {})
            incidents[main_name] = {
                "line_name": main_name,
                "status": base.get("status"),
                "message": base.get("message"),
                "affected_lines": set(),
                "update_time_text": base.get("update_time_text"),
            }

    def extract_cause_references(message: str):
        """
        「JR常磐線（各停）内での大雨の影響」のような明示的な参照から、
        路線名と原因語（大雨、旅客転落など）を取り出す。
        """
        references = []
        pattern = re.compile(
            r"(?:JR|ＪＲ)?"
            r"(?P<label>[^\s、。]+?線(?:（[^）]*）)*)"
            r"内で"
        )
        for match in pattern.finditer(str(message or "")):
            label = str(match.group("label") or "").strip()
            if not label:
                continue
            tail = str(message or "")[match.end() :].lstrip()
            for prefix in ("発生した", "発生している", "発生", "の"):
                if tail.startswith(prefix):
                    tail = tail[len(prefix) :].lstrip()
                    break
            reason_match = re.match(r"(?P<reason>[^、。]{1,80}?)の影響", tail)
            reason = (
                str(reason_match.group("reason") or "").strip()
                if reason_match
                else ""
            )
            references.append((label, reason))
        return references

    def resolve_cause_name(label: str):
        """種別・区間を残したまま、参照された1路線だけを解決する。"""
        candidate = convert_brackets(str(label or "").strip()).replace("ＪＲ", "JR")
        if candidate.startswith("JR"):
            candidate = candidate[2:].lstrip()
        if not candidate:
            return ""

        # 「常磐線（各停）」のような完全一致を最優先する。
        if candidate in name_to_line:
            return candidate

        # 「総武線（快速）」→「総武線（快速）（東京～千葉）」のように、
        # メッセージ側で区間だけ省略された場合は一意に決まるときだけ採用する。
        qualified_matches = [
            name for name in all_names if name.startswith(f"{candidate}（")
        ]
        if len(qualified_matches) == 1:
            return qualified_matches[0]

        # 補足のない名称でも候補が1路線だけなら採用する。
        normalized_matches = norm_to_names.get(normalize_line_name(candidate), [])
        if len(normalized_matches) == 1:
            return normalized_matches[0]

        # 候補が複数なら全候補へ広げず、本文どおりの名称を擬似メインにする。
        return candidate

    def compact_reason(value: str):
        return re.sub(r"\s+", "", str(value or ""))

    def cause_matches_current_incident(cause_name: str, reason: str):
        """実在路線では、参照元と現在の原因が一致するときだけ統合する。"""
        if cause_name not in name_to_line:
            return True
        reason_key = compact_reason(reason)
        main_message = compact_reason(name_to_line[cause_name].get("message"))
        return bool(reason_key and main_message and reason_key in main_message)

    # affected line -> [(cause line, reason)]
    relationships = {}
    pseudo_reason_by_cause = {}

    # --- 第1パス：本文に明示された原因路線を厳密に解決 ---
    for line in lines:
        lname = line.get("line_name")
        msg = line.get("message") or ""
        status = line.get("status")
        if not lname:
            continue
        # 運転見合わせと直通運転名称は、従来どおり常に独立表示する。
        if is_suspended(status) or is_excluded_affected(lname):
            continue

        accepted = []
        seen_causes = set()
        for label, reason in extract_cause_references(msg):
            cause = resolve_cause_name(label)
            if not cause or cause == lname:
                continue
            if cause not in name_to_line and normalize_line_name(cause) == normalize_line_name(lname):
                continue
            if not cause_matches_current_incident(cause, reason):
                continue

            # 擬似メインは同じ原因語の参照だけを1カードにまとめる。
            if cause not in name_to_line:
                reason_key = compact_reason(reason)
                existing_reason = pseudo_reason_by_cause.get(cause)
                if existing_reason is None:
                    pseudo_reason_by_cause[cause] = reason_key
                elif existing_reason != reason_key:
                    continue

            if cause not in seen_causes:
                seen_causes.add(cause)
                accepted.append((cause, reason))

        if accepted:
            relationships[lname] = accepted

    # --- 第2パス：独立表示する路線を先に作成 ---
    for line in lines:
        lname = line.get("line_name")
        if not lname or lname in relationships:
            continue
        ensure_incident(lname)
        incidents[lname]["status"] = line.get("status")
        incidents[lname]["message"] = line.get("message")
        merge_update_time(incidents[lname], line.get("update_time_text"))

    # --- 第3パス：検証済みの明示的な影響関係だけを追加 ---
    for line in lines:
        lname = line.get("line_name")
        if not lname or lname not in relationships:
            continue
        for cause, _reason in relationships[lname]:
            ensure_incident(cause)
            info = incidents[cause]
            info["affected_lines"].add(lname)
            merge_update_time(info, line.get("update_time_text"))

            # 一覧に存在しない擬似メインは、最初の影響路線を代表情報にする。
            if cause not in name_to_line:
                if info["status"] is None and line.get("status"):
                    info["status"] = line.get("status")
                if info["message"] is None and line.get("message"):
                    info["message"] = line.get("message")

    # set -> list に変換。同一路線にYahoo!の運行情報が複数ある場合は、
    # 路線を復旧扱いにせず、各情報を独立した表示行として残す。
    summary_lines = []
    for _, info in incidents.items():
        affected = sorted(convert_brackets(a) for a in info["affected_lines"])
        source = name_to_line.get(info["line_name"], {})
        notices = source.get("notices") if isinstance(source, dict) else None
        if not isinstance(notices, list) or not notices:
            notices = [
                {
                    "notice_id": "",
                    "status": info.get("status"),
                    "message": info.get("message"),
                    "update_time_text": info.get("update_time_text"),
                    "publish_time": source.get("publish_time") if isinstance(source, dict) else "",
                }
            ]
        for notice in notices:
            if not isinstance(notice, dict) or not is_trouble_status(notice.get("status")):
                continue
            summary_lines.append(
                {
                    "line_name": convert_brackets(info["line_name"]),
                    "notice_id": str(notice.get("notice_id") or ""),
                    "status": notice.get("status"),
                    "message": convert_brackets(notice.get("message") or ""),
                    "affected_lines": affected,
                    "update_time_text": notice.get("update_time_text") or info.get("update_time_text"),
                    "publish_time": notice.get("publish_time") or "",
                }
            )

    return {
        "updated": raw_data.get("updated"),
        "source": raw_data.get("source"),
        "lines": group_shared_messages(summary_lines),
    }


def main():
    try:
        # 通常: 引数なし → ネットから取得
        # テスト: --test-from-file [JSONパス] → そのファイルからサマリ生成
        use_test = len(sys.argv) >= 2 and sys.argv[1] == "--test-from-file"

        if use_test:
            src = sys.argv[2] if len(sys.argv) >= 3 else RAW_FILE
            data = load_json(src)
            if not data:
                print(f"ERROR: cannot load test file: {src}")
                return
            print(f"loaded test raw: {src} (lines={len(data.get('lines', []))})")
        else:
            # 1) エリア一覧から「対象路線リスト」を取得
            area_html = fetch_html(AREA_URL)
            catalog = parse_line_catalog(area_html)
            save_json(catalog, CATALOG_FILE)
            print(f"saved catalog: {CATALOG_FILE} (lines={len(catalog.get('lines', []))})")
            data = parse_area(area_html)
            if not data.get("area_parse_ok"):
                raise ValueError("関東エリアの運行情報欄を正しく解析できませんでした。")

            # 2) 各路線ごとに詳細ページを取りに行って、詳細情報を統合
            for line in data.get("lines", []):
                url = line.get("url")
                if not url:
                    continue
                try:
                    detail_html = fetch_html(url)
                    detail_info = parse_detail(detail_html)
                    if not detail_info:
                        raise ValueError("路線詳細を解析できませんでした。")
                    for k, v in detail_info.items():
                        line[k] = v
                except Exception as e:
                    line["error"] = str(e)

            # 2.5) 新幹線の個別ページも取得してマージ
            existing_norms = {
                normalize_line_name(l.get("line_name"))
                for l in data.get("lines", [])
                if l.get("line_name")
            }

            for sh_name, sh_url in SHINKANSEN_LINES:
                norm = normalize_line_name(sh_name)
                # 既にエリア一覧側に同名路線がある場合は重複させない
                if norm in existing_norms:
                    continue

                try:
                    detail_html = fetch_html(sh_url)
                    detail_info = parse_detail(detail_html)
                    if not detail_info:
                        raise ValueError("路線詳細を解析できませんでした。")
                    line = {
                        "line_name": sh_name,
                        "url": sh_url,
                    }
                    # status / message などを付与
                    for k, v in detail_info.items():
                        line[k] = v
                    data.setdefault("lines", []).append(line)
                except Exception as e:
                    data.setdefault("lines", []).append(
                        {
                            "line_name": sh_name,
                            "url": sh_url,
                            "error": str(e),
                        }
                    )

            # 3) 生データ（路線＋新幹線）を RAW_FILE に保存
            save_json(data, RAW_FILE)
            print(f"saved raw: {RAW_FILE} (lines={len(data.get('lines', []))})")

        # 4) 運行情報（トラブル）が全く無い場合は、空のサマリを書き出して終了
        trouble_lines = [
            l for l in data.get("lines", []) if is_trouble_status(l.get("status"))
        ]
        if not trouble_lines:
            summary = {
                "updated": data.get("updated"),
                "source": data.get("source"),
                "lines": [],
            }
            save_json(summary, SUMMARY_FILE)
            print(f"no trouble lines. saved empty summary: {SUMMARY_FILE}")
            return

        # 5) サマリJSONを作成して保存（保存直前に“運転見合わせ優先”で並び替え）
        summary = build_summary(data)

        summary["lines"].sort(
            key=lambda x: (-status_severity(x.get("status")), x.get("line_name") or "")
        )

        save_json(summary, SUMMARY_FILE)
        print(f"saved summary: {SUMMARY_FILE}")

    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    main()
