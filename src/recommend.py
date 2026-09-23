# -*- coding: utf-8 -*-
"""篩選 + 隨機推薦邏輯。

篩選條件：
1. businessStatus == OPERATIONAL（排除永久/暫時歇業）
2. rating > 3.5
3. takeout 或 delivery 至少一項為 True
4. 此刻是否營業中（依 regularOpeningHours.periods 對照「使用者實際點開頁面的當下時間」判斷，
   不是固定的晚餐時段）；若店家未提供營業時間資料，不予排除，但標記 hours_unknown=True 讓使用者自行確認。
   （Google 也有現成的 openNow 欄位，但那是「查詢當下」算好的快照，我們的資料是網格快取、
   可能是幾天前查的，openNow 會過期失真，所以改用不會過期的 periods 週期資料自行即時運算）
5. 依 mode 篩選類別（"dinner"=排除甜點/飲料/場地類；"dessert"=只保留甜點/飲料類）
6. 預算範圍：優先用 priceRange（實際新台幣金額）判斷每人消費是否落在 [min_price_twd, max_price_twd]；
   店家沒有 priceRange 時，上限退回用 priceLevel 排除 EXPENSIVE/VERY_EXPENSIVE，下限不判斷
7. 距離範圍：[min_distance_m, max_distance_m]，用實際座標算直線距離
8. 店名關鍵字黑名單（例如「婚宴」「會館」），作為 primaryType 判斷不到的備援

篩選後的候選再用加權分數排序/抽選：距離40% + 評分40% + 隨機20%（見 score_candidates）。
"""
import math
import random
from datetime import datetime

from src.places import RADIUS_METERS

MIN_RATING = 3.5

# Places API (New) primaryType：甜點/飲料類（Food and Drink 類別下非正餐的子類）
DESSERT_DRINK_TYPES = {
    "cafe", "cafeteria", "coffee_roastery", "coffee_shop", "coffee_stand",
    "juice_shop", "tea_house", "cat_cafe", "cake_shop", "candy_store",
    "confectionery", "chocolate_factory", "chocolate_shop", "dessert_restaurant",
    "dessert_shop", "donut_shop", "ice_cream_shop", "bakery", "bagel_shop",
}

# Places API (New) primaryType：宴會/活動場地類（非以供餐為主業，不適合日常晚餐）
VENUE_TYPES = {"banquet_hall", "event_venue"}

# primaryType 判斷不到時的備援：店名關鍵字黑名單
NAME_KEYWORD_BLACKLIST = ["婚宴", "喜宴", "宴會廳", "會館"]

# priceRange 資料缺失時的備援：priceLevel 超過此等級一律排除
EXCLUDED_PRICE_LEVELS = {"PRICE_LEVEL_EXPENSIVE", "PRICE_LEVEL_VERY_EXPENSIVE"}

# 每人消費預算範圍（新台幣）預設值：優先以 priceRange 實際金額判斷
MIN_PRICE_PER_PERSON_TWD = 1
MAX_PRICE_PER_PERSON_TWD = 300

# 加權分數的三個權重：距離越近、評分越高分數越高，另外保留隨機性避免每次結果都一樣
DISTANCE_WEIGHT = 0.4
RATING_WEIGHT = 0.4
RANDOM_WEIGHT = 0.2
MAX_RATING = 5.0
EARTH_RADIUS_M = 6371000


def _google_weekday(d) -> int:
    """轉換為 Google 的星期表示：0=星期日 ... 6=星期六。"""
    return (d.weekday() + 1) % 7


def _period_minutes(day: int, hour: int, minute: int) -> int:
    return day * 24 * 60 + hour * 60 + minute


