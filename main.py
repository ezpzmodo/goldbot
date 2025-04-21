import asyncio
import requests
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.errors.common import InvalidBufferError
from telethon.tl.functions.channels import GetParticipantsRequest, EditBannedRequest
from telethon.tl.types import ChannelParticipantsRecent, ChatBannedRights

# ─── 설정 ─────────────────────────────────────────
API_ID              = 27789835
API_HASH            = "8dc2f1fb271540418f1a0fc7dacc1f4e"
BOT_TOKEN           = "7676020228:AAEH3QPba6zHg4lgyDgVoDtfLfJDdzBMwYA"
AUTHORIZED_USERNAME = "cuz_z"   # @cuz_z만 명령어 실행 가능

# KST 기준 컷오프: 2025‑04‑21 15:30
KST    = timezone(timedelta(hours=9))
CUTOFF = datetime(2025, 4, 21, 15, 30, tzinfo=KST)

# 영구 밴 권한 (추방 + 재가입 차단)
ban_rights = ChatBannedRights(
    until_date=None,
    view_messages=True, send_messages=True,
    send_media=True,   send_stickers=True,
    send_gifs=True,    send_games=True,
    send_inline=True,  embed_links=True
)

# ─── HTTP로 입장 요청 싹 거절 ─────────────────────
def decline_all_requests(chat_id: int):
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    # 1) 대기 중인 입장 요청 리스트 가져오기
    r = requests.get(f"{base}/getChatJoinRequests", params={"chat_id": chat_id})
    if not r.ok:
        return
    for req in r.json().get("result", {}).get("join_requests", []):
        uid = req["user"]["id"]
        # 2) 하나씩 거절
        requests.get(f"{base}/declineChatJoinRequest", params={
            "chat_id": chat_id,
            "user_id": uid
        })

# ─── Telethon 클라이언트 시작 ─────────────────────
client = TelegramClient('banclock_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

@client.on(events.NewMessage(pattern=r'^/banclock$'))
async def banclock_handler(event):
    # 1) 그룹 전용 & 실행 권한
    if not event.is_group:
        return await event.reply("❌ 그룹에서만 사용 가능합니다.")
    sender = await event.get_sender()
    if sender.username != AUTHORIZED_USERNAME:
        return await event.reply(f"❌ @{AUTHORIZED_USERNAME}만 사용할 수 있습니다.")

    await event.reply("⏳ 작업 시작: 입장 요청 거절 중…")
    # — 입장 요청 거절 (sync) —
    decline_all_requests(event.chat_id)
    await event.reply("✅ 모든 입장 요청 거절 완료. 멤버 밴 진행…")

    # 2) 컷오프 이후 입장자 수집
    offset = 0
    limit = 200
    to_ban = []
    while True:
        resp = await client(GetParticipantsRequest(
            channel=event.chat_id,
            filter=ChannelParticipantsRecent(),
            offset=offset,
            limit=limit,
            hash=0
        ))
        if not resp.participants:
            break
        for p in resp.participants:
            jd = getattr(p, 'date', None)
            if jd and jd > CUTOFF:
                to_ban.append(p.user_id)
        offset += len(resp.participants)
        await asyncio.sleep(0.2)  # 페이지 호출 분산

    to_ban = list(set(to_ban))
    if not to_ban:
        return await event.reply("✅ 밴 대상이 없습니다.")

    # 3) 밴 루프 (429 & Flood 대기 & throttle)
    success = fail = 0
    for uid in to_ban:
        try:
            await client(EditBannedRequest(event.chat_id, uid, ban_rights))
            success += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                await client(EditBannedRequest(event.chat_id, uid, ban_rights))
                success += 1
            except:
                fail += 1
        except InvalidBufferError:
            await asyncio.sleep(10)
            try:
                await client(EditBannedRequest(event.chat_id, uid, ban_rights))
                success += 1
            except:
                fail += 1
        except:
            fail += 1
        await asyncio.sleep(0.3)  # 호출량 분산

    await event.reply(f"✅ 완료! 밴 성공: {success}명, 실패: {fail}명")

@client.on(events.NewMessage(pattern=r'^/(start|help)$'))
async def help_handler(event):
    await event.reply(
        "/banclock — 2025‑04‑21 15:30 KST 이후 입장한 멤버 전부 밴\n"
        "명령어 실행 전, 봇에 '회원 차단' 권한을 주세요."
    )

print("Bot is up. Listening for /banclock …")
client.run_until_disconnected()