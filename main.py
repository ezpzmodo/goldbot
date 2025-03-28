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

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

KST = pytz.timezone("Asia/Seoul")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

###############################################################################
# 1. DB 연결 & 테이블
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

    # 마피아
    c.execute("""
    CREATE TABLE IF NOT EXISTS mafia_sessions (
      session_id TEXT PRIMARY KEY,
      status TEXT,          -- waiting/night/day/ended
      group_id BIGINT,
      created_at TIMESTAMP DEFAULT NOW(),
      day_duration INT DEFAULT 60,
      night_duration INT DEFAULT 30
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS mafia_players (
      session_id TEXT,
      user_id BIGINT,
      role TEXT,  -- Mafia/Police/Doctor/Citizen/dead
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
      hp_bonus INT DEFAULT 0
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

    # 파티(미사용)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_parties (
      party_id SERIAL PRIMARY KEY,
      leader_id BIGINT,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rpg_party_members (
      party_id INT,
      user_id BIGINT,
      PRIMARY KEY(party_id, user_id)
    );
    """)

    # 일일채팅
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_chat_count (
      user_id BIGINT,
      date_str TEXT,
      count INT DEFAULT 0,
      PRIMARY KEY(user_id, date_str)
    );
    """)

    conn.commit()
    c.close()
    conn.close()

###############################################################################
# 2. 유저/관리/구독
###############################################################################
def ensure_user_in_db(uid:int, fname:str, lname:str, t_username:str):
    """ first+last가 없으면 @username, 그것도 없으면 '이름없음' """
    ff=(fname or "").strip()
    ll=(lname or "").strip()
    combined=ff
    if ll:
        combined+=" "+ll
    if not combined.strip():
        if t_username:
            combined=f"@{t_username}"
        else:
            combined="이름없음"
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        c.execute("INSERT INTO users(user_id,username) VALUES(%s,%s)",(uid,combined.strip()))
    else:
        if row["username"]!=combined.strip():
            c.execute("UPDATE users SET username=%s WHERE user_id=%s",(combined.strip(),uid))
    conn.commit()
    c.close();conn.close()

def is_admin_db(uid:int):
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
    c.close();conn.close()

def is_subscribed_db(uid:int):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT is_subscribed FROM users WHERE user_id=%s",(uid,))
    row=c.fetchone()
    c.close();conn.close()
    return (row and row["is_subscribed"])

def set_subscribe(uid:int,val:bool):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("UPDATE users SET is_subscribed=%s WHERE user_id=%s",(val,uid))
    conn.commit()
    c.close();conn.close()

###############################################################################
# 3. 그룹관리(불량단어,링크,스팸), 환영/퇴장
###############################################################################
BAD_WORDS=["나쁜말1","나쁜말2"]
user_message_times={}

async def welcome_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cmu:ChatMemberUpdated=update.chat_member
    if cmu.new_chat_member.status=="member":
        user=cmu.new_chat_member.user
        await context.bot.send_message(
            cmu.chat.id, f"환영합니다, {user.mention_html()}!",
            parse_mode="HTML"
        )
    elif cmu.new_chat_member.status in ("left","kicked"):
        user=cmu.new_chat_member.user
        await context.bot.send_message(
            cmu.chat.id, f"{user.full_name}님이 나갔습니다."
        )

async def filter_bad_words_and_spam_and_links(update:Update, context:ContextTypes.DEFAULT_TYPE):
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
# 4. 채팅랭킹(매일 0시 리셋)
###############################################################################
def increment_daily_chat_count(uid:int):
    now=datetime.datetime.now(tz=KST)
    ds=now.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    INSERT INTO daily_chat_count(user_id,date_str,count)
    VALUES(%s,%s,1)
    ON CONFLICT(user_id,date_str)
    DO UPDATE SET count=daily_chat_count.count+1
    """,(uid,ds))
    conn.commit()
    c.close();conn.close()

def reset_daily_chat_count():
    now=datetime.datetime.now(tz=KST)
    y= now - datetime.timedelta(days=1)
    ys=y.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("DELETE FROM daily_chat_count WHERE date_str=%s",(ys,))
    conn.commit()
    c.close();conn.close()

