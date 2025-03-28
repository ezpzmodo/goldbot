import os
import logging
import datetime
import random
import asyncio
import pytz

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
    ContextTypes
)

from apscheduler.schedulers.background import BackgroundScheduler

###############################################################################
# 0. 환경변수 & 기본설정
###############################################################################
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_URL = os.environ.get("DATABASE_URL", "")
SECRET_ADMIN_KEY = os.environ.get("SECRET_ADMIN_KEY", "MY_SUPER_SECRET")
MY_USER_ID = os.environ.get("MY_USER_ID","")  # /온 명령어를 사용할 수 있는 유일한 사람

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")
if not MY_USER_ID:
    raise ValueError("MY_USER_ID not set!")

MY_USER_ID = int(MY_USER_ID)  # 문자열이면 정수 변환

KST = pytz.timezone("Asia/Seoul")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

###############################################################################
# 1. DB 연결 & 초기화
###############################################################################
def get_db_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_conn()
    c = conn.cursor()

    # 유저
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id BIGINT PRIMARY KEY,
      username TEXT,
      is_subscribed BOOLEAN DEFAULT FALSE,
      is_admin BOOLEAN DEFAULT FALSE,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    # 그룹별 봇 활성화 여부
    c.execute("""
    CREATE TABLE IF NOT EXISTS group_settings (
      group_id BIGINT PRIMARY KEY,
      bot_enabled BOOLEAN DEFAULT FALSE
    );
    """)

    # 마피아
    c.execute("""
    CREATE TABLE IF NOT EXISTS mafia_sessions (
      session_id TEXT PRIMARY KEY,
      status TEXT,         -- waiting / night / day / ended
      group_id BIGINT,
      created_at TIMESTAMP DEFAULT NOW(),
      day_duration INT DEFAULT 60,
      night_duration INT DEFAULT 30,
      host_user_id BIGINT
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS mafia_players (
      session_id TEXT,
      user_id BIGINT,
      role TEXT,            -- Mafia/Police/Doctor/Citizen/dead
      is_alive BOOLEAN DEFAULT TRUE,
      vote_target BIGINT DEFAULT 0,
      heal_target BIGINT DEFAULT 0,
      investigate_target BIGINT DEFAULT 0,
      PRIMARY KEY(session_id,user_id)
    );
    """)

    # RPG
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_characters (
      user_id BIGINT PRIMARY KEY,
      job TEXT,
      level INT DEFAULT 1,
      exp INT DEFAULT 0,
      hp INT DEFAULT 100,
      max_hp INT DEFAULT 100,
      atk INT DEFAULT 10,
      gold INT DEFAULT 0,
      skill_points INT DEFAULT 0,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_skills (
      skill_id SERIAL PRIMARY KEY,
      name TEXT,
      job TEXT,
      required_level INT DEFAULT 1,
      damage INT DEFAULT 0,
      heal INT DEFAULT 0,
      mana_cost INT DEFAULT 0
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_learned_skills (
      user_id BIGINT,
      skill_id INT,
      PRIMARY KEY(user_id, skill_id)
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_items (
      item_id SERIAL PRIMARY KEY,
      name TEXT,
      price INT,
      atk_bonus INT DEFAULT 0,
      hp_bonus INT DEFAULT 0,
      required_job TEXT DEFAULT ''
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_inventory (
      user_id BIGINT,
      item_id INT,
      quantity INT DEFAULT 1,
      PRIMARY KEY(user_id, item_id)
    );
    """)

    # 일일채팅 (그룹별)
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_chat_count (
      user_id BIGINT,
      group_id BIGINT,
      date_str TEXT,
      count INT DEFAULT 0,
      PRIMARY KEY(user_id, group_id, date_str)
    );
    """)

    conn.commit()
    c.close()
    conn.close()

###############################################################################
# 2. 그룹 활성화
###############################################################################
def is_bot_enabled_in_group(gid:int)->bool:
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT bot_enabled FROM group_settings WHERE group_id=%s",(gid,))
    row=c.fetchone()
    c.close();conn.close()
    return (row and row["bot_enabled"])

def set_bot_enable_in_group(gid:int, val:bool):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    INSERT INTO group_settings(group_id,bot_enabled)
    VALUES(%s,%s)
    ON CONFLICT(group_id)
    DO UPDATE SET bot_enabled=%s
    """,(gid,val,val))
    conn.commit()
    c.close()
    conn.close()

###############################################################################
# 3. 유저/관리/구독
###############################################################################
def ensure_user_in_db(uid:int, fname:str, lname:str, tg_username:str):
    ff=(fname or "").strip()
    ll=(lname or "").strip()
    full=ff
    if ll:
        full+=" "+ll
    if not full.strip():
        if tg_username:
            full=f"@{tg_username}"
        else:
            full="이름없음"
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        c.execute("INSERT INTO users(user_id,username) VALUES(%s,%s)",(uid,full.strip()))
    else:
        if row["username"]!=full.strip():
            c.execute("UPDATE users SET username=%s WHERE user_id=%s",(full.strip(),uid))
    conn.commit()
    c.close()
    conn.close()

def is_admin_db(uid:int)->bool:
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT is_admin FROM users WHERE user_id=%s",(uid,))
    row=c.fetchone()
    c.close();conn.close()
    return (row and row["is_admin"])

def set_admin(uid:int,val:bool):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("UPDATE users SET is_admin=%s WHERE user_id=%s",(val,uid))
    conn.commit()
    c.close()
    conn.close()

def is_subscribed_db(uid:int)->bool:
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT is_subscribed FROM users WHERE user_id=%s",(uid,))
    row=c.fetchone()
    c.close()
    conn.close()
    return (row and row["is_subscribed"])

def set_subscribe(uid:int,val:bool):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("UPDATE users SET is_subscribed=%s WHERE user_id=%s",(val,uid))
    conn.commit()
    c.close()
    conn.close()

###############################################################################
# 4. 필터 (불량단어/링크/스팸), 환영퇴장
###############################################################################
BAD_WORDS=["금지어1","금지어2"]
user_message_times={}

async def welcome_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cmu:ChatMemberUpdated=update.chat_member
    if cmu.new_chat_member.status=="member":
        user=cmu.new_chat_member.user
        await context.bot.send_message(
            cmu.chat.id,
            f"환영합니다, {user.mention_html()}!",
            parse_mode="HTML"
        )
    elif cmu.new_chat_member.status in ("left","kicked"):
        user=cmu.new_chat_member.user
        await context.bot.send_message(
            cmu.chat.id,
            f"{user.full_name}님이 나갔습니다."
        )

async def filter_bad_words_and_spam_and_links(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        return
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    msg=update.message
    if not msg:return
    text=msg.text.lower()
    uid=update.effective_user.id

    for bad in BAD_WORDS:
        if bad in text:
            await msg.delete()
            return

    if ("http://" in text or "https://" in text) and (not is_admin_db(uid)):
        await msg.delete()
        return

    now_ts=datetime.datetime.now().timestamp()
    if uid not in user_message_times:
        user_message_times[uid]=[]
    user_message_times[uid].append(now_ts)
    threshold=now_ts-5
    user_message_times[uid]=[t for t in user_message_times[uid] if t>=threshold]
    if len(user_message_times[uid])>=10:
        await msg.delete()
        return

###############################################################################
# 5. 채팅랭킹(그룹별)
###############################################################################
def increment_daily_chat_count(uid:int,gid:int):
    now=datetime.datetime.now(tz=KST)
    ds=now.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    INSERT INTO daily_chat_count(user_id,group_id,date_str,count)
    VALUES(%s,%s,%s,1)
    ON CONFLICT(user_id,group_id,date_str)
    DO UPDATE SET count=daily_chat_count.count+1
    """,(uid,gid,ds))
    conn.commit()
    c.close()
    conn.close()

def reset_daily_chat_count():
    now=datetime.datetime.now(tz=KST)
    y= now - datetime.timedelta(days=1)
    ys=y.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("DELETE FROM daily_chat_count WHERE date_str=%s",(ys,))
    conn.commit()
    c.close()
    conn.close()

def get_daily_ranking_text(gid:int)->str:
    now=datetime.datetime.now(tz=KST)
    ds=now.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT dc.user_id,dc.count,u.username
    FROM daily_chat_count dc
    LEFT JOIN users u ON u.user_id=dc.user_id
    WHERE dc.group_id=%s AND dc.date_str=%s
    ORDER BY dc.count DESC
    LIMIT 10
    """,(gid,ds))
    rows=c.fetchall()
    c.close()
    conn.close()
    if not rows:
        return f"오늘({ds}) 채팅 기록 없음."
    msg=f"=== 오늘({ds}) 채팅랭킹 ===\n"
    rank=1
    for r in rows:
        uname=r["username"] or "이름없음"
        cnt=r["count"]
        if rank==1: prefix="🥇"
        elif rank==2: prefix="🥈"
        elif rank==3: prefix="🥉"
        else: prefix=f"{rank}위:"
        msg+=f"{prefix} {uname}({cnt}회)\n"
        rank+=1
    return msg

###############################################################################
# 6. 명령어 (영문)
###############################################################################
async def start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    uid=user.id
    ensure_user_in_db(uid,user.first_name or "",user.last_name or "",user.username or "")

    owner=str(uid)
    text=(
        "다기능 봇.\n"
        "아래 인라인 버튼은 호출자만 조작 가능."
    )
    kb=[
      [
        InlineKeyboardButton("🎮 게임",callback_data=f"{owner}|menu_games"),
        InlineKeyboardButton("🔧 그룹관리",callback_data=f"{owner}|menu_group")
      ],
      [
        InlineKeyboardButton("💳 구독",callback_data=f"{owner}|menu_subscribe"),
        InlineKeyboardButton("📊 채팅랭킹",callback_data=f"{owner}|menu_ranking")
      ]
    ]
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    msg=(
        "[help]\n"
        "/start, /help\n"
        "/adminsecret <키> => 관리자 권한 획득\n"
        "/adminon <유저ID>, /adminoff <유저ID> => 관리자 부여/박탈(관리자만)\n"
        "/announce <메시지> => 공지(관리자)\n"
        "/subscribe_toggle => 구독 토글\n"
        "/vote <주제> => 투표\n"
        f"/온 => 이 그룹 봇기능 활성(오직 user_id={MY_USER_ID}만 가능)\n\n"
        "한글명령어 => /시작, /도움말, /랭킹, /마피아시작, /참가, /방나가기, /마피아목록...\n"
        "RPG => /rpg생성, /rpg직업선택, /던전, /상점, /인벤토리, /내정보 등"
    )
    await update.message.reply_text(msg)

async def admin_secret_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("ex)/adminsecret KEY")
        return
    if args[0]==SECRET_ADMIN_KEY:
        set_admin(update.effective_user.id, True)
        await update.message.reply_text("관리자 권한 획득!")
    else:
        await update.message.reply_text("비밀키 불일치.")

async def admin_on_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not is_admin_db(update.effective_user.id):
        await update.message.reply_text("관리자만 가능")
        return
    args=context.args
    if not args:
        await update.message.reply_text("사용:/adminon <유저ID>")
        return
    try:
        target=int(args[0])
    except:
        await update.message.reply_text("숫자오류")
        return
    set_admin(target, True)
    await update.message.reply_text(f"{target} 관리자 부여")

async def admin_off_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not is_admin_db(update.effective_user.id):
        await update.message.reply_text("관리자만 가능")
        return
    args=context.args
    if not args:
        await update.message.reply_text("사용:/adminoff <유저ID>")
        return
    try:
        target=int(args[0])
    except:
        await update.message.reply_text("숫자오류.")
        return
    set_admin(target,False)
    await update.message.reply_text(f"{target} 관리자 해제")

async def announce_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not is_admin_db(update.effective_user.id):
        await update.message.reply_text("관리자전용")
        return
    msg=" ".join(context.args)
    if not msg:
        await update.message.reply_text("공지내용?")
        return
    await update.message.reply_text(f"[공지]\n{msg}")

async def subscribe_toggle_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    old=is_subscribed_db(uid)
    set_subscribe(uid, not old)
    if old:
        await update.message.reply_text("구독 해제!")
    else:
        await update.message.reply_text("구독 ON!")

async def vote_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    topic=" ".join(context.args)
    if not topic:
        await update.message.reply_text("사용:/vote <주제>")
        return
    kb=[[InlineKeyboardButton("👍",callback_data=f"vote_yes|{topic}"),
         InlineKeyboardButton("👎",callback_data=f"vote_no|{topic}")]]
    await update.message.reply_text(f"[투표]\n{topic}", reply_markup=InlineKeyboardMarkup(kb))

async def bot_on_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    uid=user.id
    if uid!=MY_USER_ID:
        await update.message.reply_text("권한없음.")
        return
    if update.effective_chat.type not in ("group","supergroup"):
        await update.message.reply_text("그룹에서만!")
        return
    gid=update.effective_chat.id
    set_bot_enable_in_group(gid,True)
    await update.message.reply_text("이 그룹에서 봇 기능 활성화!")

async def vote_callback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    parts=data.split("|",1)
    if len(parts)<2:return
    vt,topic=parts
    user=q.from_user
    if vt=="vote_yes":
        await q.edit_message_text(f"[투표]{topic}\n\n{user.first_name}님이 👍!")
    else:
        await q.edit_message_text(f"[투표]{topic}\n\n{user.first_name}님이 👎!")

###############################################################################
# 7. 한글 명령어(Regex)
###############################################################################
import re

async def hangeul_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)
async def hangeul_help_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)
async def hangeul_ranking_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    txt=get_daily_ranking_text(gid)
    await update.message.reply_text(txt)

###############################################################################
# 8. 마피아 로직
###############################################################################
MAFIA_DEFAULT_DAY_DURATION=60
MAFIA_DEFAULT_NIGHT_DURATION=30
mafia_tasks={}

def generate_mafia_session_id(group_id:int)->str:
    # 세션 ID에 group_id도 섞어서 유일성
    base=random.randint(0,999999999999)
    return f"{group_id}_{str(base).zfill(12)}"

# ... 세부함수들(세션생성, 참가, 나가기, 방삭제, force_start, cycle, etc.)

async def mafia_list_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT session_id, created_at
    FROM mafia_sessions
    WHERE status='waiting' AND group_id=%s
    ORDER BY created_at DESC
    """,(gid,))
    rows=c.fetchall()
    c.close();conn.close()
    if not rows:
        await update.message.reply_text("이 그룹에 대기중인 마피아 세션이 없음.")
        return
    txt="[대기중 마피아 세션]\n"
    kb=[]
    for r in rows:
        sid=r["session_id"]
        txt+=f"- {sid}\n"
        kb.append([InlineKeyboardButton(f"{sid} 참가", callback_data=f"mafia_join_{sid}")])
    await update.message.reply_text(txt,reply_markup=InlineKeyboardMarkup(kb))

async def mafia_list_join_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    if not data.startswith("mafia_join_"):
        await q.edit_message_text("잘못된 세션콜백.")
        return
    sid=data.split("_",2)[2]
    user=q.from_user
    uid=user.id
    ensure_user_in_db(uid, user.first_name or "", user.last_name or "", user.username or "")

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(sid,))
    sess=c.fetchone()
    if not sess or sess["status"]!="waiting":
        await q.edit_message_text("세션없거나이미시작.")
        c.close();conn.close()
        return
    if not is_bot_enabled_in_group(sess["group_id"]):
        await q.edit_message_text("그룹 미활성화.")
        c.close();conn.close()
        return

    c.execute("""
    SELECT ms.session_id
    FROM mafia_players mp
    JOIN mafia_sessions ms ON ms.session_id=mp.session_id
    WHERE mp.user_id=%s AND ms.status='waiting' AND ms.group_id=%s
    """,(uid,sess["group_id"]))
    already=c.fetchone()
    if already:
        await q.edit_message_text("이미 다른 대기방에 참가중.")
        c.close();conn.close()
        return

    c.execute("SELECT * FROM mafia_players WHERE session_id=%s AND user_id=%s",(sid,uid))
    row=c.fetchone()
    if row:
        await q.edit_message_text("이미 참가중.")
        c.close();conn.close()
        return
    c.execute("""
    INSERT INTO mafia_players(session_id,user_id,role)
    VALUES(%s,%s,%s)
    """,(sid,uid,"none"))
    conn.commit()
    c.execute("SELECT COUNT(*) as c FROM mafia_players WHERE session_id=%s",(sid,))
    n=c.fetchone()["c"]
    c.close();conn.close()
    await q.edit_message_text(f"세션 {sid} 참가완료. 현재 {n}명.")

