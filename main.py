from flask import Flask, request, jsonify, Response
import requests as http_requests
import re
import os
import threading
import time
import firebase_admin
from firebase_admin import credentials, db as firebase_db

app = Flask(__name__)

# ─── إعدادات Firebase المصلحة ──────────────────
_SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(__file__), "serviceAccount.json")
_firebase_db_url = "https://alafreky-20e4c-default-rtdb.europe-west1.firebasedatabase.app"

if os.path.exists(_SERVICE_ACCOUNT_PATH):
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(_SERVICE_ACCOUNT_PATH)
            firebase_admin.initialize_app(cred, {"databaseURL": _firebase_db_url})
        print("[firebase] ✓ متصل بنجاح")
    except Exception as e:
        print(f"[firebase] ✗ خطأ في الاتصال: {e}")
else:
    print("[firebase] ✗ ملف serviceAccount.json غير موجود بجانب main.py")

@app.route('/')
def health_check():
    return "App is Running!", 200

# ─── تشغيل السيرفر المتوافق مع Back4App ──────────
if __name__ == "__main__":
    # السيرفر يسحب المنفذ تلقائياً من البيئة (غالباً 8080)
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
