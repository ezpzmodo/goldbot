# Python 3.11 슬림 버전 사용
FROM python:3.11-slim

# 작업 디렉터리 설정
WORKDIR /app

# 필요한 파일 복사
COPY . /app

# 필요한 패키지 설치
RUN pip install --no-cache-dir -r requirements.txt

# 봇 메인 파일 실행
CMD ["python", "main.py"]