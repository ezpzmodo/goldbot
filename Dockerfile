# Python 3.10 이미지 사용
FROM python:3.10

# 작업 디렉터리 설정
WORKDIR /app

# 필요한 파일 복사
COPY . /app

# 필요한 패키지 설치
RUN pip install -r requirements.txt

# TELEGRAM_TOKEN 환경 변수 설정 (Fly.io의 Secrets 기능 사용)
ENV TELEGRAM_TOKEN=${TELEGRAM_TOKEN}

# 봇 실행
CMD ["python", "main.py"]