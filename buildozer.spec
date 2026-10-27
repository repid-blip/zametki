[app]
title = Notes Bot
package.name = notesbot
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,ttf
version = 1.0
orientation = portrait
requirements = python3,kivy,certifi,pyjnius,android,sqlite3,openssl
services = botservice:service.py:foreground
android.permissions = INTERNET,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,POST_NOTIFICATIONS,WAKE_LOCK
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

p4a.branch = v2024.01.21

# --- Добавляем файлы xray в APK ---
source.include_patterns = xray_arm64, xray_arm32

[buildozer]
log_level = 2
warn_on_root = 1