def get_daily_ranking_text():
    now=datetime.datetime.now(tz=KST)
    ds=now.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT dc.user_id,dc.count,u.username
    FROM daily_chat_count dc
    LEFT JOIN users u ON u.user_id=dc.user_id
    WHERE dc.date_str=%s
    ORDER BY dc.count DESC
    LIMIT 10
    """,(ds,))
    rows=c.fetchall()
    c.close();conn.close()
    if not rows:
        return f"오늘({ds}) 채팅 기록이 없습니다."
    msg=f"=== 오늘({ds}) 채팅랭킹 ===\n"
    rank=1
    for r in rows:
        uname=r["username"] or "이름없음"
        cnt=r["count"]
        if rank==1: prefix="🥇"
        elif rank==2: prefix="🥈"
        elif rank==3: prefix="🥉"
        else: prefix=f"{rank}위:"
        msg+=f"{prefix} {uname} ({cnt}회)\n"
        rank+=1
    return msg

###############################################################################
# 5. 영문 명령어
###############################################################################
async def start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    uid=user.id
    ensure_user_in_db(uid, user.first_name or "", user.last_name or "", user.username or "")
    owner_id=str(uid)
    text=(
      "다기능 봇.\n"
      "인라인 버튼은 이 대화 호출자만 클릭 가능."
    )
    kb=[
      [
        InlineKeyboardButton("🎮 게임", callback_data=f"{owner_id}|menu_games"),
        InlineKeyboardButton("🔧 그룹관리", callback_data=f"{owner_id}|menu_group")
      ],
      [
        InlineKeyboardButton("💳 구독", callback_data=f"{owner_id}|menu_subscribe"),
        InlineKeyboardButton("📊 채팅랭킹", callback_data=f"{owner_id}|menu_ranking")
      ]
    ]
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    msg=(
        "[도움말]\n"
        "/start\n"
        "/help\n"
        "/adminsecret <키>\n"
        "/announce <메시지> (관리자)\n"
        "/subscribe_toggle\n"
        "/vote <주제>\n\n"
        "한글:\n"
        "/시작 /도움말 /랭킹\n"
        "/마피아시작 /마피아목록 /참가 /마피아강제시작 /방나가기 /살해 /치료 /조사 /투표\n"
        "/rpg생성 /rpg직업선택 /rpg상태 /던전 /상점 /인벤토리 /스킬목록 /스킬습득 /내정보"
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

async def announce_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if not is_admin_db(uid):
        await update.message.reply_text("관리자 전용.")
        return
    msg=" ".join(context.args)
    if not msg:
        await update.message.reply_text("공지할 내용?")
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
        await update.message.reply_text("사용법:/vote <주제>")
        return
    kb=[[InlineKeyboardButton("👍",callback_data=f"vote_yes|{topic}"),
         InlineKeyboardButton("👎",callback_data=f"vote_no|{topic}")]]
    await update.message.reply_text(f"[투표]\n{topic}",reply_markup=InlineKeyboardMarkup(kb))

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
# 6. 한글 명령어(Regex)
###############################################################################
import re

async def hangeul_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def hangeul_help_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)

async def hangeul_ranking_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt=get_daily_ranking_text()
    await update.message.reply_text(txt)

# 마피아(한글 래퍼)
async def hangeul_mafia_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_start_command(update, context)

async def hangeul_mafia_join_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_join_command(update, context)

async def hangeul_mafia_force_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_force_start_command(update, context)

async def hangeul_mafia_kill_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_kill_command(update, context)

async def hangeul_mafia_doctor_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_doctor_command(update, context)

async def hangeul_mafia_police_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_police_command(update, context)

async def hangeul_mafia_vote_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_vote_command(update, context)

async def hangeul_mafia_list_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_list_command(update, context)

# **추가**: /방나가기
async def hangeul_mafia_leave_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await mafia_leave_command(update, context)

###############################################################################
# 7. 마피아 로직
###############################################################################
MAFIA_DEFAULT_DAY_DURATION=60
MAFIA_DEFAULT_NIGHT_DURATION=30
mafia_tasks={}

def generate_mafia_session_id():
    num=random.randint(0,999999999999)
    return str(num).zfill(12)

async def mafia_list_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT session_id, created_at
    FROM mafia_sessions
    WHERE status='waiting'
    ORDER BY created_at DESC
    LIMIT 10
    """)
    rows=c.fetchall()
    c.close();conn.close()
    if not rows:
        await update.message.reply_text("대기중인 마피아 세션이 없습니다.")
        return
    txt="[대기중인 마피아 세션]\n"
    kb=[]
    for r in rows:
        sid=r["session_id"]
        txt+=f"- {sid}\n"
        kb.append([InlineKeyboardButton(f"{sid} 참가",callback_data=f"mafia_join_{sid}")])
    await update.message.reply_text(txt,reply_markup=InlineKeyboardMarkup(kb))

async def mafia_list_join_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    if not data.startswith("mafia_join_"):
        await q.edit_message_text("잘못된 세션 콜백.")
        return
    sid=data.split("_",2)[2]
    user=q.from_user
    uid=user.id
    ensure_user_in_db(uid,user.first_name or "", user.last_name or "", user.username or "")

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(sid,))
    sess=c.fetchone()
    if not sess or sess["status"]!="waiting":
        await q.edit_message_text("세션없거나 이미 시작됨.")
        c.close();conn.close()
        return
    # 이미 대기중인 세션에 참가?
    # 한 사람이 동시에 2개 waiting세션 참가 불가
    c.execute("""
    SELECT ms.session_id
    FROM mafia_players mp
    JOIN mafia_sessions ms ON ms.session_id=mp.session_id
    WHERE mp.user_id=%s AND ms.status='waiting'
    """,(uid,))
    already=c.fetchone()
    if already:
        await q.edit_message_text("이미 다른 대기 세션에 참가중이므로 불가.")
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
    await q.edit_message_text(f"세션 {sid} 참가 완료. 현재 {n}명 대기중.")