def _is_open_now(regular_opening_hours: dict | None, now: datetime) -> bool | None:
    """回傳 True/False 表示「此時此刻」是否營業中；None 表示資料不足無法判斷。

    用 periods（每週固定週期，不會過期）對照使用者實際點開頁面當下的時間點，
    而不是固定的 18:00-20:00 晚餐窗口——確保使用者點選當下看到的推薦確實營業中。
    """
    if not regular_opening_hours:
        return None
    periods = regular_opening_hours.get("periods")
    if not periods:
        return None

    now_google_day = _google_weekday(now.date())
    now_minutes = _period_minutes(now_google_day, now.hour, now.minute)

    for period in periods:
        open_p = period.get("open")
        close_p = period.get("close")
        if not open_p:
            continue
        open_min = _period_minutes(open_p["day"], open_p["hour"], open_p["minute"])

        if close_p:
            close_min = _period_minutes(close_p["day"], close_p["hour"], close_p["minute"])
            if close_min <= open_min:
                close_min += 7 * 24 * 60  # 跨週（例如營業到隔天凌晨）
        else:
            # 沒有 close 代表 24 小時營業
            close_min = open_min + 7 * 24 * 60

        # 將週期正規化到本週附近比較，判斷此刻的時間點是否落在營業區間內
        for offset in (-7 * 24 * 60, 0, 7 * 24 * 60):
            o = open_min + offset
            c = close_min + offset
            if o <= now_minutes < c:
                return True

    return False


def _matches_name_blacklist(place: dict) -> bool:
    name = place.get("displayName", {}).get("text", "")
    return any(keyword in name for keyword in NAME_KEYWORD_BLACKLIST)


def _price_out_of_range(place: dict, min_price_twd: int, max_price_twd: int) -> bool:
    """優先用 priceRange 實際金額判斷是否落在 [min_price_twd, max_price_twd] 之外；
    沒有 priceRange 時，上限退回用 priceLevel 等級判斷，下限因無對應資料不判斷。"""
    price_range = place.get("priceRange")
    if price_range:
        end_price = price_range.get("endPrice") or {}
        start_price = price_range.get("startPrice") or {}
        end_units = end_price.get("units")
        start_units = start_price.get("units")

        # 上限：優先用 endPrice；缺 endPrice（代表區間無上限）才退回用 startPrice
        upper = end_units if end_units is not None else start_units
        if upper is not None and int(upper) > max_price_twd:
            return True

        # 下限：優先用 startPrice；缺 startPrice 才退回用 endPrice
        lower = start_units if start_units is not None else end_units
        if lower is not None and int(lower) < min_price_twd:
            return True

        return False

    return place.get("priceLevel") in EXCLUDED_PRICE_LEVELS


def _matches_food_keywords(place: dict, keywords: list[str]) -> bool:
    """店名是否包含任一指定關鍵字（例如「飯」「麵」）。keywords 為空代表不限制。"""
    if not keywords:
        return True
    name = place.get("displayName", {}).get("text", "")
    return any(keyword in name for keyword in keywords)


def filter_candidates(
    places: list[dict],
    now: datetime | None = None,
    mode: str = "dinner",
    min_price_twd: int = MIN_PRICE_PER_PERSON_TWD,
    max_price_twd: int = MAX_PRICE_PER_PERSON_TWD,
    food_keywords: list[str] | None = None,
    base_lat: float | None = None,
    base_lng: float | None = None,
    min_distance_m: float = 0,
    max_distance_m: float = RADIUS_METERS,
) -> list[dict]:
    """套用評價/服務方式/營業狀態/類別/價格/距離/菜色關鍵字篩選，
    回傳附加 hours_unknown（與有算距離時 distance_m）標記的候選清單。

    mode="dinner"（預設）：排除甜點/飲料/場地類，適合正餐晚餐。
    mode="dessert"：只保留甜點/飲料類，適合下午茶/冰品場景。
    food_keywords：店名關鍵字白名單（例如 ["飯", "麵"]），符合任一即保留；留空或 None 代表不限制。
    base_lat/base_lng：提供時才會套用距離篩選（[min_distance_m, max_distance_m]）。
    """
    if mode not in ("dinner", "dessert"):
        raise ValueError(f"未知的 mode: {mode}")

    now = now or datetime.now()
    candidates = []
    for place in places:
        if place.get("businessStatus") != "OPERATIONAL":
            continue
        rating = place.get("rating")
        if rating is None or rating <= MIN_RATING:
            continue
        if not (place.get("takeout") or place.get("delivery")):
            continue
        if _price_out_of_range(place, min_price_twd, max_price_twd):
            continue
        if _matches_name_blacklist(place):
            continue
        if not _matches_food_keywords(place, food_keywords or []):
            continue

        # Google 對很多店家的 primaryType 是空值，實際類型只出現在 types 陣列裡，
        # 例如「木柵製冰所」primaryType=None 但 types 含 dessert_restaurant；
        # 「彭園婚宴新店館」primaryType=chinese_restaurant 但 types 含 banquet_hall/wedding_venue，
        # 因此必須合併 primaryType 與 types 一起判斷，不能只看 primaryType。
        all_types = set(place.get("types") or [])
        primary_type = place.get("primaryType")
        if primary_type:
            all_types.add(primary_type)

        if all_types & VENUE_TYPES:
            continue  # 婚宴會館/活動場地類，任何模式都不適合日常晚餐
        is_dessert_drink = bool(all_types & DESSERT_DRINK_TYPES)
        if mode == "dinner" and is_dessert_drink:
            continue
        if mode == "dessert" and not is_dessert_drink:
            continue

        open_status = _is_open_now(place.get("regularOpeningHours"), now)
        if open_status is False:
            continue  # 此刻不是營業時間，明確排除（確保推薦時點得到就吃得到）

        distance_m = None
        if base_lat is not None and base_lng is not None:
            location = place.get("location") or {}
            place_lat, place_lng = location.get("latitude"), location.get("longitude")
            if place_lat is None or place_lng is None:
                continue  # 要求算距離但沒有座標資料，無法判斷是否在範圍內，排除
            distance_m = _haversine_distance_m(base_lat, base_lng, place_lat, place_lng)
            if not (min_distance_m <= distance_m <= max_distance_m):
                continue

        enriched = dict(place)
        enriched["hours_unknown"] = open_status is None
        if distance_m is not None:
            enriched["distance_m"] = distance_m
        candidates.append(enriched)

    return candidates


