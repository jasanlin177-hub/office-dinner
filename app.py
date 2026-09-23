# -*- coding: utf-8 -*-
"""辦公室晚餐自動推薦程式（Streamlit 介面）。

以文山第一分局為基點，搜尋機車車程約10分鐘（直線距離2.5公里）內、
Google Maps 評價 > 3.5、支援外帶或外送、且此刻營業中的餐廳，隨機推薦3間。
"""
from datetime import date, datetime

import streamlit as st

from src import config
from src.geocode import GeocodeError, get_base_location
from src.places import PlacesApiError, refresh_due_cell, get_all_places, STALE_DAYS, GRID_SIZE
from src.recommend import filter_candidates, pick_recommendations, score_candidates

WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

st.set_page_config(page_title="辦公室晚餐推薦", page_icon="🍱", layout="centered")
st.title("🍱 辦公室晚餐推薦")
st.caption("基點：文山第一分局・範圍：機車車程約10分鐘（直線2.5公里內）")

# --- API Key 設定 ---
api_key = config.load_api_key()
if not api_key:
    st.warning("尚未設定 Google API Key，請先輸入（將儲存於個人目錄，不會存進專案資料夾）。")
    input_key = st.text_input("Google API Key", type="password")
    if st.button("儲存 API Key"):
        if input_key.strip():
            config.save_api_key(input_key.strip())
            st.success("已儲存，請重新整理頁面。")
            st.rerun()
        else:
            st.error("API Key 不可為空白")
    st.stop()

today = date.today()
st.write(f"今天是 {today.isoformat()}（{WEEKDAY_NAMES[today.weekday()]}）")

st.caption(
    f"店家資料改用 {GRID_SIZE}x{GRID_SIZE} 網格分批查詢，每格超過 {STALE_DAYS} 天沒更新就會在"
    "開啟頁面時自動補查（一次最多補一格），約每月完整輪替一輪，平時不會重複查詢浪費 API 費用。"
)
dessert_mode = st.toggle("🍰 下午茶/冰品模式（改為只顯示甜點、飲料類店家）", value=False)

col1, col2 = st.columns(2)
with col1:
    want_rice = st.checkbox("🍚 飯（店名含「飯」，不勾則不限制）", value=False)
with col2:
    want_noodle = st.checkbox("🍜 麵（店名含「麵」，不勾則不限制）", value=False)

food_keywords = []
if want_rice:
    food_keywords.append("飯")
if want_noodle:
    food_keywords.append("麵")

min_price_twd, max_price_twd = st.slider(
    "每人預算範圍（新台幣）", min_value=1, max_value=600, value=(1, 300), step=10
)
min_distance_km, max_distance_km = st.slider(
    "距離範圍（公里）", min_value=0.0, max_value=2.5, value=(0.0, 2.0), step=0.1
)

try:
    lat, lng, base_address = get_base_location(api_key)
except GeocodeError as e:
    st.error(f"取得文山第一分局座標失敗：{e}")
    st.stop()

st.caption(f"基點座標：{lat:.6f}, {lng:.6f}（{base_address}）")

try:
    with st.spinner("檢查是否有網格資料到期需要更新..."):
        refreshed_cell = refresh_due_cell(api_key, lat, lng)
    if refreshed_cell:
        st.toast(f"已自動更新網格 {refreshed_cell} 的店家資料", icon="🔄")
    raw_places = get_all_places(lat, lng)
except PlacesApiError as e:
    st.error(f"Google Places API 查詢失敗：{e}")
    st.stop()

if not raw_places:
    st.error(
        "目前沒有任何店家資料（可能是第一次開啟，網格還在陸續更新中，"
        "請確認 API Key 是否已啟用 Places API (New) 及計費帳戶，稍後重新整理頁面）。"
    )
    st.stop()