async def mafia_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        await update.message.reply_text("그룹에서만 사용 가능.")
        return
    group_id=update.effective_chat.id
    session_id=generate_mafia_session_id()

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    INSERT INTO mafia_sessions(session_id,status,group_id,day_duration,night_duration)
    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
    """,(session_id,"waiting",group_id,MAFIA_DEFAULT_DAY_DURATION,MAFIA_DEFAULT_NIGHT_DURATION))
    conn.commit()
    c.close();conn.close()

    await update.message.reply_text(
      f"마피아 세션 생성: {session_id}\n"
      f"/참가 {session_id}\n"
      f"/마피아강제시작 {session_id}\n"
      "또는 /마피아목록 으로 확인 가능"
    )

async def mafia_join_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("사용법:/참가 <세션ID>")
        return
    session_id=args[0]
    user=update.effective_user
    uid=user.id
    ensure_user_in_db(uid,user.first_name or "",user.last_name or "",user.username or "")

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess=c.fetchone()
    if not sess:
        await update.message.reply_text("해당 세션 없음.")
        c.close();conn.close()
        return
    if sess["status"]!="waiting":
        await update.message.reply_text("이미 시작된 세션.")
        c.close();conn.close()
        return
    # 한 사람이 이미 다른 waiting 세션에 있는지 체크
    c.execute("""
    SELECT ms.session_id
    FROM mafia_players mp
    JOIN mafia_sessions ms ON ms.session_id=mp.session_id
    WHERE mp.user_id=%s AND ms.status='waiting'
    """,(uid,))
    already=c.fetchone()
    if already:
        await update.message.reply_text("이미 다른 대기 세션에 참가중이므로 불가.")
        c.close();conn.close()
        return

    c.execute("SELECT * FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,uid))
    row=c.fetchone()
    if row:
        await update.message.reply_text("이미참가중.")
        c.close();conn.close()
        return
    c.execute("INSERT INTO mafia_players(session_id,user_id,role) VALUES(%s,%s,%s)",(session_id,uid,"none"))
    conn.commit()
    c.execute("SELECT COUNT(*) as c FROM mafia_players WHERE session_id=%s",(session_id,))
    n=c.fetchone()["c"]
    c.close();conn.close()
    await update.message.reply_text(f"참가완료. 현재 {n}명 대기중.")

async def mafia_leave_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    """
    /방나가기 <세션ID>
    - waiting 상태인 세션일 경우 -> DB에서 제거
    - 이미 시작이면 '나갈 수 없음'
    """
    args=context.args
    if not args:
        await update.message.reply_text("사용:/방나가기 <세션ID>")
        return
    session_id=args[0]
    uid=update.effective_user.id

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess=c.fetchone()
    if not sess:
        await update.message.reply_text("세션없음.")
        c.close();conn.close()
        return
    if sess["status"]!="waiting":
        await update.message.reply_text("이미 시작된 세션에서 탈퇴 불가.")
        c.close();conn.close()
        return
    # 대기중인 세션
    c.execute("DELETE FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,uid))
    rowcount=c.rowcount
    conn.commit()
    c.close();conn.close()
    if rowcount>0:
        await update.message.reply_text(f"{session_id} 세션에서 나갔습니다.")
    else:
        await update.message.reply_text("해당 세션에 참가중이 아님.")

async def mafia_force_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args or len(args)<1:
        await update.message.reply_text("사용법:/마피아강제시작 <세션ID>")
        return
    session_id=args[0]
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess=c.fetchone()
    if not sess or sess["status"]!="waiting":
        await update.message.reply_text("세션이 없거나 이미 시작됨.")
        c.close();conn.close()
        return
    c.execute("SELECT user_id FROM mafia_players WHERE session_id=%s",(session_id,))
    rows=c.fetchall()
    players=[r["user_id"] for r in rows]
    if len(players)<5:
        await update.message.reply_text("최소5명 필요.")
        c.close();conn.close()
        return
    random.shuffle(players)
    mafia_id=players[0]
    police_id=players[1]
    doctor_id=players[2]
    for i,pid in enumerate(players):
        if pid==mafia_id: role="Mafia"
        elif pid==police_id: role="Police"
        elif pid==doctor_id: role="Doctor"
        else: role="Citizen"
        c.execute("""
        UPDATE mafia_players
        SET role=%s,is_alive=TRUE,vote_target=0,heal_target=0,investigate_target=0
        WHERE session_id=%s AND user_id=%s
        """,(role,session_id,pid))
    c.execute("UPDATE mafia_sessions SET status='night' WHERE session_id=%s",(session_id,))
    conn.commit()
    group_id=sess["group_id"]
    day_dur=sess["day_duration"]
    night_dur=sess["night_duration"]
    c.close();conn.close()

    await update.message.reply_text(f"마피아 게임 시작! (세션:{session_id}) 첫번째 밤.")

    # 역할 안내 DM
    for pid in players:
        conn2=get_db_conn()
        c2=conn2.cursor()
        c2.execute("SELECT role FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,pid))
        rr=c2.fetchone()
        c2.close();conn2.close()
        if not rr: continue
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
            await context.bot.send_message(pid, txt)
        except:
            pass

    # 타이머
    if session_id in mafia_tasks:
        mafia_tasks[session_id].cancel()
    mafia_tasks[session_id]=asyncio.create_task(
        mafia_cycle(session_id, group_id, day_dur, night_dur, context)
    )

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
            await context.bot.send_message(group_id, f"밤이 끝났습니다. 낮({day_dur}초) 시작!\n/투표 <세션ID> <유저ID>")
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
            await context.bot.send_message(group_id, f"낮이 끝났습니다. 밤({night_dur}초) 시작!")
        except:
            pass

        if check_mafia_win_condition(session_id):
            break

def check_mafia_win_condition(session_id:str):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s",(session_id,))
    rows=c.fetchall()
    c.close();conn.close()
    alive_mafia=0
    alive_citizen=0
    for r in rows:
        if not r["is_alive"]: continue
        if r["role"]=="Mafia": alive_mafia+=1
        else: alive_citizen+=1
    return (alive_mafia==0 or alive_citizen==0)

async def resolve_night_actions(session_id, group_id, context):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    SELECT user_id,role,is_alive,vote_target,heal_target,investigate_target
    FROM mafia_players
    WHERE session_id=%s
    """,(session_id,))
    rows=c.fetchall()
    mafia_kill_target=None
    doc_heal={}
    pol_invest={}
    for r in rows:
        if r["role"]=="Mafia" and r["is_alive"]:
            if r["vote_target"]!=0:
                mafia_kill_target=r["vote_target"]
        elif r["role"]=="Doctor" and r["is_alive"]:
            if r["heal_target"]!=0:
                doc_heal[r["user_id"]]=r["heal_target"]
        elif r["role"]=="Police" and r["is_alive"]:
            if r["investigate_target"]!=0:
                pol_invest[r["user_id"]]=r["investigate_target"]
    final_dead=None
    if mafia_kill_target:
        healed=any(doc_heal[k]==mafia_kill_target for k in doc_heal)
        if not healed:
            c.execute("""
            UPDATE mafia_players
            SET is_alive=FALSE, role='dead'
            WHERE session_id=%s AND user_id=%s
            """,(session_id, mafia_kill_target))
            final_dead=mafia_kill_target
    for pol_id, suspect_id in pol_invest.items():
        c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,suspect_id))
        sr=c.fetchone()
        if sr:
            try:
                await context.bot.send_message(pol_id, f"[조사결과]{suspect_id}:{sr['role']}")
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
            await context.bot.send_message(group_id, f"밤 사이에 {final_dead}님이 사망.")
        except:
            pass

