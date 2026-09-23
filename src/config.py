# -*- coding: utf-8 -*-
"""API Key 設定管理。

本機執行：Google API Key 依安全規範不可存放於專案目錄（會被 git 追蹤或誤傳出去），
一律存於使用者個人目錄 %APPDATA%\\自動點餐\\config.json。

部署到 Streamlit Community Cloud 等雲端平台時：改用平台的 st.secrets 機制
（在平台後台設定，不寫進程式碼、不進 git），load_api_key() 會優先讀 st.secrets，
讀不到（本機開發、平台沒設定）才退回讀本機 %APPDATA% 設定檔。
"""
import json
import os
from pathlib import Path

import streamlit as st

APP_DIR_NAME = "自動點餐"
CONFIG_FILENAME = "config.json"


def _config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # 非 Windows 環境的保底位置（正常上班環境應為 Windows，走不到這裡）
        appdata = str(Path.home() / ".config")
    config_dir = Path(appdata) / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / CONFIG_FILENAME


def load_api_key() -> str | None:
    try:
        if "google_api_key" in st.secrets:
            return st.secrets["google_api_key"]
    except Exception:
        pass  # 本機沒有 .streamlit/secrets.toml 是正常情況，不代表錯誤，繼續往下退回本機設定檔

    path = _config_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("google_api_key")


def save_api_key(api_key: str) -> None:
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"google_api_key": api_key}, f, ensure_ascii=False, indent=2)
