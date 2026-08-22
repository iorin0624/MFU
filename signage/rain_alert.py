"""Rain-cloud movement analysis and Discord alerts for river screenshots.

The upstream map is rendered at a fixed 1920x1223 viewport and cropped to
1920x1080 by ``river_capture.py``.  The helpers in this module deliberately
avoid external weather APIs: they inspect the colour overlay in the captured
images and compare it with the recent local history.
"""

from __future__ import annotations

import io
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image, ImageDraw


CAPTURE_SIZE = (1920, 1080)
MAP_CENTER_PIXEL = (960.0, 686.0)
MAP_TOP = 149
MAP_RIGHT = 1370
FLOATING_PANEL = (1115, 742, 1370, 1080)
POINT_RADIUS = 22
DRY_SCORE = 0.16
RAIN_COVERAGE = 0.20
ANALYSIS_OFFSETS_MINUTES = (5, 10, 15, 20, 25, 30)
STATE_LABELS = {
    "disabled": "無効",
    "unknown": "判定不能",
    "dry": "雨なし",
    "approaching": "雨が近づいています",
    "raining": "雨が降っています",
    "weakening": "雨が弱まっています",
    "stopping_soon": "まもなく止みそうです",
}


@dataclass(frozen=True)
class MapGeometry:
    center_lat: float
    center_lng: float
    zoom: float
    center_x: float = MAP_CENTER_PIXEL[0]
    center_y: float = MAP_CENTER_PIXEL[1]

    @classmethod
    def from_url(cls, river_url: str) -> "MapGeometry":
        query = parse_qs(urlparse(river_url).query)
        try:
            return cls(
                center_lat=float(query["clat"][0]),
                center_lng=float(query["clon"][0]),
                zoom=float(query["zm"][0]),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("川の防災情報URLから中心座標とズームを読み取れません。") from exc

    @property
    def world_size(self) -> float:
        return 256.0 * (2.0 ** self.zoom)

    @staticmethod
    def _world_xy(lat: float, lng: float, world_size: float) -> tuple[float, float]:
        latitude = max(-85.05112878, min(85.05112878, lat))
        sin_lat = math.sin(math.radians(latitude))
        x = (lng + 180.0) / 360.0 * world_size
        y = (0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * world_size
        return x, y

    def to_pixel(self, lat: float, lng: float) -> tuple[float, float]:
        center_world = self._world_xy(self.center_lat, self.center_lng, self.world_size)
        point_world = self._world_xy(lat, lng, self.world_size)
        return (
            self.center_x + point_world[0] - center_world[0],
            self.center_y + point_world[1] - center_world[1],
        )

    def to_coordinates(self, x: float, y: float) -> tuple[float, float]:
        center_world = self._world_xy(self.center_lat, self.center_lng, self.world_size)
        world_x = center_world[0] + x - self.center_x
        world_y = center_world[1] + y - self.center_y
        lng = world_x / self.world_size * 360.0 - 180.0
        n = math.pi - (2.0 * math.pi * world_y / self.world_size)
        lat = math.degrees(math.atan(math.sinh(n)))
        return lat, lng


def point_is_visible(x: float, y: float, margin: int = POINT_RADIUS) -> bool:
    if x < margin or x >= MAP_RIGHT - margin or y < MAP_TOP + margin or y >= CAPTURE_SIZE[1] - margin:
        return False
    left, top, right, bottom = FLOATING_PANEL
    return not (left - margin <= x <= right + margin and top - margin <= y <= bottom + margin)


def _rain_pixel_weight(rgb: tuple[int, int, int]) -> float:
    """Return an approximate precipitation strength for an averaged map pixel."""
    red, green, blue = rgb
    spread = max(rgb) - min(rgb)
    if spread < 24:
        return 0.0

    # Light/medium rain: the upstream overlay uses cyan, blue and violet.
    if blue >= 165 and blue - red >= 28 and blue - green >= 5:
        return 0.75 if blue - red < 70 else 1.0

    # Heavy rain: yellow -> orange -> red.
    if red >= 178 and blue <= 185 and red - blue >= 35 and green >= 65:
        if green >= 185:
            return 1.55
        if green >= 125:
            return 2.25
        return 2.8

    # Very heavy rain: red-purple/magenta.
    if red >= 135 and blue >= 125 and green <= 165 and red - green >= 18 and blue - green >= 5:
        return 3.0
    return 0.0


def _sample_point(image: Image.Image, x: float, y: float) -> dict:
    cx, cy = int(round(x)), int(round(y))
    pixels = image.load()
    weights: list[float] = []
    radius_sq = POINT_RADIUS * POINT_RADIUS
    inner_sq = 5 * 5
    for py in range(cy - POINT_RADIUS, cy + POINT_RADIUS + 1, 2):
        for px in range(cx - POINT_RADIUS, cx + POINT_RADIUS + 1, 2):
            distance_sq = (px - cx) ** 2 + (py - cy) ** 2
            if inner_sq <= distance_sq <= radius_sq:
                weights.append(_rain_pixel_weight(pixels[px, py]))
    if not weights:
        return {"score": 0.0, "coverage": 0.0, "rainy": False}
    coverage = sum(1 for weight in weights if weight > 0) / len(weights)
    score = sum(weights) / len(weights)
    return {
        "score": round(score, 4),
        "coverage": round(coverage, 4),
        "rainy": score >= DRY_SCORE and coverage >= RAIN_COVERAGE,
    }


def _field_features(image: Image.Image, x: float, y: float, radius: int = 300, scale: int = 8) -> dict:
    left = max(0, int(x) - radius)
    top = max(MAP_TOP, int(y) - radius)
    right = min(MAP_RIGHT, int(x) + radius)
    bottom = min(CAPTURE_SIZE[1], int(y) + radius)
    if right - left < scale * 3 or bottom - top < scale * 3:
        return {"distance": None, "vector": None, "rain_cells": 0}

    width = max(1, (right - left) // scale)
    height = max(1, (bottom - top) // scale)
    sampled = image.crop((left, top, right, bottom)).resize((width, height), Image.Resampling.BOX)
    raw = list(sampled.getdata())
    mask = [_rain_pixel_weight(pixel) > 0 for pixel in raw]
    center_x = (x - left) / (right - left) * width
    center_y = (y - top) / (bottom - top) * height
    candidates: list[tuple[float, float, float]] = []

    for row in range(1, height - 1):
        for column in range(1, width - 1):
            index = row * width + column
            if not mask[index]:
                continue
            neighbours = 0
            for offset_y in (-1, 0, 1):
                start = (row + offset_y) * width + column - 1
                neighbours += sum(mask[start : start + 3])
            if neighbours < 5:
                continue
            absolute_x = left + (column + 0.5) * (right - left) / width
            absolute_y = top + (row + 0.5) * (bottom - top) / height
            if not point_is_visible(absolute_x, absolute_y, margin=0):
                continue
            dx = absolute_x - x
            dy = absolute_y - y
            candidates.append((math.hypot(dx, dy), dx, dy))

    if not candidates:
        return {"distance": None, "vector": None, "rain_cells": 0}
    distance, dx, dy = min(candidates, key=lambda item: item[0])
    return {
        "distance": round(distance, 2),
        "vector": [round(dx, 2), round(dy, 2)],
        "rain_cells": len(candidates),
    }


def _intensity_label(score: float, rainy: bool) -> str:
    if not rainy:
        return "雨なし"
    if score < 0.45:
        return "弱い雨"
    if score < 0.9:
        return "雨"
    if score < 1.5:
        return "やや強い雨"
    return "強い雨"


def _monotonic_ratio(values: list[float], decreasing: bool) -> float:
    if len(values) < 2:
        return 0.0
    matches = 0
    for before, after in zip(values, values[1:]):
        if (before >= after - 2.0) if decreasing else (before <= after + 0.04):
            matches += 1
    return matches / (len(values) - 1)


def analyse_point(point: dict, frames: list[tuple[datetime, Image.Image]], geometry: MapGeometry) -> dict:
    x, y = geometry.to_pixel(float(point["lat"]), float(point["lng"]))
    base = {
        "id": point["id"],
        "name": point["name"],
        "pixel": {"x": round(x, 1), "y": round(y, 1)},
        "eta_minutes": None,
        "confidence": 0,
    }
    if not point_is_visible(x, y):
        return {
            **base,
            "state": "unknown",
            "state_label": STATE_LABELS["unknown"],
            "intensity": "判定不能",
            "message": "地点が画像の地図範囲外、または操作パネルの下にあります。",
        }
    if not frames:
        return {
            **base,
            "state": "unknown",
            "state_label": STATE_LABELS["unknown"],
            "intensity": "判定不能",
            "message": "解析できる画像がありません。",
        }

    observations = []
    for captured_at, image in frames:
        sample = _sample_point(image, x, y)
        observations.append({"captured_at": captured_at, **sample})
    current = observations[0]
    intensity = _intensity_label(current["score"], current["rainy"])

    # Rain has just cleared: retain a stopping event for a few observations so
    # the three-sample confirmation rule can still complete.
    past_rain = [item for item in observations[1:] if item["rainy"]]
    if not current["rainy"] and past_rain:
        confidence = min(95, 65 + len(past_rain) * 7)
        return {
            **base,
            "state": "stopping_soon",
            "state_label": "雨が止んだ可能性があります",
            "intensity": intensity,
            "eta_minutes": 0,
            "confidence": confidence,
            "score": current["score"],
            "coverage": current["coverage"],
            "message": "直前まで雨域でしたが、現在は雨域から外れています。",
        }

    if current["rainy"]:
        chronological = list(reversed(observations))
        scores = [item["score"] for item in chronological]
        oldest = chronological[0]
        elapsed = max(1.0, (current["captured_at"] - oldest["captured_at"]).total_seconds() / 60.0)
        decrease = oldest["score"] - current["score"]
        decrease_rate = decrease / elapsed
        trend_ratio = _monotonic_ratio(scores, decreasing=True)
        eta = None
        if decrease >= 0.12 and decrease_rate > 0 and trend_ratio >= 0.55:
            eta = max(1, round(max(0.0, current["score"] - DRY_SCORE) / decrease_rate))
        if eta is not None and eta <= int(point["stop_minutes"]):
            confidence = min(95, round(55 + trend_ratio * 25 + min(15, decrease * 20)))
            return {
                **base,
                "state": "stopping_soon",
                "state_label": STATE_LABELS["stopping_soon"],
                "intensity": intensity,
                "eta_minutes": eta,
                "confidence": confidence,
                "score": current["score"],
                "coverage": current["coverage"],
                "message": f"直近{round(elapsed)}分の弱まり方から、約{eta}分以内に雨域を抜ける見込みです。",
            }
        if decrease >= 0.10 and trend_ratio >= 0.5:
            return {
                **base,
                "state": "weakening",
                "state_label": STATE_LABELS["weakening"],
                "intensity": intensity,
                "confidence": min(90, round(50 + trend_ratio * 30)),
                "score": current["score"],
                "coverage": current["coverage"],
                "message": "雨域の色が直近の画像より弱まっています。",
            }
        return {
            **base,
            "state": "raining",
            "state_label": STATE_LABELS["raining"],
            "intensity": intensity,
            "confidence": min(95, round(55 + current["coverage"] * 40)),
            "score": current["score"],
            "coverage": current["coverage"],
            "message": "現在地点が雨域に入っています。",
        }

    fields = []
    for item, (_, image) in zip(observations, frames):
        feature = _field_features(image, x, y)
        if feature["distance"] is not None:
            fields.append({"captured_at": item["captured_at"], **feature})
    if len(fields) >= 2:
        chronological = list(reversed(fields))
        distances = [item["distance"] for item in chronological]
        elapsed = max(1.0, (chronological[-1]["captured_at"] - chronological[0]["captured_at"]).total_seconds() / 60.0)
        progress = chronological[0]["distance"] - chronological[-1]["distance"]
        speed = progress / elapsed
        trend_ratio = _monotonic_ratio(distances, decreasing=True)
        if progress >= 8.0 and speed >= 0.55 and trend_ratio >= 0.55:
            eta = max(1, round(chronological[-1]["distance"] / speed))
            if eta <= int(point["rain_minutes"]):
                confidence = min(95, round(50 + trend_ratio * 25 + min(20, progress / 3)))
                return {
                    **base,
                    "state": "approaching",
                    "state_label": STATE_LABELS["approaching"],
                    "intensity": intensity,
                    "eta_minutes": eta,
                    "confidence": confidence,
                    "score": current["score"],
                    "coverage": current["coverage"],
                    "message": f"雨域との距離が直近{round(elapsed)}分で約{round(progress)}px縮まりました。",
                }

    return {
        **base,
        "state": "dry",
        "state_label": STATE_LABELS["dry"],
        "intensity": intensity,
        "confidence": 70 if len(observations) >= 3 else 55,
        "score": current["score"],
        "coverage": current["coverage"],
        "message": "通知条件に該当する雨域の接近は確認されていません。",
    }


def _capture_timestamp(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem, "%Y%m%dT%H%M%S%z")
    except ValueError:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except OSError:
            return None


def load_analysis_frames(history_dir: Path, latest_path: Path) -> list[tuple[datetime, Image.Image]]:
    if not latest_path.is_file():
        return []
    current_at = datetime.fromtimestamp(latest_path.stat().st_mtime).astimezone()
    candidates = []
    if history_dir.is_dir():
        for path in history_dir.glob("*.webp"):
            captured_at = _capture_timestamp(path)
            if captured_at and current_at - timedelta(minutes=34) <= captured_at < current_at - timedelta(seconds=30):
                candidates.append((captured_at, path))
    selected: list[tuple[datetime, Path]] = [(current_at, latest_path)]
    used: set[Path] = {latest_path}
    for offset in ANALYSIS_OFFSETS_MINUTES:
        target = current_at - timedelta(minutes=offset)
        eligible = sorted(candidates, key=lambda item: abs((item[0] - target).total_seconds()))
        if eligible and abs((eligible[0][0] - target).total_seconds()) <= 180 and eligible[0][1] not in used:
            selected.append(eligible[0])
            used.add(eligible[0][1])

    frames = []
    for captured_at, path in selected:
        try:
            with Image.open(path) as source:
                frames.append((captured_at, source.convert("RGB")))
        except (OSError, ValueError):
            continue
    return frames


def _within_notification_window(now: datetime, start: str, end: str) -> bool:
    start_hour, start_minute = (int(value) for value in start.split(":"))
    end_hour, end_minute = (int(value) for value in end.split(":"))
    current = now.hour * 60 + now.minute
    start_value = start_hour * 60 + start_minute
    end_value = end_hour * 60 + end_minute
    if start_value == end_value:
        return True
    if start_value < end_value:
        return start_value <= current < end_value
    return current >= start_value or current < end_value


def _read_json(path: Path, default: dict) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else default
    except (FileNotFoundError, OSError, ValueError):
        return default


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


def _event_type(result: dict) -> str | None:
    if result.get("state") == "approaching":
        return "approach"
    if result.get("state") == "stopping_soon":
        return "stop"
    return None


def _prepare_notification(point: dict, result: dict, record: dict, confirmations: int, now: datetime) -> str | None:
    event = _event_type(result)
    if record.get("signature") != f"{point['lat']:.6f},{point['lng']:.6f}":
        record.clear()
        record["signature"] = f"{point['lat']:.6f},{point['lng']:.6f}"

    if event == record.get("candidate"):
        record["candidate_count"] = int(record.get("candidate_count", 0)) + 1
    else:
        record["candidate"] = event
        record["candidate_count"] = 1 if event else 0

    if result.get("state") == "raining":
        record["sent_stop"] = False
    if result.get("state") == "dry" and not event:
        record["dry_count"] = int(record.get("dry_count", 0)) + 1
        if record["dry_count"] >= 10:
            record["sent_approach"] = False
    else:
        record["dry_count"] = 0

    if not event or int(record.get("candidate_count", 0)) < confirmations:
        return None
    sent_key = "sent_approach" if event == "approach" else "sent_stop"
    if record.get(sent_key):
        return None
    if not _within_notification_window(now, point["notify_start"], point["notify_end"]):
        record["pending_event"] = event
        return None
    return event


def _crop_point_image(image: Image.Image, result: dict) -> bytes:
    x = int(round(result["pixel"]["x"]))
    y = int(round(result["pixel"]["y"]))
    half_width, half_height = 260, 175
    left = max(0, min(image.width - half_width * 2, x - half_width))
    top = max(MAP_TOP, min(image.height - half_height * 2, y - half_height))
    crop = image.crop((left, top, left + half_width * 2, top + half_height * 2))
    marker_x, marker_y = x - left, y - top
    draw = ImageDraw.Draw(crop)
    draw.ellipse(
        (marker_x - 12, marker_y - 12, marker_x + 12, marker_y + 12),
        outline=(255, 255, 255),
        width=6,
    )
    draw.ellipse(
        (marker_x - 9, marker_y - 9, marker_x + 9, marker_y + 9),
        outline=(220, 20, 60),
        width=5,
    )
    output = io.BytesIO()
    crop.save(output, format="PNG", optimize=True)
    return output.getvalue()


def send_discord_alert(
    webhook_url: str,
    point: dict,
    result: dict,
    event: str,
    image_bytes: bytes,
    page_url: str,
) -> bool:
    if not webhook_url:
        return False
    if event == "approach":
        title = f"☔ {point['name']}：雨が降りそうです"
        description = f"約{result['eta_minutes']}分以内に雨域へ入る可能性があります。"
        colour = 0x3498DB
    else:
        title = f"🌤️ {point['name']}：雨が止みそうです"
        description = (
            "雨域から外れた可能性があります。"
            if result.get("eta_minutes") == 0
            else f"約{result['eta_minutes']}分以内に雨域を抜ける可能性があります。"
        )
        colour = 0x2ECC71
    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "url": page_url,
                "color": colour,
                "fields": [
                    {"name": "現在", "value": result.get("intensity", "不明"), "inline": True},
                    {"name": "確度", "value": f"{result.get('confidence', 0)}%", "inline": True},
                    {"name": "通知時間帯", "value": f"{point['notify_start']}～{point['notify_end']}", "inline": True},
                ],
                "image": {"url": "attachment://rain-point.png"},
                "footer": {"text": "スクリーンショット比較による目安です。実際の雨量・予報も確認してください。"},
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        ],
        "allowed_mentions": {"parse": []},
    }
    response = requests.post(
        webhook_url,
        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
        files={"files[0]": ("rain-point.png", image_bytes, "image/png")},
        timeout=15,
    )
    response.raise_for_status()
    return True


def run_rain_analysis(
    *,
    config: dict,
    river_url: str,
    history_dir: Path,
    latest_path: Path,
    state_path: Path,
    status_path: Path,
    webhook_url: str,
    page_url: str,
    logger,
    now: datetime | None = None,
    sender: Callable[..., bool] = send_discord_alert,
) -> dict:
    current = now or datetime.now().astimezone()
    status = {
        "updated_at": current.isoformat(timespec="seconds"),
        "enabled": bool(config.get("enabled")),
        "discord_configured": bool(webhook_url),
        "points": [],
        "notifications_sent": [],
    }
    if not config.get("enabled"):
        _write_json(status_path, status)
        return status

    geometry = MapGeometry.from_url(river_url)
    frames = load_analysis_frames(history_dir, latest_path)
    persistent = _read_json(state_path, {"points": {}})
    point_states = persistent.setdefault("points", {})
    confirmations = max(2, min(5, int(config.get("confirmations", 3))))

    for point in config.get("points", [])[:2]:
        if not point.get("enabled"):
            status["points"].append(
                {
                    "id": point["id"],
                    "name": point["name"],
                    "state": "disabled",
                    "state_label": STATE_LABELS["disabled"],
                }
            )
            continue
        result = analyse_point(point, frames, geometry)
        result["notification_window_active"] = _within_notification_window(
            current, point["notify_start"], point["notify_end"]
        )
        record = point_states.setdefault(point["id"], {})
        event = _prepare_notification(point, result, record, confirmations, current)
        result["confirmation_count"] = int(record.get("candidate_count", 0))
        result["confirmation_required"] = confirmations
        if event:
            if not webhook_url:
                result["notification_error"] = "adminのDiscord Webhookが未設定です。"
            else:
                try:
                    image_bytes = _crop_point_image(frames[0][1], result)
                    if sender(webhook_url, point, result, event, image_bytes, page_url):
                        sent_key = "sent_approach" if event == "approach" else "sent_stop"
                        record[sent_key] = True
                        record["pending_event"] = None
                        record["last_notified_at"] = current.isoformat(timespec="seconds")
                        status["notifications_sent"].append({"point_id": point["id"], "event": event})
                except Exception as exc:
                    logger.exception("雨雲Discord通知に失敗しました point=%s", point["id"])
                    result["notification_error"] = str(exc)
        record["last_state"] = result.get("state")
        record["updated_at"] = current.isoformat(timespec="seconds")
        status["points"].append(result)

    persistent["updated_at"] = current.isoformat(timespec="seconds")
    _write_json(state_path, persistent)
    _write_json(status_path, status)
    return status