async def resolve_day_vote(session_id, group_id, context):
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
    SET is_alive=FALSE, role='dead'
    WHERE session_id=%s AND user_id=%s
    """,(session_id,top_user))
    conn.commit()
    c.close();conn.close()
    try:
        await context.bot.send_message(group_id, f"{top_user}님이 {top_cnt}표로 처형되었습니다.")
    except:
        pass
    if check_mafia_win_condition(session_id):
        await context.bot.send_message(group_id,"게임종료!")
        return True
    return False

async def mafia_kill_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인채팅에서만.")
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/살해 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("유효ID아님.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Mafia" or not row["is_alive"]:
        await update.message.reply_text("마피아아님or사망.")
        c.close();conn.close()
        return
    c.execute("UPDATE mafia_players SET vote_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님을 살해 대상으로 설정.")

async def mafia_doctor_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인채팅에서만.")
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/치료 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("유효한ID아님.")
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
        await update.message.reply_text("개인채팅에서만.")
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/조사 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("유효한ID아님.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Police" or not row["is_alive"]:
        await update.message.reply_text("경찰 아님 or 사망.")
        c.close();conn.close()
        return
    c.execute("UPDATE mafia_players SET investigate_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님 조사 대상으로 설정.")

async def mafia_vote_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용:/투표 <세션ID> <유저ID>")
        return
    sess_id,tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("유효한ID아님.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT status FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sr=c.fetchone()
    if not sr or sr["status"]!="day":
        await update.message.reply_text("낮이 아님.")
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
    c.close()
    conn.close()
    await update.message.reply_text(f"{tgt}님에게 투표.")

###############################################################################
# 8. RPG (던전 전투+아이템 atk/hp 보너스 적용, 죽으면 HP회복+쿨다운)
###############################################################################
rpg_fight_state={}
rpg_cooldown={}  # uid -> timestamp (끝나는 시각)

async def rpg_create_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    fn=update.effective_user.first_name or ""
    ln=update.effective_user.last_name or ""
    un=update.effective_user.username or ""
    ensure_user_in_db(uid, fn, ln, un)
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if row:
        await update.message.reply_text("이미 캐릭터가 있음.")
        c.close();conn.close()
        return
    c.execute("""
    INSERT INTO rpg_characters(user_id,job,level,exp,hp,max_hp,atk,gold,skill_points)
    VALUES(%s,%s,1,0,100,100,10,100,0)
    """,(uid,"none"))
    conn.commit()
    c.close();conn.close()
    await update.message.reply_text("캐릭터 생성완료! /rpg직업선택 으로 직업을 고르세요.")

async def rpg_set_job_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    kb=[
      [InlineKeyboardButton("전사", callback_data="rpg_job_warrior")],
      [InlineKeyboardButton("마법사", callback_data="rpg_job_mage")],
      [InlineKeyboardButton("도적", callback_data="rpg_job_thief")]
    ]
    await update.message.reply_text("직업을 선택하세요:", reply_markup=InlineKeyboardMarkup(kb))

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
        await q.edit_message_text("캐릭터없음. /rpg생성 먼저.")
        c.close();conn.close()
        return
    if row["job"]!="none":
        await q.edit_message_text("이미 직업선택됨.")
        c.close();conn.close()
        return
    if job=="warrior":
        hp=120;atk=12
    elif job=="mage":
        hp=80;atk=15
    else:
        hp=100;atk=10
    c.execute("""
    UPDATE rpg_characters
    SET job=%s,hp=%s,max_hp=%s,atk=%s
    WHERE user_id=%s
    """,(job,hp,hp,atk,uid))
    conn.commit()
    c.close();conn.close()
    await q.edit_message_text(f"{job} 직업선택 완료!")

async def rpg_status_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    c.close();conn.close()
    if not row:
        await update.message.reply_text("캐릭터없음./rpg생성")
        return
    job=row["job"]
    lv=row["level"]
    xp=row["exp"]
    hp=row["hp"]
    mhp=row["max_hp"]
    atk=row["atk"]
    gold=row["gold"]
    sp=row["skill_points"]
    msg=(f"[캐릭터]\n"
         f"직업:{job}\n"
         f"레벨:{lv}, EXP:{xp}/{lv*100}\n"
         f"HP:{hp}/{mhp}, ATK:{atk}\n"
         f"Gold:{gold}, 스킬포인트:{sp}")
    await update.message.reply_text(msg)

async def rpg_dungeon_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    now_ts=datetime.datetime.now().timestamp()
    # 쿨다운?
    if uid in rpg_cooldown:
        if now_ts<rpg_cooldown[uid]:
            remain=int(rpg_cooldown[uid]-now_ts)
            await update.message.reply_text(f"던전 쿨다운 {remain}초 남음.")
            return
    kb=[
      [InlineKeyboardButton("공격(쉬움)", callback_data="rdsel_easy")],
      [InlineKeyboardButton("공격(보통)", callback_data="rdsel_normal")],
      [InlineKeyboardButton("공격(어려움)", callback_data="rdsel_hard")]
    ]
    await update.message.reply_text("던전 난이도 선택:", reply_markup=InlineKeyboardMarkup(kb))

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

    diff=data.split("_",1)[1]  # easy/normal/hard
    if diff=="easy":
        monster="슬라임"
        mhp=40;matk=5
    elif diff=="normal":
        monster="오크"
        mhp=80;matk=10
    else:
        monster="드래곤"
        mhp=150;matk=20

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    char=c.fetchone()
    if not char:
        await q.edit_message_text("캐릭터없음.")
        c.close();conn.close()
        return

    # 아이템 보너스 계산
    base_hp=char["hp"]
    base_atk=char["atk"]
    # 아이템 합
    c.execute("""
    SELECT inv.quantity,it.atk_bonus,it.hp_bonus
    FROM rpg_inventory inv
    JOIN rpg_items it ON it.item_id=inv.item_id
    WHERE inv.user_id=%s
    """,(uid,))
    invrows=c.fetchall()
    sum_atk=0
    sum_hp=0
    for i in invrows:
        sum_atk+=(i["atk_bonus"]*i["quantity"])
        sum_hp+=(i["hp_bonus"]*i["quantity"])

    p_hp=base_hp+sum_hp  # 전투 시작 체력
    p_atk=base_atk+sum_atk

    # 스킬 목록
    c.execute("""
    SELECT s.skill_id,s.name,s.damage,s.heal,s.mana_cost
    FROM rpg_learned_skills ls
    JOIN rpg_skills s ON s.skill_id=ls.skill_id
    WHERE ls.user_id=%s
    """,(uid,))
    learned=c.fetchall()
    c.close();conn.close()

    rpg_fight_state[uid]={
      "monster":monster,
      "m_hp":mhp,
      "m_atk":matk,
      "p_hp":p_hp,
      "p_atk":p_atk,
      "phase":"ongoing",
      "skills":learned
    }

    kb=[
      [InlineKeyboardButton("공격",callback_data=f"rfd_{uid}_atk"),
       InlineKeyboardButton("스킬",callback_data=f"rfd_{uid}_skill"),
       InlineKeyboardButton("도망",callback_data=f"rfd_{uid}_run")]
    ]
    txt=(f"{monster} 출현!\n"
         f"몬스터HP:{mhp}, 내HP:{p_hp}\n"
         "행동선택:")
    await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def rpg_fight_action_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    parts=data.split("_")
    if len(parts)<3:return
    owner_str=parts[1]
    action=parts[2]
    uid=q.from_user.id
    if str(uid)!=owner_str:
        await q.answer("이 전투는 당신 전투 아님!", show_alert=True)
        return
    st=rpg_fight_state.get(uid)
    if not st or st["phase"]!="ongoing":
        await q.answer("전투없거나끝.")
        return

    monster=st["monster"]
    m_hp=st["m_hp"]
    m_atk=st["m_atk"]
    p_hp=st["p_hp"]
    p_atk=st["p_atk"]
    skills=st["skills"]

    if action=="run":
        st["phase"]="end"
        await q.edit_message_text("도망쳤습니다. 전투종료!")
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
            # 죽음 -> HP풀회복, 60초쿨
            await handle_rpg_death(uid)
            await q.edit_message_text(f"{monster}에게 패배! HP회복, 60초후 재도전가능")
            return
        elif m_hp<=0:
            st["phase"]="end"
            await rpg_fight_victory(uid,monster,q,dmg_p,dmg_m,m_hp,p_hp)
            return
        else:
            kb=[
              [InlineKeyboardButton("공격",callback_data=f"rfd_{uid}_atk"),
               InlineKeyboardButton("스킬",callback_data=f"rfd_{uid}_skill"),
               InlineKeyboardButton("도망",callback_data=f"rfd_{uid}_run")]
            ]
            txt=(f"{monster}HP:{m_hp}, 내HP:{p_hp}\n"
                 f"(내공격:{dmg_p},몬공:{dmg_m})\n"
                 "행동선택:")
            await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    elif action=="skill":
        if not skills:
            await q.edit_message_text("배운스킬이 없습니다.")
            return
        kb=[]
        for s in skills:
            sid=s["skill_id"]
            nm=s["name"]
            kb.append([InlineKeyboardButton(nm, callback_data=f"rfd_{uid}_useSkill_{sid}")])
        kb.append([InlineKeyboardButton("뒤로", callback_data=f"rfd_{uid}_back")])
        txt=f"{monster}HP:{m_hp}, 내HP:{p_hp}\n스킬선택:"
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif action=="back":
        kb=[
          [InlineKeyboardButton("공격",callback_data=f"rfd_{uid}_atk"),
           InlineKeyboardButton("스킬",callback_data=f"rfd_{uid}_skill"),
           InlineKeyboardButton("도망",callback_data=f"rfd_{uid}_run")]
        ]
        txt=(f"{monster}HP:{m_hp}, 내HP:{p_hp}\n행동선택:")
        await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    else:
        # useSkill sid
        if action.startswith("useSkill"):
            sid_str=action.split("_")[1]
            try:
                sid=int(sid_str)
            except:
                await q.answer("스킬ID오류",show_alert=True)
                return
            skill=None
            for s in skills:
                if s["skill_id"]==sid:
                    skill=s;break
            if not skill:
                await q.answer("스킬없음",show_alert=True)
                return
            dmg=skill["damage"]
            heal=skill["heal"]
            var_dmg=0
            var_heal=0
            if dmg>0:
                var_dmg=random.randint(dmg-2,dmg+2)
                if var_dmg<0:var_dmg=0
                m_hp-=var_dmg
            if heal>0:
                var_heal=random.randint(heal-2,heal+2)
                if var_heal<0:var_heal=0
                p_hp+=var_heal
            dmg_m=0
            if m_hp>0:
                dmg_m=random.randint(m_atk-2,m_atk+2)
                if dmg_m<0:dmg_m=0
                p_hp-=dmg_m
            st["m_hp"]=m_hp
            st["p_hp"]=p_hp
            if p_hp<=0:
                st["phase"]="end"
                await handle_rpg_death(uid)
                await q.edit_message_text("스킬쓰다 패배! HP회복, 60초후 재도전가능.")
                return
            elif m_hp<=0:
                st["phase"]="end"
                await rpg_fight_victory(uid,monster,q,var_dmg,dmg_m,m_hp,p_hp,True)
                return
            else:
                kb=[
                  [InlineKeyboardButton("공격",callback_data=f"rfd_{uid}_atk"),
                   InlineKeyboardButton("스킬",callback_data=f"rfd_{uid}_skill"),
                   InlineKeyboardButton("도망",callback_data=f"rfd_{uid}_run")]
                ]
                txt=(f"{monster}HP:{m_hp}, 내HP:{p_hp}\n"
                     f"(스킬사용 dmg:{var_dmg}, heal:{var_heal},몬공:{dmg_m})\n"
                     "행동선택:")
                await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
        else:
            await q.answer("알수없는action",show_alert=True)

async def handle_rpg_death(uid:int):
    """ 전투 패배 -> HP 풀회복 & 60초 쿨다운 """
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT max_hp FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        c.close();conn.close()
        return
    mhp=row["max_hp"]
    c.execute("UPDATE rpg_characters SET hp=%s WHERE user_id=%s",(mhp,uid))
    conn.commit()
    c.close();conn.close()
    rpg_cooldown[uid]=datetime.datetime.now().timestamp()+60

async def rpg_fight_victory(uid:int, monster:str, q, dmg_p:int, dmg_m:int, m_hp:int, p_hp:int, skillUsed=False):
    reward_exp=30
    reward_gold=50
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT level,exp,gold,hp,max_hp,atk,skill_points FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        c.close();conn.close()
        await q.edit_message_text(f"{monster} 처치!(캐릭터없음 보상X)")
        return
    lv=row["level"]
    xp=row["exp"]+reward_exp
    gold=row["gold"]+reward_gold
    sp=row["skill_points"]
    hp=row["hp"]
    mhp=row["max_hp"]
    atk=row["atk"]
    lvup_count=0
    while xp>=(lv*100):
        xp-=(lv*100)
        lv+=1
        sp+=1
        mhp+=20
        hp=mhp
        atk+=5
        lvup_count+=1
    # 승리 후 HP전부회복
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
         f"획득:EXP+{reward_exp}, GOLD+{reward_gold}{lu_txt}\n"
         "HP 전부 회복!\n"
         "전투끝!")
    await q.edit_message_text(txt)

async def rpg_shop_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    """ 상점 아이템 목록/버튼 """
    conn=get_db_conn()
    c=conn.cursor()
    # 예시로 밸런스 예쁜 아이템 몇개만 미리 insert해놔도 됨(이 코드는 안함)
    c.execute("SELECT * FROM rpg_items ORDER BY price ASC")
    items=c.fetchall()
    c.close();conn.close()
    if not items:
        await update.message.reply_text("상점에 아이템이 없음.")
        return
    text="[상점 목록]\n"
    kb=[]
    for it in items:
        text+=(f"{it['item_id']}.{it['name']} (가격:{it['price']},ATK+{it['atk_bonus']},HP+{it['hp_bonus']})\n")
        kb.append([InlineKeyboardButton(f"{it['name']} 구매",callback_data=f"rpg_shop_buy_{it['item_id']}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def rpg_shop_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    """ 상점 구매 콜백 """
    q=update.callback_query
    data=q.data
    await q.answer()
    if not data.startswith("rpg_shop_buy_"):
        await q.edit_message_text("잘못된 상점콜백.")
        return
    iid=data.split("_",3)[3]
    try:
        item_id=int(iid)
    except:
        await q.edit_message_text("itemID 오류.")
        return
    uid=q.from_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT gold FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        await q.edit_message_text("캐릭터없음.")
        c.close();conn.close()
        return
    p_gold=row["gold"]
    c.execute("SELECT * FROM rpg_items WHERE item_id=%s",(item_id,))
    irow=c.fetchone()
    if not irow:
        await q.edit_message_text("아이템없음.")
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
    await q.edit_message_text(f"{irow['name']} 구매완료! (-{price} gold)")

async def rpg_inventory_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(uid,))
    crow=c.fetchone()
    if not crow:
        await update.message.reply_text("캐릭터없음.")
        c.close();conn.close()
        return
    p_gold=crow["gold"]
    txt=f"[인벤토리]\nGold:{p_gold}\n"
    c.execute("""
    SELECT inv.item_id,inv.quantity,it.name,it.atk_bonus,it.hp_bonus
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
            txt+=(f"{i['name']} x{i['quantity']} (ATK+{i['atk_bonus']},HP+{i['hp_bonus']})\n")
    await update.message.reply_text(txt)

