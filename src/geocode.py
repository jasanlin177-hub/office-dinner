# -*- coding: utf-8 -*-
"""地理編碼：取得「文山第一分局」的經緯度，作為搜尋圓心。

依 Google Geocoding API 官方文件：
endpoint: https://maps.googleapis.com/maps/api/geocode/json
必要參數: address, key
座標路徑: results[0].geometry.location.{lat,lng}
"""
import json
from pathlib import Path

import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
BASE_ADDRESS = "文山第一分局"

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "base_location.json"


class GeocodeError(Exception):
    pass


def _load_cache() -> dict | None:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save_cache(lat: float, lng: float, formatted_address: str) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"lat": lat, "lng": lng, "formatted_address": formatted_address},
            f,
            ensure_ascii=False,
            indent=2,
        )


def get_base_location(api_key: str, force_refresh: bool = False) -> tuple[float, float, str]:
    """回傳 (lat, lng, formatted_address)。結果快取於 data/base_location.json（不含機密資訊）。"""
    if not force_refresh:
        cached = _load_cache()
        if cached and "lat" in cached and "lng" in cached:
            return cached["lat"], cached["lng"], cached.get("formatted_address", BASE_ADDRESS)

    resp = requests.get(
        GEOCODE_URL,
        params={"address": BASE_ADDRESS, "key": api_key, "language": "zh-TW", "region": "tw"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status")
    if status != "OK":
        raise GeocodeError(
            f"Geocoding API 回傳異常狀態: {status}，"
            f"錯誤訊息: {data.get('error_message', '（無）')}"
        )

    results = data.get("results") or []
    if not results:
        raise GeocodeError("Geocoding API 查無「文山第一分局」的座標結果")

    location = results[0]["geometry"]["location"]
    lat, lng = location["lat"], location["lng"]
    formatted_address = results[0].get("formatted_address", BASE_ADDRESS)

    _save_cache(lat, lng, formatted_address)
    return lat, lng, formatted_address
