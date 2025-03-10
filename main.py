from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackContext, filters
import os
import asyncio
import nest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import pytz
import logging

# 로그 설정
logging.basicConfig(level=logging.INFO)

# 이미 실행 중인 이벤트 루프에 중첩 허용
nest_asyncio.apply()

# 메시지 카운트를 저장할 딕셔너리
message_count = {}

# 한국 시간대 설정 (Asia/Seoul)
KST = pytz.timezone('Asia/Seoul')

# 메시지 감지 핸들러 (텍스트, 스티커 포함)
async def count_messages(update: Update, context: CallbackContext):
    if update.message:
        user_id = update.message.from_user.id
        first_name = update.message.from_user.first_name or ""
        last_name = update.message.from_user.last_name or ""
        
        # 성과 이름을 결합 (빈 값은 자동으로 무시)
        user_name = f"{first_name} {last_name}".strip()
        
        # 메시지 카운트 증가
        if user_id in message_count:
            message_count[user_id]['count'] += 1
        else:
            message_count[user_id] = {'name': user_name, 'count': 1}

# 순위 표시 핸들러 (1등부터 10등까지 표시)
async def show_ranking(update: Update, context: CallbackContext):
    global message_count
    ranking = sorted(message_count.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
    
    if not ranking:
        await update.message.reply_text("아직 메시지가 없습니다.")
        return

    # 한국 표준시 (KST) 시간으로 날짜 표시
    current_time = datetime.now(KST).strftime('%Y-%m-%d')
    ranking_message = f"📊 {current_time} 채팅 순위 (1등부터 10등까지):\n"
    
    for i, (user_id, data) in enumerate(ranking, start=1):
        if i == 1:
            ranking_message += f"🥇 1등: {data['name']} - {data['count']}개 메시지\n"
        elif i == 2:
            ranking_message += f"🥈 2등: {data['name']} - {data['count']}개 메시지\n"
        elif i == 3:
            ranking_message += f"🥉 3등: {data['name']} - {data['count']}개 메시지\n"
        else:
            ranking_message += f"{i}등: {data['name']} - {data['count']}개 메시지\n"

    await update.message.reply_text(ranking_message)

# 메시지 카운트 초기화
async def reset_message_count():
    global message_count
    message_count.clear()  # 기존 데이터를 완전히 초기화
    current_time = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    print(f"메시지 카운트가 초기화되었습니다. ({current_time})")
    logging.info(f"메시지 카운트가 초기화되었습니다. ({current_time})")

# 시작 메시지 핸들러
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("안녕하세요! 이 봇은 하루 단위로 채팅 순위를 보여줍니다. /ranking 명령어를 사용해보세요.")

# 메인 함수
async def main():
    # 환경변수에서 API 토큰 가져오기
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("Error: TELEGRAM_TOKEN 환경 변수를 설정해주세요.")
        return

    app = ApplicationBuilder().token(token).build()
    
    # 핸들러 등록 (텍스트와 스티커 모두 감지)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ranking", show_ranking))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, count_messages))  # 텍스트 메시지 감지
    app.add_handler(MessageHandler(filters.Sticker.ALL, count_messages))  # 스티커 메시지 감지

    # 스케줄러 설정 (매일 자정에 초기화, 한국 시간 기준)
    scheduler = AsyncIOScheduler(timezone='Asia/Seoul')
    scheduler.add_job(reset_message_count, 'cron', hour=0, minute=0)
    scheduler.start()
    
    # 봇 시작
    await app.run_polling()

# 비동기 함수 실행
asyncio.run(main())