async def rpg_skill_list_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
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
    text=f"[{job} 스킬목록]\n스킬포인트:{sp}\n"
    for s in skills:
        text+=(f"ID:{s['skill_id']} {s['name']} (LvReq:{s['required_level']}, dmg:{s['damage']}, heal:{s['heal']})\n")
    await update.message.reply_text(text)

async def rpg_skill_learn_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
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
    crow=c.fetchone()
    if not crow:
        await update.message.reply_text("캐릭터없음.")
        c.close();conn.close()
        return
    job=crow["job"]
    lv=crow["level"]
    sp=crow["skill_points"]
    c.execute("SELECT * FROM rpg_skills WHERE skill_id=%s AND job=%s",(sid,job))
    srow=c.fetchone()
    if not srow:
        await update.message.reply_text("없는스킬 or 직업불일치.")
        c.close();conn.close()
        return
    if lv<srow["required_level"]:
        await update.message.reply_text("레벨부족.")
        c.close();conn.close()
        return
    if sp<1:
        await update.message.reply_text("스킬포인트부족.")
        c.close();conn.close()
        return
    c.execute("SELECT * FROM rpg_learned_skills WHERE user_id=%s AND skill_id=%s",(uid,sid))
    lr=c.fetchone()
    if lr:
        await update.message.reply_text("이미 배운 스킬.")
        c.close();conn.close()
        return
    c.execute("INSERT INTO rpg_learned_skills(user_id,skill_id) VALUES(%s,%s)",(uid,sid))
    c.execute("UPDATE rpg_characters SET skill_points=skill_points-1 WHERE user_id=%s",(uid,))
    conn.commit()
    c.close();conn.close()
    await update.message.reply_text("스킬습득 완료!")

