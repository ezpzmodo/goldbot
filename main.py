#!/usr/bin/env python3
import asyncio
import logging
import requests
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.errors.common import InvalidBufferError
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import (
    ChatBannedRights,
    ChannelParticipantsAdmins,
    ChannelParticipantsRecent,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── 설정 ─────────────────────────────────────────
API_ID            = 27789835
API_HASH          = "8dc2f1fb271540418f1a0fc7dacc1f4e"
BOT_TOKEN         = "7676020228:AAEH3QPba6zHg4lgyDgVoDtfLfJDdzBMwYA"
BOT_USERNAME      = "your_bot_username"   # @ 포함 말고, ex) mybot
AUTHORIZED_USER   = "cuz_z"               # 이 사용자만 실행 가능

# 컷오프 시각 (KST) — 2025‑04‑21 15:30
KST    = timezone(timedelta(hours=9))
CUTOFF = datetime(2025, 4, 21, 15, 30, tzinfo=KST)

# 밴 권한: 추방 + 재가입 차단
ban_rights = ChatBannedRights(
    until_date=None,
    view_messages=True, send_messages=True,
    send_media=True,   send_stickers=True,
    send_gifs=True,    send_games=True,
    send_inline=True,  embed_links=True
)

# 중단 플래그
stop_flag = False

# ─── Telethon 클라이언트 시작 ─────────────────────
client = TelegramClient('banclock_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

def decline_all_requests(chat_id: int) -> int:
    """
    Bot API로 pending 입장 요청을 전부 페이지 처리하며 거절합니다.
    chat_id를 '-100...' 형식으로 맞춰서 호출해야 합니다.
    """
    # Bot API용 chat_id
    api_chat_id = str(chat_id)
    if not api_chat_id.startswith('-100'):
        api_chat_id = f"-100{api_chat_id}"

    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    total_declined = 0
    limit = 100
    offset_request = None

    while True:
        params = {"chat_id": api_chat_id, "limit": limit}
        if offset_request:
            params["offset_request"] = offset_request

        resp = requests.get(f"{base}/getChatJoinRequests", params=params)
        # 404이면 이 채팅은 지원 안 하는 걸로 간주
        if resp.status_code == 404:
            break
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("getChatJoinRequests failed: %s", e)
            break

        if not data.get("ok"):
            logger.error("getChatJoinRequests error: %s", data.get("description"))
            break

        # Bot API v6.7+: result가 바로 리스트로 옴
        reqs = data.get("result", [])
        if not isinstance(reqs, list) or not reqs:
            break

        for req in reqs:
            rid = req.get("request_id")
            uid = req["user"]["id"]
            decline = requests.post(
                f"{base}/declineChatJoinRequest",
                json={"chat_id": api_chat_id, "user_id": uid}
            )
            if decline.ok:
                total_declined += 1
            else:
                logger.warning("Decline failed %s: %s", uid, decline.text)
            offset_request = rid  # 다음 페이지 기준

    return total_declined

@client.on(events.NewMessage(
    pattern=fr"^/banclock(?:@{BOT_USERNAME})?$"
))
async def banclock_handler(event):
    global stop_flag
    # 그룹+권한 체크
    if not event.is_group:
        return await event.reply("❌ 그룹에서만 사용 가능합니다.")
    sender = await event.get_sender()
    if sender.username != AUTHORIZED_USER:
        return await event.reply(f"❌ @{AUTHORIZED_USER}만 실행할 수 있습니다.")

    chat_id = event.chat_id
    stop_flag = False

    # 1) Pending 입장 요청 거절
    await event.reply("⏳ Pending 입장 요청 거절 중…")
    declined = decline_all_requests(chat_id)
    await event.reply(f"✅ 요청 {declined}건 거절 완료. 밴 작업 시작…")

    # 2) 관리자 목록 (밴 제외)
    admin_ids = {u.id async for u in client.iter_participants(
        chat_id, filter=ChannelParticipantsAdmins
    )}

    # 3) 컷오프 이후 입장자만 골라 밴
    success = fail = 0
    async for user in client.iter_participants(
        chat_id, filter=ChannelParticipantsRecent
    ):
        if stop_flag:
            break

        uid = user.id
        join_date = getattr(user, "date", None)
        # 컷오프 이전 or 관리자면 건너뛰기
        if not join_date or join_date <= CUTOFF or uid in admin_ids:
            continue

        try:
            await client(EditBannedRequest(chat_id, uid, ban_rights))
            success += 1
        except FloodWaitError as fw:
            await asyncio.sleep(fw.seconds + 1)
            try:
                await client(EditBannedRequest(chat_id, uid, ban_rights))
                success += 1
            except:
                fail += 1
        except InvalidBufferError:
            await asyncio.sleep(10)
            try:
                await client(EditBannedRequest(chat_id, uid, ban_rights))
                success += 1
            except:
                fail += 1
        except Exception as e:
            logger.warning("Ban failed for %s: %s", uid, e)
            fail += 1

        await asyncio.sleep(0.3)  # rate‑limit 방지

    # 4) 최종 리포트
    if stop_flag:
        await event.reply(f"🛑 중단됨: 성공 {success}명, 실패 {fail}명")
    else:
        await event.reply(f"✅ 밴 완료: 성공 {success}명, 실패 {fail}명")

@client.on(events.NewMessage(
    pattern=fr"^/stopclock(?:@{BOT_USERNAME})?$"
))
async def stopclock_handler(event):
    global stop_flag
    # 그룹+권한 체크
    if not event.is_group:
        return await event.reply("❌ 그룹에서만 사용 가능합니다.")
    sender = await event.get_sender()
    if sender.username != AUTHORIZED_USER:
        return await event.reply(f"❌ @{AUTHORIZED_USER}만 실행할 수 있습니다.")

    stop_flag = True
    await event.reply("🛑 중단 요청 접수 — 현재 작업을 멈춥니다.")

@client.on(events.NewMessage(pattern=r"^/(start|help)$"))
async def help_handler(event):
    await event.reply(
        f"/banclock@{BOT_USERNAME} — 4/21 15:30 KST 이후 입장한 멤버 모두 밴\n"
        f"/stopclock@{BOT_USERNAME} — 진행 중인 작업 즉시 중단"
    )

if __name__ == "__main__":
    print("Bot is up. Listening for /banclock and /stopclock …")
    client.run_until_disconnected()