async def mafia_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        return
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    user=update.effective_user
    uid=user.id

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT session_id FROM mafia_sessions
    WHERE status='waiting' AND host_user_id=%s AND group_id=%s
    """,(uid,gid))
    existing=c.fetchone()
    if existing:
        c.close();conn.close()
        await update.message.reply_text("이미 다른 방(대기중)을 만들었음. /방삭제 먼저.")
        return

    sess_id=generate_mafia_session_id(gid)
    c.execute("""
    INSERT INTO mafia_sessions(session_id,status,group_id,day_duration,night_duration,host_user_id)
    VALUES(%s,%s,%s,%s,%s,%s)
    """,(sess_id,"waiting",gid,MAFIA_DEFAULT_DAY_DURATION,MAFIA_DEFAULT_NIGHT_DURATION,uid))
    conn.commit()
    c.close();conn.close()

    await update.message.reply_text(
      f"마피아 세션 생성:{sess_id}\n"
      f"/참가 {sess_id}\n"
      f"/마피아강제시작 {sess_id}\n"
      f"/방삭제 {sess_id} 로 삭제 가능\n"
      "/마피아목록 으로 대기목록 확인"
    )

async def mafia_join_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        return
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    args=context.args
    if not args:
        await update.message.reply_text("사용:/참가 <세션ID>")
        return
    sess_id=args[0]
    user=update.effective_user
    uid=user.id
    ensure_user_in_db(uid, user.first_name or "",user.last_name or "",user.username or "")

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sess=c.fetchone()
    if not sess:
        await update.message.reply_text("세션없음.")
        c.close();conn.close()
        return
    if sess["group_id"]!=gid:
        await update.message.reply_text("이 그룹 세션 아님.")
        c.close();conn.close()
        return
    if sess["status"]!="waiting":
        await update.message.reply_text("이미 시작된 세션.")
        c.close();conn.close()
        return
    c.execute("""
    SELECT ms.session_id
    FROM mafia_players mp
    JOIN mafia_sessions ms ON ms.session_id=mp.session_id
    WHERE mp.user_id=%s AND ms.status='waiting' AND ms.group_id=%s
    """,(uid,gid))
    already=c.fetchone()
    if already:
        c.close();conn.close()
        await update.message.reply_text("이미 다른 대기세션 참가중.")
        return

    c.execute("SELECT * FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if row:
        await update.message.reply_text("이미참가중.")
        c.close();conn.close()
        return
    c.execute("""
    INSERT INTO mafia_players(session_id,user_id,role)
    VALUES(%s,%s,%s)
    """,(sess_id,uid,"none"))
    conn.commit()
    c.execute("SELECT COUNT(*) as c FROM mafia_players WHERE session_id=%s",(sess_id,))
    n=c.fetchone()["c"]
    c.close();conn.close()
    await update.message.reply_text(f"참가완료. 현재 {n}명 대기중.")

async def mafia_leave_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    args=context.args
    if not args:
        await update.message.reply_text("사용:/방나가기 <세션ID>")
        return
    sess_id=args[0]
    uid=update.effective_user.id

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sess=c.fetchone()
    if not sess:
        await update.message.reply_text("세션없음.")
        c.close();conn.close()
        return
    if sess["group_id"]!=gid:
        await update.message.reply_text("이 그룹세션 아님.")
        c.close();conn.close()
        return
    if sess["status"]!="waiting":
        await update.message.reply_text("이미 진행중이라 나가기 불가.")
        c.close();conn.close()
        return
    c.execute("DELETE FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    rowcount=c.rowcount
    conn.commit()
    c.close();conn.close()
    if rowcount>0:
        await update.message.reply_text(f"{sess_id} 방 나가기 완료.")
    else:
        await update.message.reply_text("그 세션에 참가중이 아님.")

async def mafia_delete_room(update:Update, context:ContextTypes.DEFAULT_TYPE):
    # /방삭제 <세션ID>
    # 본인 or 관리자
    if update.effective_chat.type not in ("group","supergroup"):
        return
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    args=context.args
    if not args:
        await update.message.reply_text("사용:/방삭제 <세션ID>")
        return
    sess_id=args[0]
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sess=c.fetchone()
    if not sess:
        c.close();conn.close()
        await update.message.reply_text("세션 없음.")
        return
    if sess["group_id"]!=gid:
        c.close();conn.close()
        await update.message.reply_text("이 그룹 세션 아님.")
        return
    if sess["status"]!="waiting":
        # 이미 시작 => 관리자만 삭제
        if not is_admin_db(uid):
            c.close();conn.close()
            await update.message.reply_text("이미 시작된 방. 관리자만 삭제 가능.")
            return
        # 관리자
        c.execute("DELETE FROM mafia_players WHERE session_id=%s",(sess_id,))
        c.execute("DELETE FROM mafia_sessions WHERE session_id=%s",(sess_id,))
        conn.commit()
        c.close();conn.close()
        await update.message.reply_text("진행중인 방을 관리자 권한으로 삭제.")
        return
    # 대기중
    if (uid==sess["host_user_id"]) or is_admin_db(uid):
        c.execute("DELETE FROM mafia_players WHERE session_id=%s",(sess_id,))
        c.execute("DELETE FROM mafia_sessions WHERE session_id=%s",(sess_id,))
        conn.commit()
        c.close();conn.close()
        await update.message.reply_text("대기중 방 삭제완료.")
    else:
        c.close();conn.close()
        await update.message.reply_text("본인방이 아님(또는 관리자아님). 삭제불가.")

async def mafia_force_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    # ...(동일)...
    args=context.args
    if not args or len(args)<1:
        await update.message.reply_text("사용:/마피아강제시작 <세션ID>")
        return
    sess_id=args[0]
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sess=c.fetchone()
    if not sess or sess["group_id"]!=gid or sess["status"]!="waiting":
        await update.message.reply_text("세션없거나이미시작(또는 다른그룹).")
        c.close();conn.close()
        return
    c.execute("SELECT user_id FROM mafia_players WHERE session_id=%s",(sess_id,))
    rows=c.fetchall()
    players=[r["user_id"] for r in rows]
    if len(players)<5:
        await update.message.reply_text("최소5명(마피아/경찰/의사/시민2+)")
        c.close();conn.close()
        return
    random.shuffle(players)
    mafia_id=players[0]
    police_id=players[1]
    doctor_id=players[2]
    for pid in players:
        if pid==mafia_id: role="Mafia"
        elif pid==police_id: role="Police"
        elif pid==doctor_id: role="Doctor"
        else: role="Citizen"
        c.execute("""
        UPDATE mafia_players
        SET role=%s,is_alive=TRUE,vote_target=0,heal_target=0,investigate_target=0
        WHERE session_id=%s AND user_id=%s
        """,(role,sess_id,pid))
    c.execute("UPDATE mafia_sessions SET status='night' WHERE session_id=%s",(sess_id,))
    conn.commit()
    group_id=sess["group_id"]
    c.close();conn.close()
    await update.message.reply_text(f"마피아게임시작! 세션:{sess_id}, 첫밤")

    # 역할 안내
    for pid in players:
        conn2=get_db_conn()
        c2=conn2.cursor()
        c2.execute("SELECT role FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,pid))
        rr=c2.fetchone()
        c2.close();conn2.close()
        if rr:
            ro=rr["role"]
            if ro=="Mafia":
                txt="당신은 [마피아] => 밤에 /살해 <세션ID> <유저ID>"
            elif ro=="Police":
                txt="당신은 [경찰] => 밤에 /조사 <세션ID> <유저ID>"
            elif ro=="Doctor":
                txt="당신은 [의사] => 밤에 /치료 <세션ID> <유저ID>"
            else:
                txt="당신은 [시민]"
            try:
                await context.bot.send_message(pid,txt)
            except:
                pass
    # cycle
    mafia_tasks[sess_id]=asyncio.create_task(mafia_cycle(sess_id, group_id, sess["day_duration"], sess["night_duration"], context))

async def mafia_cycle(session_id, group_id, day_dur, night_dur, context:ContextTypes.DEFAULT_TYPE):
    while True:
        await asyncio.sleep(night_dur)
        await resolve_night_actions(session_id, group_id, context)
        conn=get_db_conn()
        c=conn.cursor()
        c.execute("UPDATE mafia_sessions SET status='day' WHERE session_id=%s",(session_id,))
        conn.commit()
        c.close();conn.close()
        try:
            await context.bot.send_message(group_id, text=f"밤이끝! 낮({day_dur}초) 시작.\n/투표 <세션ID> <유저ID>")
        except:
            pass
        await asyncio.sleep(day_dur)
        ended=await resolve_day_vote(session_id, group_id, context)
        if ended: break
        conn2=get_db_conn()
        c2=conn2.cursor()
        c2.execute("UPDATE mafia_sessions SET status='night' WHERE session_id=%s",(session_id,))
        conn2.commit()
        c2.close();conn2.close()
        try:
            await context.bot.send_message(group_id, text=f"낮 끝. 밤({night_dur}초) 시작!")
        except:
            pass
        if check_mafia_win_condition(session_id):
            break

def check_mafia_win_condition(session_id:str)->bool:
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s",(session_id,))
    rows=c.fetchall()
    c.close();conn.close()
    alive_mafia=0
    alive_citizen=0
    for r in rows:
        if not r["is_alive"]:
            continue
        if r["role"]=="Mafia":
            alive_mafia+=1
        else:
            alive_citizen+=1
    return (alive_mafia==0 or alive_citizen==0)

async def resolve_night_actions(session_id, group_id, context:ContextTypes.DEFAULT_TYPE):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT user_id,role,is_alive,vote_target,heal_target,investigate_target
    FROM mafia_players
    WHERE session_id=%s
    """,(session_id,))
    rows=c.fetchall()
    mafia_kill_target=None
    doc_heals={}
    pol_invest={}
    for r in rows:
        if r["role"]=="Mafia" and r["is_alive"]:
            if r["vote_target"]!=0:
                mafia_kill_target=r["vote_target"]
        elif r["role"]=="Doctor" and r["is_alive"]:
            if r["heal_target"]!=0:
                doc_heals[r["user_id"]]=r["heal_target"]
        elif r["role"]=="Police" and r["is_alive"]:
            if r["investigate_target"]!=0:
                pol_invest[r["user_id"]]=r["investigate_target"]
    final_dead=None
    if mafia_kill_target:
        healed=any(doc_heals[k]==mafia_kill_target for k in doc_heals)
        if not healed:
            c.execute("""
            UPDATE mafia_players
            SET is_alive=FALSE, role='dead'
            WHERE session_id=%s AND user_id=%s
            """,(session_id, mafia_kill_target))
            final_dead=mafia_kill_target
    for pol_id, suspect_id in pol_invest.items():
        c.execute("SELECT role FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,suspect_id))
        sr=c.fetchone()
        if sr:
            try:
                await context.bot.send_message(pol_id,f"[조사결과]{suspect_id}:{sr['role']}")
            except:
                pass
    c.execute("""
    UPDATE mafia_players
    SET vote_target=0,heal_target=0,investigate_target=0
    WHERE session_id=%s
    """,(session_id,))
    conn.commit()
    c.close();conn.close()
    if final_dead:
        try:
            await context.bot.send_message(group_id, f"밤 사이에 {final_dead}님 사망.")
        except:
            pass

async def resolve_day_vote(session_id, group_id, context:ContextTypes.DEFAULT_TYPE):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT user_id,vote_target
    FROM mafia_players
    WHERE session_id=%s AND is_alive=TRUE AND vote_target<>0
    """,(session_id,))
    votes=c.fetchall()
    if not votes:
        c.close();conn.close()
        try:
            await context.bot.send_message(group_id,"투표가 없었습니다.")
        except:
            pass
        if check_mafia_win_condition(session_id):
            await context.bot.send_message(group_id,"게임종료.")
            return True
        return False
    vote_count={}
    for v in votes:
        vt=v["vote_target"]
        vote_count[vt]=vote_count.get(vt,0)+1
    sorted_v=sorted(vote_count.items(), key=lambda x:x[1], reverse=True)
    top_user, top_cnt=sorted_v[0]
    c.execute("""
    UPDATE mafia_players
    SET is_alive=FALSE,role='dead'
    WHERE session_id=%s AND user_id=%s
    """,(session_id,top_user))
    conn.commit()
    c.close();conn.close()
    try:
        await context.bot.send_message(group_id,f"{top_user}님이 {top_cnt}표로 처형.")
    except:
        pass
    if check_mafia_win_condition(session_id):
        await context.bot.send_message(group_id,"게임종료.")
        return True
    return False

async def mafia_kill_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/살해 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("ID오류.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Mafia" or not row["is_alive"]:
        await update.message.reply_text("마피아아님 or 사망.")
        c.close();conn.close()
        return
    c.execute("UPDATE mafia_players SET vote_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님을 살해 대상으로 설정.")

async def mafia_doctor_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/치료 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("ID오류")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Doctor" or not row["is_alive"]:
        await update.message.reply_text("의사가 아니거나 사망.")
        c.close();conn.close()
        return
    c.execute("UPDATE mafia_players SET heal_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님을 치료 대상으로 설정.")

async def mafia_police_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/조사 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("ID오류")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Police" or not row["is_alive"]:
        await update.message.reply_text("경찰X or 사망.")
        c.close();conn.close()
        return
    c.execute("UPDATE mafia_players SET investigate_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님 조사 대상으로 설정.")

async def mafia_vote_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        return
    gid=update.effective_chat.id
    if not is_bot_enabled_in_group(gid):
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/투표 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("ID오류")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT status,group_id FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sr=c.fetchone()
    if not sr or sr["group_id"]!=gid or sr["status"]!="day":
        await update.message.reply_text("낮상태가 아님 or 이 그룹세션X.")
        c.close();conn.close()
        return
    c.execute("SELECT is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    rr=c.fetchone()
    if not rr or not rr["is_alive"]:
        await update.message.reply_text("죽었거나 참가X.")
        c.close();conn.close()
        return
    c.execute("""
    UPDATE mafia_players
    SET vote_target=%s
    WHERE session_id=%s AND user_id=%s
    """,(tgt,sess_id,uid))
    conn.commit()
    c.close();conn.close()
    await update.message.reply_text(f"{tgt}님에게 투표.")

###############################################################################
# 9. RPG 로직
###############################################################################
rpg_fight_state={}
rpg_cooldown={}  # uid->timestamp

async def rpg_create_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup","private"):
        return
    gid=update.effective_chat.id
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(gid):
            return
    uid=update.effective_user.id
    ensure_user_in_db(uid, update.effective_user.first_name or "", update.effective_user.last_name or "", update.effective_user.username or "")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if row:
        await update.message.reply_text("이미 캐릭터 있음.")
        c.close();conn.close()
        return
    c.execute("""
    INSERT INTO rpg_characters(user_id,job,level,exp,hp,max_hp,atk,gold,skill_points)
    VALUES(%s,%s,1,0,100,100,10,100,0)
    """,(uid,"none"))
    conn.commit()
    c.close();conn.close()
    await update.message.reply_text("캐릭터생성 완료! /rpg직업선택 으로 직업 골라보세요.")

async def rpg_set_job_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    kb=[
      [InlineKeyboardButton("전사",callback_data="rpg_job_전사")],
      [InlineKeyboardButton("마법사",callback_data="rpg_job_마법사")],
      [InlineKeyboardButton("도적",callback_data="rpg_job_도적")]
    ]
    await update.message.reply_text("직업선택:",reply_markup=InlineKeyboardMarkup(kb))

async def rpg_job_callback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    if not data.startswith("rpg_job_"):
        await q.edit_message_text("직업콜백오류.")
        return
    job=data.split("_",2)[2]
    uid=q.from_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await q.edit_message_text("캐릭터 없음. /rpg생성")
        c.close();conn.close()
        return
    if row["job"]!="none":
        await q.edit_message_text("이미 직업 있음.")
        c.close();conn.close()
        return
    if job=="전사":
        hp=120; atk=12
    elif job=="마법사":
        hp=80; atk=15
    else:
        hp=100; atk=10
    c.execute("""
    UPDATE rpg_characters
    SET job=%s,hp=%s,max_hp=%s,atk=%s
    WHERE user_id=%s
    """,(job,hp,hp,atk,uid))
    conn.commit()
    c.close();conn.close()
    await q.edit_message_text(f"{job} 직업선택 완료!")

async def rpg_status_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await update.message.reply_text("캐릭터없음.")
        c.close();conn.close()
        return
    job=row["job"]
    lv=row["level"]
    xp=row["exp"]
    hp=row["hp"]
    mhp=row["max_hp"]
    atk=row["atk"]
    gold=row["gold"]
    sp=row["skill_points"]
    msg=(f"[캐릭터]\n직업:{job}\n"
         f"Lv:{lv}, EXP:{xp}/???\n"
         f"HP:{hp}/{mhp}, ATK:{atk}\n"
         f"Gold:{gold}, 스킬포인트:{sp}")
    await update.message.reply_text(msg)
    c.close();conn.close()

async def rpg_dungeon_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    uid=update.effective_user.id
    now_ts=datetime.datetime.now().timestamp()
    if uid in rpg_cooldown:
        if now_ts<rpg_cooldown[uid]:
            remain=int(rpg_cooldown[uid]-now_ts)
            await update.message.reply_text(f"던전 쿨다운 {remain}초 남음.")
            return
    kb=[
      [InlineKeyboardButton("슬라임굴(쉬움)",callback_data="rdsel_easy")],
      [InlineKeyboardButton("오크숲(보통)",callback_data="rdsel_normal")],
      [InlineKeyboardButton("드래곤둥지(어려움)",callback_data="rdsel_hard")]
    ]
    await update.message.reply_text("던전 난이도 선택:",reply_markup=InlineKeyboardMarkup(kb))

async def rpg_dungeon_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    uid=q.from_user.id
    now_ts=datetime.datetime.now().timestamp()
    if uid in rpg_cooldown:
        if now_ts<rpg_cooldown[uid]:
            remain=int(rpg_cooldown[uid]-now_ts)
            await q.edit_message_text(f"던전 쿨다운 {remain}초 남음.")
            return

    if data=="rdsel_easy":
        monster="슬라임"
        mhp=30;matk=5
        reward_exp=30; reward_gold=30
    elif data=="rdsel_normal":
        monster="오크"
        mhp=60;matk=10
        reward_exp=60; reward_gold=60
    else:
        monster="드래곤"
        mhp=120;matk=20
        reward_exp=120; reward_gold=120

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    char=c.fetchone()
    if not char:
        await q.edit_message_text("캐릭터없음.")
        c.close();conn.close()
        return

    # 레벨업 공식: lv^2 * 50
    # (전투 끝에 적용)
    # 아이템 ATK/HP
    base_hp=char["hp"]
    base_atk=char["atk"]

    c.execute("""
    SELECT inv.quantity,it.atk_bonus,it.hp_bonus,it.required_job
    FROM rpg_inventory inv
    JOIN rpg_items it ON it.item_id=inv.item_id
    WHERE inv.user_id=%s
    """,(uid,))
    invrows=c.fetchall()
    sum_atk=0
    sum_hp=0
    for i in invrows:
        # 만약 직업 제한이 있고, 맞지 않으면 무시
        reqjob=i["required_job"]
        if reqjob and reqjob!=char["job"]:
            continue
        sum_atk+=(i["atk_bonus"]*i["quantity"])
        sum_hp+=(i["hp_bonus"]*i["quantity"])
    p_hp=base_hp+sum_hp
    p_atk=base_atk+sum_atk
    c.close();conn.close()

    rpg_fight_state[uid]={
      "monster":monster,
      "m_hp":mhp,
      "m_atk":matk,
      "p_hp":p_hp,
      "p_atk":p_atk,
      "phase":"ongoing",
      "reward_exp":reward_exp,
      "reward_gold":reward_gold
    }

    kb=[
      [InlineKeyboardButton("공격",callback_data=f"rfd_{uid}_atk"),
       InlineKeyboardButton("도망",callback_data=f"rfd_{uid}_run")]
    ]
    await q.edit_message_text(f"{monster} 출현!\n몬스터HP:{mhp}, 내HP:{p_hp}\n행동선택:",reply_markup=InlineKeyboardMarkup(kb))

async def rpg_fight_action_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    parts=data.split("_")
    if len(parts)<3:return
    uidstr=parts[1]
    action=parts[2]
    user=q.from_user
    if str(user.id)!=uidstr:
        await q.answer("본인 전투가 아님!",show_alert=True)
        return
    st=rpg_fight_state.get(user.id)
    if not st or st["phase"]!="ongoing":
        await q.answer("전투없거나끝.")
        return

    monster=st["monster"]
    m_hp=st["m_hp"]
    m_atk=st["m_atk"]
    p_hp=st["p_hp"]
    p_atk=st["p_atk"]
    reward_exp=st["reward_exp"]
    reward_gold=st["reward_gold"]

    if action=="run":
        st["phase"]="end"
        await q.edit_message_text("도망쳤습니다. 전투끝!")
        return
    elif action=="atk":
        dmg_p=random.randint(p_atk-2,p_atk+2)
        if dmg_p<0:dmg_p=0
        m_hp-=dmg_p
        dmg_m=0
        if m_hp>0:
            dmg_m=random.randint(m_atk-2,m_atk+2)
            if dmg_m<0:dmg_m=0
            p_hp-=dmg_m
        st["m_hp"]=m_hp
        st["p_hp"]=p_hp
        if p_hp<=0:
            st["phase"]="end"
            await handle_rpg_death(user.id)
            await q.edit_message_text("패배! HP회복+60초 쿨다운.")
            return
        elif m_hp<=0:
            st["phase"]="end"
            await rpg_fight_victory(user.id,monster,q,reward_exp,reward_gold)
            return
        else:
            kb=[[InlineKeyboardButton("공격",callback_data=f"rfd_{user.id}_atk"),
                 InlineKeyboardButton("도망",callback_data=f"rfd_{user.id}_run")]]
            await q.edit_message_text(f"{monster}HP:{m_hp},내HP:{p_hp}\n(내공격:{dmg_p},몬공:{dmg_m})\n행동선택:",reply_markup=InlineKeyboardMarkup(kb))
    else:
        await q.answer("알수없는action",show_alert=True)

async def handle_rpg_death(uid:int):
    # 패배 시 => HP=풀회복 + 60초 쿨
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT max_hp FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if row:
        c.execute("UPDATE rpg_characters SET hp=%s WHERE user_id=%s",(row["max_hp"],uid))
    conn.commit()
    c.close();conn.close()
    rpg_cooldown[uid]=datetime.datetime.now().timestamp()+60

async def rpg_fight_victory(uid:int, monster:str, query, exp_gain:int, gold_gain:int):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT level,exp,gold,hp,max_hp,atk,skill_points FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        c.close();conn.close()
        await query.edit_message_text(f"{monster} 처치!\n(캐릭터없어 보상X)\n전투끝!")
        return
    lv=row["level"]
    xp=row["exp"]
    gold=row["gold"]
    sp=row["skill_points"]
    hp=row["hp"]
    mhp=row["max_hp"]
    atk=row["atk"]
    xp+=(exp_gain)
    gold+=(gold_gain)
    # 레벨업 공식: while xp >= (lv*lv*50):
    lvup_count=0
    while xp>=(lv*lv*50):
        xp-=(lv*lv*50)
        lv+=1
        sp+=1
        mhp+=20
        hp=mhp
        atk+=5
        lvup_count+=1
    # 승리 후 HP 전부회복
    hp=mhp
    c.execute("""
    UPDATE rpg_characters
    SET exp=%s,gold=%s,level=%s,skill_points=%s,hp=%s,max_hp=%s,atk=%s
    WHERE user_id=%s
    """,(xp,gold,lv,sp,hp,mhp,atk,uid))
    conn.commit()
    c.close();conn.close()

    lu_txt=""
    if lvup_count>0:
        lu_txt=f"\n레벨 {lvup_count}번 상승!"
    txt=(f"{monster} 처치!\n"
         f"획득: EXP+{exp_gain}, GOLD+{gold_gain}{lu_txt}\n"
         f"HP 전부 회복!\n"
         "전투끝!")
    await query.edit_message_text(txt)

async def rpg_shop_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_items ORDER BY price ASC")
    items=c.fetchall()
    c.close();conn.close()
    if not items:
        await update.message.reply_text("상점 아이템 없음.")
        return
    text="[상점 목록]\n"
    kb=[]
    for it in items:
        text+=(f"{it['item_id']}.{it['name']} (가격:{it['price']},ATK+{it['atk_bonus']},HP+{it['hp_bonus']},직업:{it['required_job']})\n")
        kb.append([InlineKeyboardButton(f"{it['name']} 구매",callback_data=f"rpg_shop_buy_{it['item_id']}")])
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(kb))

async def rpg_shop_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    if not data.startswith("rpg_shop_buy_"):
        await q.edit_message_text("상점콜백오류.")
        return
    iid=data.split("_",3)[3]
    try:
        item_id=int(iid)
    except:
        await q.edit_message_text("아이템ID오류.")
        return
    uid=q.from_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT gold,job FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await q.edit_message_text("캐릭터없음.")
        c.close();conn.close()
        return
    p_gold=row["gold"]
    p_job=row["job"]
    c.execute("SELECT * FROM rpg_items WHERE item_id=%s",(item_id,))
    irow=c.fetchone()
    if not irow:
        await q.edit_message_text("아이템없음.")
        c.close();conn.close()
        return
    # 직업제한
    reqjob=irow["required_job"]
    if reqjob and reqjob!=p_job:
        await q.edit_message_text(f"이 아이템은 {reqjob} 전용.")
        c.close();conn.close()
        return
    price=irow["price"]
    if p_gold<price:
        await q.edit_message_text("골드부족.")
        c.close();conn.close()
        return
    new_gold=p_gold-price
    c.execute("UPDATE rpg_characters SET gold=%s WHERE user_id=%s",(new_gold,uid))
    c.execute("""
    INSERT INTO rpg_inventory(user_id,item_id,quantity)
    VALUES(%s,%s,1)
    ON CONFLICT(user_id,item_id)
    DO UPDATE SET quantity=rpg_inventory.quantity+1
    """,(uid,item_id))
    conn.commit()
    c.close();conn.close()
    await q.edit_message_text(f"{irow['name']} 구매 완료! -{price} gold")

async def rpg_inventory_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT gold FROM rpg_characters WHERE user_id=%s",(uid,))
    crow=c.fetchone()
    if not crow:
        await update.message.reply_text("캐릭터없음.")
        c.close();conn.close()
        return
    p_gold=crow["gold"]
    txt=f"[인벤토리]\nGold:{p_gold}\n"
    c.execute("""
    SELECT inv.item_id,inv.quantity,it.name,it.atk_bonus,it.hp_bonus,it.required_job
    FROM rpg_inventory inv
    JOIN rpg_items it ON it.item_id=inv.item_id
    WHERE inv.user_id=%s
    """,(uid,))
    inv=c.fetchall()
    c.close();conn.close()
    if not inv:
        txt+="(아이템없음)"
    else:
        for i in inv:
            req=(f"(직업:{i['required_job']})" if i["required_job"] else "")
            txt+=(f"{i['name']} x{i['quantity']} (ATK+{i['atk_bonus']},HP+{i['hp_bonus']}) {req}\n")
    await update.message.reply_text(txt)

async def rpg_skill_list_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT job,level,skill_points FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await update.message.reply_text("캐릭터없음.")
        c.close();conn.close()
        return
    job=row["job"]
    lv=row["level"]
    sp=row["skill_points"]
    c.execute("SELECT * FROM rpg_skills WHERE job=%s ORDER BY required_level ASC",(job,))
    skills=c.fetchall()
    c.close();conn.close()
    if not skills:
        await update.message.reply_text("이 직업 스킬정보 없음.")
        return
    text=f"[{job} 스킬]\n스킬포인트:{sp}\n"
    for s in skills:
        text+=(f"ID:{s['skill_id']} {s['name']} (LvReq:{s['required_level']}, dmg:{s['damage']}, heal:{s['heal']})\n")
    await update.message.reply_text(text)

async def rpg_skill_learn_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    args=context.args
    if not args:
        await update.message.reply_text("사용:/스킬습득 <스킬ID>")
        return
    try:
        sid=int(args[0])
    except:
        await update.message.reply_text("스킬ID오류.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT job,level,skill_points FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await update.message.reply_text("캐릭터없음.")
        c.close();conn.close()
        return
    job=row["job"]
    lv=row["level"]
    sp=row["skill_points"]
    c.execute("SELECT * FROM rpg_skills WHERE skill_id=%s AND job=%s",(sid,job))
    sk=c.fetchone()
    if not sk:
        await update.message.reply_text("없는스킬 or 직업불일치.")
        c.close();conn.close()
        return
    if lv<sk["required_level"]:
        await update.message.reply_text("레벨부족.")
        c.close();conn.close()
        return
    if sp<1:
        await update.message.reply_text("스킬포인트부족.")
        c.close();conn.close()
        return
    c.execute("SELECT * FROM rpg_learned_skills WHERE user_id=%s AND skill_id=%s",(uid,sid))
    already=c.fetchone()
    if already:
        await update.message.reply_text("이미 배움.")
        c.close();conn.close()
        return
    c.execute("INSERT INTO rpg_learned_skills(user_id,skill_id) VALUES(%s,%s)",(uid,sid))
    c.execute("UPDATE rpg_characters SET skill_points=skill_points-1 WHERE user_id=%s",(uid,))
    conn.commit()
    c.close();conn.close()
    await update.message.reply_text("스킬습득 완료!")

async def rpg_myinfo_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group","supergroup"):
        if not is_bot_enabled_in_group(update.effective_chat.id):
            return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await update.message.reply_text("캐릭터 없음.")
        c.close();conn.close()
        return
    job=row["job"]
    lv=row["level"]
    xp=row["exp"]
    hp=row["hp"]
    mhp=row["max_hp"]
    atk=row["atk"]
    gold=row["gold"]
    sp=row["skill_points"]
    msg=(f"[내정보]\n직업:{job}\nLv:{lv}, EXP:{xp}/??\nHP:{hp}/{mhp}, ATK:{atk}\nGold:{gold},스킬포인트:{sp}\n")
    c.execute("""
    SELECT inv.item_id,inv.quantity,it.name,it.atk_bonus,it.hp_bonus,it.required_job
    FROM rpg_inventory inv
    JOIN rpg_items it ON it.item_id=inv.item_id
    WHERE inv.user_id=%s
    """,(uid,))
    inv=c.fetchall()
    c.close();conn.close()
    if not inv:
        msg+="(인벤토리 없음)"
    else:
        msg+="\n[인벤토리]\n"
        for i in inv:
            req=(f"(직업:{i['required_job']})" if i["required_job"] else "")
            msg+=(f"- {i['name']} x{i['quantity']} (ATK+{i['atk_bonus']},HP+{i['hp_bonus']}) {req}\n")
    await update.message.reply_text(msg)

###############################################################################
# 10. 인라인 메뉴(호출자만)
###############################################################################
async def menu_callback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    parts=data.split("|",1)
    if len(parts)<2:
        await q.answer("콜백오류", show_alert=True)
        return
    owner_id_str=parts[0]
    cmd=parts[1]
    caller_id=str(q.from_user.id)
    if caller_id!=owner_id_str:
        await q.answer("이건 당신 메뉴가 아님!", show_alert=True)
        return
    await q.answer()

    if cmd=="menu_games":
        kb=[
          [InlineKeyboardButton("마피아",callback_data=f"{owner_id_str}|menu_mafia")],
          [InlineKeyboardButton("RPG",callback_data=f"{owner_id_str}|menu_rpg")],
          [InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]
        ]
        await q.edit_message_text("게임 메뉴",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_mafia":
        txt=(
            "[마피아]\n"
            "/마피아시작 /마피아목록\n"
            "/참가 <세션ID>\n"
            "/방나가기 <세션ID>\n"
            "/마피아강제시작 <세션ID>\n"
            "(마피아DM) /살해 <세션ID> <유저ID>\n"
            "(의사DM) /치료 <세션ID> <유저ID>\n"
            "(경찰DM) /조사 <세션ID> <유저ID>\n"
            "(그룹)/투표 <세션ID> <유저ID>"
        )
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_games")]]
        await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_rpg":
        txt=(
            "[RPG]\n"
            "/rpg생성 /rpg직업선택 /rpg상태\n"
            "/던전 /상점 /인벤토리\n"
            "/스킬목록 /스킬습득 <ID> /내정보"
        )
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_games")]]
        await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_group":
        kb=[
          [InlineKeyboardButton("공지(관리자)",callback_data=f"{owner_id_str}|menu_group_announce")],
          [InlineKeyboardButton("투표/설문",callback_data=f"{owner_id_str}|menu_group_vote")],
          [InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]
        ]
        await q.edit_message_text("그룹관리 메뉴",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_group_announce":
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_group")]]
        await q.edit_message_text("공지:/announce <메시지>",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_group_vote":
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_group")]]
        await q.edit_message_text("투표:/vote <주제>",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_subscribe":
        s=is_subscribed_db(int(caller_id))
        stat="구독자 ✅" if s else "비구독 ❌"
        toggle="구독해지" if s else "구독하기"
        kb=[
          [InlineKeyboardButton(toggle,callback_data=f"{owner_id_str}|menu_sub_toggle")],
          [InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]
        ]
        await q.edit_message_text(f"현재상태:{stat}", reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_sub_toggle":
        s=is_subscribed_db(int(caller_id))
        set_subscribe(int(caller_id), not s)
        nowtxt="구독자 ✅" if not s else "비구독 ❌"
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_subscribe")]]
        await q.edit_message_text(f"이제 {nowtxt} 되었습니다.",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_ranking":
        if not is_bot_enabled_in_group(int(q.message.chat_id)):
            await q.edit_message_text("봇미활성.")
            return
        txt=get_daily_ranking_text(q.message.chat_id)
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]]
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_back_main":
        kb=[
          [InlineKeyboardButton("🎮 게임",callback_data=f"{owner_id_str}|menu_games"),
           InlineKeyboardButton("🔧 그룹관리",callback_data=f"{owner_id_str}|menu_group")],
          [InlineKeyboardButton("💳 구독",callback_data=f"{owner_id_str}|menu_subscribe"),
           InlineKeyboardButton("📊 채팅랭킹",callback_data=f"{owner_id_str}|menu_ranking")]
        ]
        await q.edit_message_text("메인메뉴로 복귀",reply_markup=InlineKeyboardMarkup(kb))
    else:
        await q.edit_message_text("알수없는메뉴.")

###############################################################################
# 11. 일반 텍스트
###############################################################################
async def text_message_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat=update.effective_chat
    if chat.type in ("group","supergroup"):
        if is_bot_enabled_in_group(chat.id):
            await filter_bad_words_and_spam_and_links(update, context)
            increment_daily_chat_count(update.effective_user.id, chat.id)
    else:
        pass # 개인채팅일 땐 필터X

###############################################################################
# 12. 스케줄러
###############################################################################
def schedule_jobs():
    sch=BackgroundScheduler(timezone=str(KST))
    sch.add_job(reset_daily_chat_count,'cron',hour=0,minute=0)
    sch.start()

###############################################################################
# 13. seed_rpg_data: 아이템/스킬 등록
###############################################################################
def seed_rpg_data():
    conn=get_db_conn()
    c=conn.cursor()
    # 스킬 (전사/마법사/도적)
    c.execute("SELECT * FROM rpg_skills WHERE name='강타' AND job='전사'")
    if not c.fetchone():
        c.execute("INSERT INTO rpg_skills(name,job,required_level,damage,heal,mana_cost) VALUES('강타','전사',1,10,0,0)")
    c.execute("SELECT * FROM rpg_skills WHERE name='분노의칼날' AND job='전사'")
    if not c.fetchone():
        c.execute("INSERT INTO rpg_skills(name,job,required_level,damage,heal,mana_cost) VALUES('분노의칼날','전사',5,20,0,0)")

    c.execute("SELECT * FROM rpg_skills WHERE name='파이어볼' AND job='마법사'")
    if not c.fetchone():
        c.execute("INSERT INTO rpg_skills(name,job,required_level,damage,heal,mana_cost) VALUES('파이어볼','마법사',1,15,0,0)")
    c.execute("SELECT * FROM rpg_skills WHERE name='힐' AND job='마법사'")
    if not c.fetchone():
        c.execute("INSERT INTO rpg_skills(name,job,required_level,damage,heal,mana_cost) VALUES('힐','마법사',5,0,15,0)")

    c.execute("SELECT * FROM rpg_skills WHERE name='백스탭' AND job='도적'")
    if not c.fetchone():
        c.execute("INSERT INTO rpg_skills(name,job,required_level,damage,heal,mana_cost) VALUES('백스탭','도적',1,12,0,0)")
    c.execute("SELECT * FROM rpg_skills WHERE name='독칼' AND job='도적'")
    if not c.fetchone():
        c.execute("INSERT INTO rpg_skills(name,job,required_level,damage,heal,mana_cost) VALUES('독칼','도적',5,18,0,0)")

    # 아이템
    c.execute("SELECT * FROM rpg_items WHERE name='튼튼한목검'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('튼튼한목검',100,5,0,'전사')
        """)
    c.execute("SELECT * FROM rpg_items WHERE name='강철검'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('강철검',300,12,0,'전사')
        """)
    c.execute("SELECT * FROM rpg_items WHERE name='가죽갑옷'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('가죽갑옷',150,0,15,'')
        """)
    c.execute("SELECT * FROM rpg_items WHERE name='주문서'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('주문서',150,10,0,'마법사')
        """)
    c.execute("SELECT * FROM rpg_items WHERE name='마법지팡이'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('마법지팡이',350,20,0,'마법사')
        """)
    c.execute("SELECT * FROM rpg_items WHERE name='단검'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('단검',120,7,0,'도적')
        """)
    c.execute("SELECT * FROM rpg_items WHERE name='독단검'")
    if not c.fetchone():
        c.execute("""
        INSERT INTO rpg_items(name,price,atk_bonus,hp_bonus,required_job)
        VALUES('독단검',300,15,0,'도적')
        """)
    conn.commit()
    c.close()
    conn.close()

###############################################################################
# 14. main
###############################################################################
def main():
    init_db()
    seed_rpg_data()
    schedule_jobs()

    app=ApplicationBuilder().token(BOT_TOKEN).build()

    # 영문 명령
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("adminsecret", admin_secret_command))
    app.add_handler(CommandHandler("adminon", admin_on_command))
    app.add_handler(CommandHandler("adminoff", admin_off_command))
    app.add_handler(CommandHandler("announce", announce_command))
    app.add_handler(CommandHandler("subscribe_toggle", subscribe_toggle_command))
    app.add_handler(CommandHandler("vote", vote_command))
    app.add_handler(MessageHandler(
    filters.Regex(r"^/온(\s.*)?$"),  # /온 으로 시작
    bot_on_command
))

    import re
    # 한글
    app.add_handler(MessageHandler(filters.Regex(r"^/시작(\s.*)?$"), hangeul_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/도움말(\s.*)?$"), hangeul_help_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/랭킹(\s.*)?$"), hangeul_ranking_command))

    # 마피아
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아시작(\s.*)?$"), mafia_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아목록(\s.*)?$"), mafia_list_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/참가(\s.*)?$"), mafia_join_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/방나가기(\s.*)?$"), mafia_leave_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아강제시작(\s.*)?$"), mafia_force_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/살해(\s.*)?$"), mafia_kill_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/치료(\s.*)?$"), mafia_doctor_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/조사(\s.*)?$"), mafia_police_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/투표(\s.*)?$"), mafia_vote_command))
    app.add_handler(MessageHandler(
    filters.Regex(r"^/방삭제(\s.*)?$"),
    mafia_delete_room
))

    # RPG
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg생성(\s.*)?$"), rpg_create_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg직업선택(\s.*)?$"), rpg_set_job_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg상태(\s.*)?$"), rpg_status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/던전(\s.*)?$"), rpg_dungeon_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/상점(\s.*)?$"), rpg_shop_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/인벤토리(\s.*)?$"), rpg_inventory_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/스킬목록(\s.*)?$"), rpg_skill_list_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/스킬습득(\s.*)?$"), rpg_skill_learn_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/내정보(\s.*)?$"), rpg_myinfo_command))

    # 콜백
    app.add_handler(CallbackQueryHandler(vote_callback_handler, pattern="^vote_(yes|no)\\|"))
    app.add_handler(CallbackQueryHandler(rpg_dungeon_callback, pattern="^rdsel_.*"))
    app.add_handler(CallbackQueryHandler(rpg_fight_action_callback, pattern="^rfd_.*"))
    app.add_handler(CallbackQueryHandler(rpg_job_callback_handler, pattern="^rpg_job_.*"))
    app.add_handler(CallbackQueryHandler(rpg_shop_callback, pattern="^rpg_shop_buy_.*"))
    app.add_handler(CallbackQueryHandler(mafia_list_join_callback, pattern="^mafia_join_.*"))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^.*\\|menu_.*"))

    # 환영/퇴장
    app.add_handler(ChatMemberHandler(welcome_message, ChatMemberHandler.CHAT_MEMBER))

    # 일반 텍스트
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    logger.info("봇 시작!")
    app.run_polling()

if __name__=="__main__":
    main()