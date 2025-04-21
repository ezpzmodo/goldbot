import asyncio
from datetime import datetime, timezone, timedelta
import requests
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.errors.common import InvalidBufferError
from telethon.tl.functions.channels import GetParticipantsRequest, EditBannedRequest
from telethon.tl.types import ChannelParticipantsRecent, ChatBannedRights

# ─── 설정 ─────────────────────────────────────────
API_ID              = 27789835
API_HASH            = "8dc2f1fb271540418f1a0fc7dacc1f4e"
BOT_TOKEN           = "7676020228:AAEH3QPba6zHg4lgyDgVoDtfLfJDdzBMwYA"
AUTHORIZED_USERNAME = "cuz_z"   # 이 계정만 /banclock 실행 가능

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

# ─── Telethon 클라이언트 시작 ─────────────────────
client = TelegramClient('banclock_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ─── 입장 요청 전부 거절 ────────────────────────────
def decline_all_requests(chat_id: int) -> int:
    """
    Bot API를 통해 getChatJoinRequests → declineChatJoinRequest 호출
    반환: 거절된 요청 건수
    """
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    resp = requests.get(f"{base}/getChatJoinRequests", params={"chat_id": chat_id})
    if not resp.ok:
        print("getChatJoinRequests failed:", resp.text)
        return 0

    reqs = resp.json().get("result", {}).get("join_requests", [])
    for req in reqs:
        uid = req["user"]["id"]
        d = requests.post(f"{base}/declineChatJoinRequest", params={
            "chat_id": chat_id,
            "user_id": uid
        })
        if not d.ok:
            print(f"Failed to decline {uid}:", d.text)

    return len(reqs)

# ─── /banclock 핸들러 ─────────────────────────────────
@client.on(events.NewMessage(pattern=r'^/banclock$'))
async def banclock_handler(event):
    # 1) 그룹에서만 동작
    if not event.is_group:
        return await event.reply("❌ 그룹에서만 사용 가능합니다.")
    # 2) 실행 권한 확인
    sender = await event.get_sender()
    if sender.username != AUTHORIZED_USERNAME:
        return await event.reply(f"❌ 이 명령어는 @{AUTHORIZED_USERNAME}만 사용할 수 있습니다.")

    # 3) 입장 요청 거절
    count = decline_all_requests(event.chat_id)
    await event.reply(f"✅ 입장 요청 {count}건 거절 완료. 밴 작업 시작…")

    # 4) 컷오프 이후 가입자 수집
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
            join_date = getattr(p, 'date', None)
            if join_date and join_date > CUTOFF:
                to_ban.append(p.user_id)
        offset += len(resp.participants)
        await asyncio.sleep(0.2)  # 페이지별 호출 분산

    to_ban = list(set(to_ban))
    total = len(to_ban)
    if total == 0:
        return await event.reply("✅ 밴 대상이 없습니다.")

    # 5) 실제 밴 처리 (429/Flood 처리 + throttle)
    success = fail = 0
    for uid in to_ban:
        try:
            await client(EditBannedRequest(event.chat_id, uid, ban_rights))
            success += 1
        except FloodWaitError as fw:
            await asyncio.sleep(fw.seconds + 1)
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
        except Exception:
            fail += 1
        await asyncio.sleep(0.3)  # 밴 요청 분산

    # 6) 최종 리포트
    await event.reply(f"✅ 완료! 밴 성공: {success}명, 실패: {fail}명")

# ─── /start, /help 핸들러 ─────────────────────────────
@client.on(events.NewMessage(pattern=r'^/(start|help)$'))
async def help_handler(event):
    await event.reply(
        "/banclock — 2025‑04‑21 15:30 KST 이후 입장한 멤버 전부 밴\n"
        "입장 요청도 동시에 전부 거절합니다."
    )

print("Bot is up. Listening for /banclock …")
client.run_until_disconnected()