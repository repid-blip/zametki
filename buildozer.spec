[app]
title = Notes Bot
package.name = notesbot
package.domain = org.example
source.dir = .
source.include_exts = py
version = 1.0
orientation = portrait
requirements = python3,kivy,certifi,pyjnius,android,sqlite3,openssl
services = botservice:service.py:foreground
android.permissions = INTERNET,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,POST_NOTIFICATIONS,WAKE_LOCK
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
