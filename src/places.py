# -*- coding: utf-8 -*-
"""呼叫 Google Places API (New) searchText，以矩形網格分批查詢附近餐廳資料。

背景與演進：
1. 原本用 searchNearby 依 includedTypes 查詢，但單次最多只回傳20筆（依熱門度排序、不支援分頁），
   在餐廳密集的都會區很容易漏掉排名較後面但實際存在的店家。
2. 改用 searchText 搭配多個菜系/類別關鍵字查詢提高覆蓋率，但全範圍一次查完 19 個關鍵字
   花費較高，而且「每日自動更新」沒有實質意義（店家不會天天變動）。
3. 現在改成：把 2.5 公里搜尋範圍切成矩形網格，每次只更新「最久沒更新」的一格
   （超過 STALE_DAYS 天沒更新即視為到期），其餘格維持上次查到的資料直到輪到它。
   GRID_SIZE=3 即 3x3=9 格，搭配 STALE_DAYS=3，約每月完整輪替一輪。

依官方文件：
endpoint: POST https://places.googleapis.com/v1/places:searchText
headers : Content-Type: application/json
          X-Goog-Api-Key: <API_KEY>
          X-Goog-FieldMask: <逗號分隔欄位>
body    : textQuery（必要）, locationRestriction.rectangle{low{lat,lng}, high{lat,lng}}, pageSize（1-20）
          注意：searchText 的 locationRestriction 只支援 rectangle，不支援 circle（實際呼叫過才發現，
          曾誤用 locationBias.circle 這種「軟性偏好」做法，範圍外仍可能混入結果，
          改成 rectangle 網格後改用硬性限制，查詢後仍會再用實際座標篩掉超出 2.5 公里半徑的角落誤差）
欄位    : places.rating / places.userRatingCount / places.regularOpeningHours
          （periods[].open/close.day, 0=星期日）/ places.takeout / places.delivery
          / places.businessStatus / places.priceRange
"""
import json
import math
import time
from pathlib import Path

import requests

EARTH_RADIUS_M = 6371000
METERS_PER_LAT_DEGREE = 111320

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
RADIUS_METERS = 2500  # 機車車程約10分鐘的概略直線距離半徑
GRID_SIZE = 3  # 3x3=9 格
STALE_DAYS = 3  # 每格超過此天數沒更新即視為到期，9格 x 3天 約一個月輪完一輪
GRID_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "grid_cache.json"

FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.regularOpeningHours",
        "places.takeout",
        "places.delivery",
        "places.dineIn",
        "places.businessStatus",
        "places.nationalPhoneNumber",
        "places.googleMapsUri",
        "places.primaryType",
        "places.priceLevel",
        "places.priceRange",
        "places.types",
    ]
)

# 分菜系/類別關鍵字查詢，取代單一 includedTypes 查詢，提高覆蓋率。
# 關鍵字刻意不含地名（例如不寫「文山區」），避免 Google 忽略 locationRestriction。
TEXT_SEARCH_QUERIES = [
    "餐廳", "小吃店", "便當", "自助餐", "麵店",
    "中式餐廳", "台式料理", "日式料理", "韓式料理", "義式料理",
    "美式餐廳", "港式餐廳", "泰式料理", "速食店", "熱炒",
    "滷味", "雞排", "鹹酥雞", "早午餐",
]


class PlacesApiError(Exception):
    pass


def _haversine_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _build_grid(lat: float, lng: float) -> dict[str, dict]:
    """把以 (lat,lng) 為中心、半徑 RADIUS_METERS 的外接正方形切成 GRID_SIZE x GRID_SIZE 矩形。"""
    lat_delta = RADIUS_METERS / METERS_PER_LAT_DEGREE
    lng_delta = RADIUS_METERS / (METERS_PER_LAT_DEGREE * math.cos(math.radians(lat)))

    min_lat, max_lat = lat - lat_delta, lat + lat_delta
    min_lng, max_lng = lng - lng_delta, lng + lng_delta
    cell_lat_size = (max_lat - min_lat) / GRID_SIZE
    cell_lng_size = (max_lng - min_lng) / GRID_SIZE

    cells = {}
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            cell_min_lat = min_lat + row * cell_lat_size
            cell_min_lng = min_lng + col * cell_lng_size
            cell_id = f"r{row}c{col}"
            cells[cell_id] = {
                "min_lat": cell_min_lat,
                "max_lat": cell_min_lat + cell_lat_size,
                "min_lng": cell_min_lng,
                "max_lng": cell_min_lng + cell_lng_size,
            }
    return cells


def _load_grid_cache() -> dict:
    if GRID_CACHE_PATH.exists():
        try:
            with open(GRID_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"cells": {}}