def _haversine_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def score_candidates(
    candidates: list[dict], base_lat: float, base_lng: float, seed: str | None = None
) -> list[dict]:
    """為每個候選加上 distance_m 與 weighted_score（距離40% + 評分40% + 隨機20%），
    並依 weighted_score 由高到低排序回傳。

    距離分數：越靠近基點分數越高（以搜尋半徑 RADIUS_METERS 為 0 分基準，超過半徑視為 0 分）。
    評分分數：以篩選門檻 MIN_RATING ~ 5 分正規化到 0-1（篩選後的候選評分必定 > MIN_RATING）。
    """
    rng = random.Random(seed) if seed else random.Random()
    scored = []
    for place in candidates:
        location = place.get("location") or {}
        place_lat, place_lng = location.get("latitude"), location.get("longitude")
        if place_lat is not None and place_lng is not None:
            distance_m = _haversine_distance_m(base_lat, base_lng, place_lat, place_lng)
        else:
            distance_m = RADIUS_METERS  # 無座標資料時，距離分數視為最差（0分）

        distance_score = max(0.0, min(1.0, 1 - distance_m / RADIUS_METERS))

        rating = place.get("rating", MIN_RATING)
        rating_score = max(0.0, min(1.0, (rating - MIN_RATING) / (MAX_RATING - MIN_RATING)))

        random_score = rng.random()

        weighted_score = (
            DISTANCE_WEIGHT * distance_score
            + RATING_WEIGHT * rating_score
            + RANDOM_WEIGHT * random_score
        )

        enriched = dict(place)
        enriched["distance_m"] = distance_m
        enriched["weighted_score"] = weighted_score
        scored.append(enriched)

    scored.sort(key=lambda p: p["weighted_score"], reverse=True)
    return scored


RECOMMENDATION_COUNT = 3


def pick_recommendations(scored_candidates: list[dict], seed: str | None = None) -> list[dict]:
    """從已計算 weighted_score 的候選中做「不重複的加權抽樣」挑 RECOMMENDATION_COUNT 間
    （候選數不足時，有幾間就挑幾間）。

    weighted_score 越高的店家中選機率越高，但仍保留機會給分數較低的店家（非硬性排名截斷）。
    seed 用於「同一天多次開啟結果一致」，例如當天日期字串。
    """
    if not scored_candidates:
        return []
    rng = random.Random(seed) if seed else random.Random()
    count = min(len(scored_candidates), RECOMMENDATION_COUNT)

    pool = list(scored_candidates)
    picks = []
    for _ in range(count):
        weights = [max(p["weighted_score"], 1e-6) for p in pool]
        total = sum(weights)
        threshold = rng.uniform(0, total)
        cumulative = 0.0
        for i, w in enumerate(weights):
            cumulative += w
            if cumulative >= threshold:
                picks.append(pool.pop(i))
                break
    return picks
