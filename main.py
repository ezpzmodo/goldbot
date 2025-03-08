from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackContext, filters
import os
import asyncio
import nest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

# 이미 실행 중인 이벤트 루프에 중첩 허용
nest_asyncio.apply()

# 메시지 카운트를 저장할 딕셔너리
message_count = {}

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

# 순위 표시 핸들러
async def show_ranking(update: Update, context: CallbackContext):
    ranking = sorted(message_count.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
    
    if not ranking:
        await update.message.reply_text("아직 메시지가 없습니다.")
        return

    ranking_message = f"📊 {datetime.now().strftime('%Y-%m-%d')} 메시지 순위 (상위 10명):\n"
    for i, (user_id, data) in enumerate(ranking, start=1):
        ranking_message += f"{i}. {data['name']} - {data['count']}개 메시지\n"

    await update.message.reply_text(ranking_message)

# 메시지 카운트 초기화
async def reset_message_count():
    global message_count
    message_count = {}
    print("메시지 카운트가 초기화되었습니다.")

# 시작 메시지 핸들러
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("안녕하세요! 이 봇은 하루 단위로 채팅 순위를 보여줍니다. /ranking 명령어를 사용해보세요.")

# 메인 함수
async def main():
    # Replit 시크릿 환경변수에서 API 토큰 가져오기
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

    # 스케줄러 설정 (매일 자정에 초기화)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(reset_message_count, 'cron', hour=0, minute=0, timezone='Asia/Seoul')
    scheduler.start()
    
    # 봇 시작
    await app.run_polling()

# 비동기 함수 실행
asyncio.run(main())