def _save_grid_cache(data: dict) -> None:
    GRID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GRID_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _search_text_in_rect(api_key: str, text_query: str, rect: dict) -> list[dict]:
    body = {
        "textQuery": text_query,
        "pageSize": 20,
        "locationRestriction": {
            "rectangle": {
                "low": {"latitude": rect["min_lat"], "longitude": rect["min_lng"]},
                "high": {"latitude": rect["max_lat"], "longitude": rect["max_lng"]},
            }
        },
        "languageCode": "zh-TW",
    }
    resp = requests.post(
        SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        json=body,
        timeout=15,
    )
    if resp.status_code != 200:
        raise PlacesApiError(
            f"Places API 呼叫失敗 (query={text_query})，"
            f"HTTP {resp.status_code}: {resp.text[:500]}"
        )
    return resp.json().get("places", [])


def _ensure_grid_initialized(lat: float, lng: float, grid_data: dict) -> bool:
    """確保 grid_data["cells"] 裡有網格框線資料；回傳是否有異動（座標改變需重建）。"""
    expected_cells = _build_grid(lat, lng)
    changed = False
    for cell_id, rect in expected_cells.items():
        existing = grid_data["cells"].get(cell_id)
        if existing is None:
            grid_data["cells"][cell_id] = {**rect, "last_updated": None, "places": []}
            changed = True
        else:
            # 基點座標理論上不會變（文山第一分局固定），但保險起見同步框線
            for key in ("min_lat", "max_lat", "min_lng", "max_lng"):
                if abs(existing.get(key, 0) - rect[key]) > 1e-9:
                    existing[key] = rect[key]
                    changed = True
    return changed


MIN_REFRESH_INTERVAL_SECONDS = 20 * 60 * 60  # 全域節流：每天最多觸發一次，防止短時間內被重複點開頁面刷爆


def refresh_due_cell(api_key: str, lat: float, lng: float) -> str | None:
    """檢查是否有網格到期（從未更新，或超過 STALE_DAYS 天沒更新），
    每次呼叫最多只更新「最久沒更新」的一格，且全域每天最多觸發一次，
    避免短時間內重複開啟頁面就把整個網格一次查完、產生非預期的 API 費用。

    回傳有實際更新的話回傳該格 cell_id，否則回傳 None。
    """
    grid_data = _load_grid_cache()
    if "cells" not in grid_data:
        grid_data["cells"] = {}
    initialized = _ensure_grid_initialized(lat, lng, grid_data)

    now = time.time()
    stale_seconds = STALE_DAYS * 24 * 60 * 60

    last_refresh_at = grid_data.get("last_refresh_at")
    if last_refresh_at is not None and (now - last_refresh_at) < MIN_REFRESH_INTERVAL_SECONDS:
        if initialized:
            _save_grid_cache(grid_data)
        return None  # 今天已經觸發過一次了，不論有沒有格子到期都先不查

    # 找出最久沒更新的一格（從未更新的視為最舊，優先更新）
    oldest_cell_id, oldest_last_updated = None, None
    for cell_id, cell in grid_data["cells"].items():
        last_updated = cell.get("last_updated")
        if last_updated is None:
            oldest_cell_id = cell_id
            break  # 從未更新過的直接優先處理
        if oldest_last_updated is None or last_updated < oldest_last_updated:
            oldest_cell_id, oldest_last_updated = cell_id, last_updated

    if oldest_cell_id is None:
        return None

    cell = grid_data["cells"][oldest_cell_id]
    is_due = cell.get("last_updated") is None or (now - cell["last_updated"]) >= stale_seconds
    if not is_due:
        if initialized:
            _save_grid_cache(grid_data)
        return None

    merged: dict[str, dict] = {}
    for query in TEXT_SEARCH_QUERIES:
        for place in _search_text_in_rect(api_key, query, cell):
            place_id = place.get("id")
            if place_id and place_id not in merged:
                merged[place_id] = place

    cell["places"] = list(merged.values())
    cell["last_updated"] = now
    grid_data["last_refresh_at"] = now
    _save_grid_cache(grid_data)
    return oldest_cell_id


def grid_status() -> tuple[int, int]:
    """回傳 (已有資料的網格數, 總網格數)，供介面顯示目前累積進度。"""
    grid_data = _load_grid_cache()
    cells = grid_data.get("cells", {})
    updated = sum(1 for c in cells.values() if c.get("last_updated") is not None)
    return updated, GRID_SIZE * GRID_SIZE


def get_all_places(lat: float, lng: float) -> list[dict]:
    """合併所有網格目前累積的資料（不論該格新舊），依 place id 去重，
    並用實際座標篩掉網格四角超出 2.5 公里半徑的部分。"""
    grid_data = _load_grid_cache()
    if "cells" not in grid_data:
        return []

    merged: dict[str, dict] = {}
    for cell in grid_data["cells"].values():
        for place in cell.get("places", []):
            place_id = place.get("id")
            if place_id and place_id not in merged:
                merged[place_id] = place

    places = []
    for place in merged.values():
        location = place.get("location") or {}
        place_lat, place_lng = location.get("latitude"), location.get("longitude")
        if place_lat is None or place_lng is None:
            continue
        if _haversine_distance_m(lat, lng, place_lat, place_lng) <= RADIUS_METERS:
            places.append(place)
    return places