mode = "dessert" if dessert_mode else "dinner"
now = datetime.now()
candidates = filter_candidates(
    raw_places,
    now=now,
    mode=mode,
    min_price_twd=min_price_twd,
    max_price_twd=max_price_twd,
    food_keywords=food_keywords,
    base_lat=lat,
    base_lng=lng,
    min_distance_m=min_distance_km * 1000,
    max_distance_m=max_distance_km * 1000,
)

mode_desc = "甜點/飲料類" if dessert_mode else "正餐類（已排除甜點/飲料/婚宴場地）"
food_desc = f"、店名含「{'/'.join(food_keywords)}」" if food_keywords else ""
st.write(
    f"共查得 {len(raw_places)} 間店家，符合條件"
    f"（評價>3.5、可外帶/外送、此刻營業中、每人預算NT${min_price_twd}-{max_price_twd}、"
    f"距離{min_distance_km:.1f}-{max_distance_km:.1f}公里、{mode_desc}{food_desc}）"
    f"的有 {len(candidates)} 間。"
)

if not candidates:
    st.warning("今天沒有符合條件的店家，可能都公休或評價不足，建議手動查詢附近店家。")
    st.stop()

scored_candidates = score_candidates(candidates, lat, lng)
recommendations = pick_recommendations(scored_candidates)

st.subheader(f"今日推薦（{len(recommendations)} 間）")
for place in recommendations:
    name = place.get("displayName", {}).get("text", "（未知店名）")
    address = place.get("formattedAddress", "")
    rating = place.get("rating")
    rating_count = place.get("userRatingCount")
    phone = place.get("nationalPhoneNumber", "")
    maps_uri = place.get("googleMapsUri", "")
    takeout = "✅ 外帶" if place.get("takeout") else ""
    delivery = "✅ 外送" if place.get("delivery") else ""
    price_range = place.get("priceRange")
    if price_range:
        start_units = (price_range.get("startPrice") or {}).get("units")
        end_units = (price_range.get("endPrice") or {}).get("units")
        currency = (price_range.get("startPrice") or price_range.get("endPrice") or {}).get(
            "currencyCode", ""
        )
        if start_units is not None and end_units is not None:
            price_display = f"約 {currency} {start_units}-{end_units}/人"
        elif end_units is not None:
            price_display = f"約 {currency} {end_units} 以下/人"
        elif start_units is not None:
            price_display = f"約 {currency} {start_units} 以上/人"
        else:
            price_display = "價位未知"
    else:
        price_symbols = {
            "PRICE_LEVEL_FREE": "免費",
            "PRICE_LEVEL_INEXPENSIVE": "$",
            "PRICE_LEVEL_MODERATE": "$$",
            "PRICE_LEVEL_EXPENSIVE": "$$$",
            "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
        }
        price_display = price_symbols.get(place.get("priceLevel"), "價位未知")

    distance_m = place.get("distance_m")
    distance_display = f"📏 約 {distance_m/1000:.1f} 公里" if distance_m is not None else ""

    with st.container(border=True):
        st.markdown(f"### {name}")
        st.write(f"⭐ {rating}（{rating_count} 則評論） {price_display} {takeout} {delivery}")
        st.write(f"{distance_display}　📍 {address}")
        if phone:
            st.write(f"📞 {phone}")
        if place.get("hours_unknown"):
            st.caption("⚠️ 此店家未提供完整營業時間資料，建議出發前致電確認是否營業。")
        if maps_uri:
            st.markdown(f"[在 Google Maps 開啟]({maps_uri})")

with st.expander(f"查看所有符合條件的店家清單（依加權分數排序，共{len(scored_candidates)}間）"):
    for place in scored_candidates:
        name = place.get("displayName", {}).get("text", "（未知店名）")
        maps_uri = place.get("googleMapsUri", "")
        distance_km = place.get("distance_m", 0) / 1000
        line = f"- {name}（⭐{place.get('rating')}・📏{distance_km:.1f}km）"
        if maps_uri:
            st.markdown(f"{line} [Google Maps]({maps_uri})")
        else:
            st.write(line)
