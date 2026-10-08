FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -r app && mkdir /data && chown app /data
USER app
# DB·secret.key는 /data 볼륨에 둔다. TZ=KST-9: tzdata 없이 한국시간(점검 날짜가 자정 기준으로 바뀜)
ENV CHECKLIST_DB=/data/checklist.db CHECKLIST_SECRET=/data/secret.key TZ=KST-9 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python", "app.py"]
