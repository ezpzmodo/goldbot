import asyncio
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetParticipantsRequest, EditBannedRequest
from telethon.tl.types import ChannelParticipantsRecent, ChatBannedRights

# ─── 필수 설정 ─────────────────────────────────────
API_ID    = 27789835      # 본인 API ID (Telethon은 BOT_TOKEN 단독으로는 못 쓰고, API_ID/API_HASH 필요)
API_HASH  = "8dc2f1fb271540418f1a0fc7dacc1f4e"      # 본인 API HASH
BOT_TOKEN = "7676020228:AAEH3QPba6zHg4lgyDgVoDtfLfJDdzBMwYA"     # 봇 토큰만으로는 사용자 조회 기능이 제한돼서 Telethon은 위 3개가 필요합니다.

# KST 기준 컷오프: 2025-04-21 15:30
KST    = timezone(timedelta(hours=9))
CUTOFF = datetime(2025, 4, 21, 15, 30, tzinfo=KST)

# 영구 밴 권한
ban_rights = ChatBannedRights(
    until_date=None,
    view_messages=True,
    send_messages=True,
    send_media=True,
    send_stickers=True,
    send_gifs=True,
    send_games=True,
    send_inline=True,
    embed_links=True
)

# ─── 클라이언트 시작 ─────────────────────────────────
client = TelegramClient('banclock_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ─── /banclock 핸들러 ─────────────────────────────────
@client.on(events.NewMessage(pattern=r'^/banclock$'))
async def banclock_handler(event):
    # 그룹에서만 작동
    if not event.is_group:
        return await event.reply("❌ 그룹에서만 사용 가능해요.")
    chat_id = event.chat_id

    await event.reply("⏳ 밴 작업 시작합니다…")

    # 가입자 전부 스캔
    offset = 0
    limit = 200
    to_ban = []
    while True:
        res = await client(GetParticipantsRequest(
            channel=chat_id,
            filter=ChannelParticipantsRecent(),
            offset=offset,
            limit=limit,
            hash=0
        ))
        if not res.participants:
            break
        for p in res.participants:
            # 가입 일시가 컷오프 이후면 밴 대기 리스트에 추가
            if p.date and p.date > CUTOFF:
                to_ban.append(p.user_id)
        offset += len(res.participants)

    to_ban = list(set(to_ban))
    total = len(to_ban)
    if total == 0:
        return await event.reply("✅ 밴 대상이 없습니다.")

    # 한 번에 밴
    tasks = [
        client(EditBannedRequest(chat_id, user_id, ban_rights))
        for user_id in to_ban
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    await event.reply(f"✅ 총 {total}명 밴(추방+재가입 차단) 완료했습니다.")

# ─── 도움말 ─────────────────────────────────────────
@client.on(events.NewMessage(pattern=r'^/(start|help)$'))
async def help_handler(event):
    await event.reply("/banclock — 2025‑04‑21 15:30 KST 이후 입장한 멤버 전부 한 번에 밴")

print("Bot is up. Listening for /banclock …")
client.run_until_disconnected()