async def rpg_myinfo_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
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
    msg=(f"[내정보]\n직업:{job}\n"
         f"레벨:{lv}, EXP:{xp}/{lv*100}\n"
         f"HP:{hp}/{mhp}, ATK:{atk}\n"
         f"Gold:{gold}, 스킬포인트:{sp}\n")

    # 아이템 인벤
    c.execute("""
    SELECT inv.item_id,inv.quantity,it.name,it.atk_bonus,it.hp_bonus
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
            msg+=(f"- {i['name']} x{i['quantity']} (ATK+{i['atk_bonus']},HP+{i['hp_bonus']})\n")

    await update.message.reply_text(msg)

###############################################################################
# 9. 인라인 메뉴
###############################################################################
async def menu_callback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    parts=data.split("|",1)
    if len(parts)<2:
        await q.answer("콜백에러", show_alert=True)
        return
    owner_id_str=parts[0]
    cmd=parts[1]
    caller_id=str(q.from_user.id)
    if caller_id!=owner_id_str:
        await q.answer("이건 당신 메뉴가 아닙니다!", show_alert=True)
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
            "/마피아강제시작 <세션ID>\n"
            "/방나가기 <세션ID>\n"
            "(마피아DM) /살해 <세션ID> <유저ID>\n"
            "(의사DM) /치료 <세션ID> <유저ID>\n"
            "(경찰DM)/조사 <세션ID> <유저ID>\n"
            "(그룹)/투표 <세션ID> <유저ID>"
        )
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_games")]]
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_rpg":
        txt=(
            "[RPG]\n"
            "/rpg생성 /rpg직업선택 /rpg상태\n"
            "/던전 /상점 /인벤토리\n"
            "/스킬목록 /스킬습득 <ID>\n"
            "/내정보"
        )
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_games")]]
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
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
        await q.edit_message_text(f"이제 {nowtxt} 되었습니다.", reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_ranking":
        txt=get_daily_ranking_text()
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]]
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_back_main":
        kb=[
          [InlineKeyboardButton("🎮 게임", callback_data=f"{owner_id_str}|menu_games"),
           InlineKeyboardButton("🔧 그룹관리", callback_data=f"{owner_id_str}|menu_group")],
          [InlineKeyboardButton("💳 구독", callback_data=f"{owner_id_str}|menu_subscribe"),
           InlineKeyboardButton("📊 채팅랭킹", callback_data=f"{owner_id_str}|menu_ranking")]
        ]
        await q.edit_message_text("메인 메뉴로 복귀",reply_markup=InlineKeyboardMarkup(kb))
    else:
        await q.edit_message_text("알 수 없는 메뉴.")

###############################################################################
# 11. 일반 텍스트
###############################################################################
async def text_message_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await filter_bad_words_and_spam_and_links(update, context)
    if update.message:
        increment_daily_chat_count(update.effective_user.id)

###############################################################################
# 12. 스케줄러
###############################################################################
def schedule_jobs():
    sch=BackgroundScheduler(timezone=str(KST))
    sch.add_job(reset_daily_chat_count,'cron',hour=0,minute=0)
    sch.start()

###############################################################################
# 13. main()
###############################################################################
def main():
    init_db()
    schedule_jobs()

    app=ApplicationBuilder().token(BOT_TOKEN).build()

    # 영문 명령어
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("adminsecret", admin_secret_command))
    app.add_handler(CommandHandler("announce", announce_command))
    app.add_handler(CommandHandler("subscribe_toggle", subscribe_toggle_command))
    app.add_handler(CommandHandler("vote", vote_command))

    import re
    # 한글 명령어
    app.add_handler(MessageHandler(filters.Regex(r"^/시작(\s.*)?$"), hangeul_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/도움말(\s.*)?$"), hangeul_help_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/랭킹(\s.*)?$"), hangeul_ranking_command))

    # 마피아
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아시작(\s.*)?$"), hangeul_mafia_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/참가(\s.*)?$"), hangeul_mafia_join_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아강제시작(\s.*)?$"), hangeul_mafia_force_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/방나가기(\s.*)?$"), hangeul_mafia_leave_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/살해(\s.*)?$"), hangeul_mafia_kill_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/치료(\s.*)?$"), hangeul_mafia_doctor_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/조사(\s.*)?$"), hangeul_mafia_police_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/투표(\s.*)?$"), hangeul_mafia_vote_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아목록(\s.*)?$"), hangeul_mafia_list_command))

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
