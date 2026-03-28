from flask import Flask, request, jsonify, Response
import requests as http_requests
import re
import os

app = Flask(__name__)

# --- تم إلغاء كود Firebase تماماً لضمان استقرار التشغيل ---

@app.route('/')
def home():
    return "السيرفر يعمل بنجاح بدون Firebase!", 200

@app.route('/test')
def test():
    return jsonify({"status": "success", "message": "API is online"}), 200

# --- إعدادات التشغيل الخاصة بـ Back4App ---
if __name__ == "__main__":
    # السيرفر يسحب المنفذ تلقائياً (غالباً 8080)
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
