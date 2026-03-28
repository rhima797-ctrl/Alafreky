FROM python:3.13-slim

# تثبيت الأدوات اللازمة للنظام
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت المكتبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ الكود
COPY . .

# أمر التشغيل المباشر (تأكدي أن اسم الملف هو main.py)
CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "120", "-b", "0.0.0.0:8080", "main:app"]
