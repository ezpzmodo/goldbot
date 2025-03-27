import os
import logging
import datetime
import random
import asyncio
import pytz

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatMemberUpdated
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler,
    filters, ContextTypes
)

from apscheduler.schedulers.background import BackgroundScheduler

###############################
# 0. 환경 변수 & 기본 설정
###############################
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_URL = os.environ.get("DATABASE_URL", "")
SECRET_ADMIN_KEY = os.environ.get("SECRET_ADMIN_KEY", "MY_SUPER_SECRET")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

# 한국 시간대
KST = pytz.timezone("Asia/Seoul")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

###############################
# 1. DB 연결 & 초기화
###############################
def get_db_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_conn()
    c = conn.cursor()

    # users
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id BIGINT PRIMARY KEY,
      username TEXT,  -- 여기서는 first_name+last_name
      is_subscribed BOOLEAN DEFAULT FALSE,
      is_admin BOOLEAN DEFAULT FALSE,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    # Mafia
    c.execute("""
    CREATE TABLE IF NOT EXISTS mafia_sessions (
      session_id TEXT PRIMARY KEY,
      status TEXT,
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
      role TEXT,   -- 'Mafia','Police','Doctor','Citizen','dead'
      is_alive BOOLEAN DEFAULT TRUE,
      vote_target BIGINT DEFAULT 0,
      heal_target BIGINT DEFAULT 0,
      investigate_target BIGINT DEFAULT 0,
      PRIMARY KEY (session_id,user_id)
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
      PRIMARY KEY(user_id,item_id)
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

    # 일일 채팅 랭킹
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_chat_count (
      user_id BIGINT,
      date_str TEXT,
      count INT DEFAULT 0,
      PRIMARY KEY (user_id, date_str)
    );
    """)

    conn.commit()
    c.close()
    conn.close()

###############################
# 2. 유저/관리자/구독
###############################
def ensure_user_in_db(uid: int, fname: str, lname: str):
    full_name = (fname or "").strip()
    if lname:
        full_name += " " + lname.strip()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=%s",(uid,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users(user_id,username) VALUES(%s,%s)",(uid, full_name.strip()))
    else:
        if row["username"] != full_name.strip():
            c.execute("UPDATE users SET username=%s WHERE user_id=%s",(full_name.strip(),uid))
    conn.commit()
    c.close()
    conn.close()

def is_admin_db(uid: int):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT is_admin FROM users WHERE user_id=%s",(uid,))
    row=c.fetchone()
    c.close()
    conn.close()
    return (row and row["is_admin"])

def set_admin(uid:int,val:bool):
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("UPDATE users SET is_admin=%s WHERE user_id=%s",(val,uid))
    conn.commit()
    c.close()
    conn.close()

def is_subscribed_db(uid:int):
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

###############################
# 3. 그룹 관리(불량단어, 스팸, 링크)
###############################
BAD_WORDS = ["나쁜말1","나쁜말2"]
user_message_times = {}

async def welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu: ChatMemberUpdated = update.chat_member
    if cmu.new_chat_member.status == "member":
        user=cmu.new_chat_member.user
        await context.bot.send_message(
            chat_id=cmu.chat.id,
            text=f"환영합니다, {user.mention_html()}!",
            parse_mode="HTML"
        )
    elif cmu.new_chat_member.status in ("left","kicked"):
        user=cmu.new_chat_member.user
        await context.bot.send_message(
            chat_id=cmu.chat.id,
            text=f"{user.full_name}님이 나갔습니다."
        )

async def filter_bad_words_and_spam_and_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.message
    if not msg: return
    text=msg.text.lower()
    uid=update.effective_user.id

    # 불량단어
    for bad in BAD_WORDS:
        if bad in text:
            await msg.delete()
            return
    # 링크차단(관리자 제외)
    if ("http://" in text or "https://" in text) and (not is_admin_db(uid)):
        await msg.delete()
        return
    # 스팸
    now_ts = datetime.datetime.now().timestamp()
    if uid not in user_message_times:
        user_message_times[uid]=[]
    user_message_times[uid].append(now_ts)
    threshold=now_ts-5
    user_message_times[uid]=[t for t in user_message_times[uid] if t>=threshold]
    if len(user_message_times[uid])>=10:
        await msg.delete()
        return

###############################
# 4. 일일 채팅 랭킹
###############################
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
    c.close()
    conn.close()

def reset_daily_chat_count():
    now=datetime.datetime.now(tz=KST)
    y=now - datetime.timedelta(days=1)
    ys=y.strftime("%Y-%m-%d")
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("DELETE FROM daily_chat_count WHERE date_str=%s",(ys,))
    conn.commit()
    c.close()
    conn.close()

def get_daily_ranking_text():
    """
    사용자 ID는 보여주지 않고, 성+이름(username)만 표기
    """
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
    c.close()
    conn.close()

    if not rows:
        return f"오늘({ds}) 채팅 기록이 없습니다."

    msg=f"=== 오늘({ds}) 채팅랭킹 ===\n"
    rank=1
    for r in rows:
        # username(성+이름) 만 표시
        uname = r["username"] or "이름없음"
        cnt=r["count"]

        if rank==1: prefix="🥇"
        elif rank==2: prefix="🥈"
        elif rank==3: prefix="🥉"
        else: prefix=f"{rank}위:"
        msg += f"{prefix} {uname} ({cnt}회)\n"
        rank+=1
    return msg

###############################
# 5. 영문 명령어
###############################
async def start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    uid=user.id
    ensure_user_in_db(uid, user.first_name or "", user.last_name or "")

    owner_id=str(uid)
    text=(
      "다기능 봇입니다.\n"
      "아래 인라인 버튼은 '호출자'인 당신만 누를 수 있습니다."
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
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    msg=(
      "[도움말]\n"
      "영문명령어:\n"
      "/start, /help, /adminsecret <키>, /announce <메시지>(관리자)\n"
      "/subscribe_toggle, /vote <주제>\n\n"
      "한글명령어:\n"
      "/시작, /도움말, /랭킹\n"
      "/마피아시작, /참가, /마피아강제시작, /살해, /치료, /조사, /투표\n"
      "/rpg생성, /rpg직업선택, /rpg상태, /던전, /상점, /인벤토리\n"
      "/스킬목록, /스킬습득 <스킬ID>"
    )
    await update.message.reply_text(msg)

async def admin_secret_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("비밀키? ex)/adminsecret KEY")
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

async def subscribe_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    old=is_subscribed_db(uid)
    set_subscribe(uid, not old)
    if not old:
        await update.message.reply_text("구독 ON!")
    else:
        await update.message.reply_text("구독 OFF!")

async def vote_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    topic=" ".join(context.args)
    if not topic:
        await update.message.reply_text("사용법: /vote <주제>")
        return
    kb=[
      [InlineKeyboardButton("👍",callback_data=f"vote_yes|{topic}"),
       InlineKeyboardButton("👎",callback_data=f"vote_no|{topic}")]
    ]
    await update.message.reply_text(f"[투표]\n{topic}", reply_markup=InlineKeyboardMarkup(kb))

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

###############################
# 6. 한글 명령어(Regex)
###############################
import re

async def hangeul_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def hangeul_help_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)

async def hangeul_ranking_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt=get_daily_ranking_text()
    await update.message.reply_text(txt)

# 마피아(한글)
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

# RPG(한글)
async def hangeul_rpg_create_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_create_command(update, context)
async def hangeul_rpg_set_job_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_set_job_command(update, context)
async def hangeul_rpg_status_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_status_command(update, context)
async def hangeul_rpg_dungeon_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_dungeon_command(update, context)
async def hangeul_rpg_shop_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_shop_command(update, context)
async def hangeul_rpg_inventory_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_inventory_command(update, context)
async def hangeul_rpg_skill_list_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_skill_list_command(update, context)
async def hangeul_rpg_skill_learn_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await rpg_skill_learn_command(update, context)

###############################
# 7. 마피아 로직
###############################
MAFIA_DEFAULT_DAY_DURATION = 60
MAFIA_DEFAULT_NIGHT_DURATION=30
mafia_tasks={}

async def mafia_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        await update.message.reply_text("그룹에서만.")
        return
    group_id=update.effective_chat.id
    session_id=f"{group_id}_{int(update.message.date.timestamp())}"

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("""
    INSERT INTO mafia_sessions(session_id,status,group_id,day_duration,night_duration)
    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
    """,(session_id,"waiting",group_id,MAFIA_DEFAULT_DAY_DURATION,MAFIA_DEFAULT_NIGHT_DURATION))
    conn.commit()
    c.close()
    conn.close()

    await update.message.reply_text(
      f"마피아 세션 생성: {session_id}\n"
      f"/참가 {session_id} 로 참가\n"
      f"/마피아강제시작 {session_id} 로 시작"
    )

async def mafia_join_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("사용법:/참가 <세션ID>")
        return
    session_id=args[0]
    user=update.effective_user
    uid=user.id
    ensure_user_in_db(uid, user.first_name or "", user.last_name or "")

    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess=c.fetchone()
    if not sess:
        await update.message.reply_text("존재하지 않는 세션.")
        c.close();conn.close();return
    if sess["status"]!="waiting":
        await update.message.reply_text("이미시작됨.")
        c.close();conn.close();return
    c.execute("SELECT * FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,uid))
    row=c.fetchone()
    if row:
        await update.message.reply_text("이미참가.")
        c.close();conn.close();return
    c.execute("INSERT INTO mafia_players(session_id,user_id,role) VALUES(%s,%s,%s)",(session_id,uid,"none"))
    conn.commit()
    c.execute("SELECT COUNT(*) as c FROM mafia_players WHERE session_id=%s",(session_id,))
    n=c.fetchone()["c"]
    c.close();conn.close()
    await update.message.reply_text(f"참가완료. 현재 {n}명.")

async def mafia_force_start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("사용법:/마피아강제시작 <세션ID>")
        return
    session_id=args[0]
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess=c.fetchone()
    if not sess or sess["status"]!="waiting":
        await update.message.reply_text("세션없거나이미시작.")
        c.close();conn.close();return
    c.execute("SELECT user_id FROM mafia_players WHERE session_id=%s",(session_id,))
    rows=c.fetchall()
    players=[r["user_id"] for r in rows]
    if len(players)<5:
        await update.message.reply_text("최소5명 필요.")
        c.close();conn.close();return
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
    c.execute("""
    UPDATE mafia_sessions SET status='night'
    WHERE session_id=%s
    """,(session_id,))
    conn.commit()
    group_id=sess["group_id"]
    day_dur=sess["day_duration"]
    night_dur=sess["night_duration"]
    c.close();conn.close()

    await update.message.reply_text(
      f"마피아 게임시작!(세션:{session_id}) 첫번째 밤."
    )

    for pid in players:
        conn2=get_db_conn()
        c2=conn2.cursor()
        c2.execute("SELECT role FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,pid))
        rr=c2.fetchone()
        c2.close()
        conn2.close()
        rrn=rr["role"]
        if rrn=="Mafia":
            rtext="[마피아]/살해 <세션ID> <유저ID>"
        elif rrn=="Police":
            rtext="[경찰]/조사 <세션ID> <유저ID>"
        elif rrn=="Doctor":
            rtext="[의사]/치료 <세션ID> <유저ID>"
        else:
            rtext="[시민]"
        try:
            await context.bot.send_message(pid, text=f"당신은 {rtext}")
        except:
            pass

    if session_id in mafia_tasks:
        mafia_tasks[session_id].cancel()
    mafia_tasks[session_id] = asyncio.create_task(mafia_cycle(session_id, group_id, day_dur, night_dur, context))

async def mafia_cycle(session_id, group_id, day_dur, night_dur, context):
    while True:
        await asyncio.sleep(night_dur)
        await resolve_night_actions(session_id, group_id, context)

        conn=get_db_conn()
        c=conn.cursor()
        c.execute("UPDATE mafia_sessions SET status='day' WHERE session_id=%s",(session_id,))
        conn.commit()
        c.close();conn.close()
        try:
            await context.bot.send_message(group_id, text=f"밤 끝. 낮({day_dur}초)시작.\n/투표 <세션ID> <유저ID>")
        except:
            pass

        await asyncio.sleep(day_dur)
        ended=await resolve_day_vote(session_id,group_id,context)
        if ended: break

        conn=get_db_conn()
        c=conn.cursor()
        c.execute("UPDATE mafia_sessions SET status='night' WHERE session_id=%s",(session_id,))
        conn.commit()
        c.close();conn.close()
        try:
            await context.bot.send_message(group_id, text=f"낮 끝. 밤({night_dur}초) 시작!")
        except:
            pass

        if check_mafia_win_condition(session_id):
            break

def check_mafia_win_condition(session_id):
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
    doctor_heals={}
    police_invest={}
    for r in rows:
        if r["role"]=="Mafia" and r["is_alive"]:
            if r["vote_target"]!=0:
                mafia_kill_target=r["vote_target"]
        elif r["role"]=="Doctor" and r["is_alive"]:
            if r["heal_target"]!=0:
                doctor_heals[r["user_id"]]=r["heal_target"]
        elif r["role"]=="Police" and r["is_alive"]:
            if r["investigate_target"]!=0:
                police_invest[r["user_id"]]=r["investigate_target"]

    final_dead=None
    if mafia_kill_target:
        healed=any(doctor_heals[k]==mafia_kill_target for k in doctor_heals)
        if not healed:
            c.execute("""
            UPDATE mafia_players
            SET is_alive=FALSE, role='dead'
            WHERE session_id=%s AND user_id=%s
            """,(session_id, mafia_kill_target))
            final_dead=mafia_kill_target

    for pol_id, suspect_id in police_invest.items():
        c.execute("""
        SELECT role,is_alive FROM mafia_players
        WHERE session_id=%s AND user_id=%s
        """,(session_id,suspect_id))
        sr=c.fetchone()
        if sr:
            role_info=sr["role"]
            try:
                await context.bot.send_message(pol_id, text=f"[조사결과]{suspect_id}:{role_info}")
            except:
                pass

    c.execute("""
    UPDATE mafia_players
    SET vote_target=0,heal_target=0,investigate_target=0
    WHERE session_id=%s
    """,(session_id,))
    conn.commit()
    c.close()
    conn.close()

    if final_dead:
        try:
            await context.bot.send_message(group_id, text=f"밤에 {final_dead}님 사망")
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
            await context.bot.send_message(group_id, text="투표없음.")
        except:
            pass
        if check_mafia_win_condition(session_id):
            await context.bot.send_message(group_id, text="게임종료.")
            return True
        return False
    vote_count={}
    for v in votes:
        vt=v["vote_target"]
        vote_count[vt]=vote_count.get(vt,0)+1
    sorted_v=sorted(vote_count.items(),key=lambda x:x[1],reverse=True)
    top_user, top_cnt=sorted_v[0]
    c.execute("""
    UPDATE mafia_players
    SET is_alive=FALSE, role='dead'
    WHERE session_id=%s AND user_id=%s
    """,(session_id,top_user))
    conn.commit()
    c.close();conn.close()
    try:
        await context.bot.send_message(group_id, text=f"{top_user}님이 {top_cnt}표로 처형")
    except:
        pass
    if check_mafia_win_condition(session_id):
        await context.bot.send_message(group_id, text="게임종료!")
        return True
    return False

async def mafia_kill_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인채팅에서만.")
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용법:/살해 <세션ID> <유저ID>")
        return
    sess_id, tgt_str=args[0],args[1]
    try:
        tgt_id=int(tgt_str)
    except:
        await update.message.reply_text("유효ID아님.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Mafia" or not row["is_alive"]:
        await update.message.reply_text("마피아아님 or 사망.")
        c.close();conn.close();return
    c.execute("UPDATE mafia_players SET vote_target=%s WHERE session_id=%s AND user_id=%s",(tgt_id,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt_id}님 살해타겟설정.")

async def mafia_doctor_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인채팅.")
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용법:/치료 <세션ID> <유저ID>")
        return
    sess_id, tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("ID에러.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    row=c.fetchone()
    if not row or row["role"]!="Doctor" or not row["is_alive"]:
        await update.message.reply_text("의사아님 or 사망.")
        c.close();conn.close();return
    c.execute("UPDATE mafia_players SET heal_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님 치료타겟 설정.")

async def mafia_police_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인채팅.")
        return
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용법:/조사 <세션ID> <유저ID>")
        return
    sess_id, tgt_str=args[0],args[1]
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
    if not row or row["role"]!="Police" or not row["is_alive"]:
        await update.message.reply_text("경찰아님 or 사망.")
        c.close();conn.close();return
    c.execute("UPDATE mafia_players SET investigate_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님 조사타겟 설정.")

async def mafia_vote_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args
    if len(args)<2:
        await update.message.reply_text("사용법:/투표 <세션ID> <유저ID>")
        return
    sess_id, tgt_str=args[0],args[1]
    try:
        tgt=int(tgt_str)
    except:
        await update.message.reply_text("ID오류.")
        return
    uid=update.effective_user.id
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT status FROM mafia_sessions WHERE session_id=%s",(sess_id,))
    sr=c.fetchone()
    if not sr or sr["status"]!="day":
        await update.message.reply_text("현재 낮아님.")
        c.close();conn.close();return
    c.execute("SELECT is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(sess_id,uid))
    rr=c.fetchone()
    if not rr or not rr["is_alive"]:
        await update.message.reply_text("당신은 죽었거나참가X.")
        c.close();conn.close();return
    c.execute("UPDATE mafia_players SET vote_target=%s WHERE session_id=%s AND user_id=%s",(tgt,sess_id,uid))
    conn.commit();c.close();conn.close()
    await update.message.reply_text(f"{tgt}님에게 투표.")

###############################
# 8. RPG
###############################
# (생략없이 포함: 캐릭터, 상점, 등등)

# 위에서 이미 선언( rpg_create_command, rpg_set_job_command, etc. )

###############################
# 9. 던전(턴제 전투+스킬)
###############################
rpg_fight_state={} # uid-> dict(...)

async def rpg_dungeon_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    kb=[
      [InlineKeyboardButton("쉬움",callback_data="rdsel_easy")],
      [InlineKeyboardButton("보통",callback_data="rdsel_normal")],
      [InlineKeyboardButton("어려움",callback_data="rdsel_hard")]
    ]
    await update.message.reply_text("던전난이도선택(턴제+스킬):",reply_markup=InlineKeyboardMarkup(kb))

async def rpg_dungeon_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    await q.answer()
    uid=q.from_user.id

    if not data.startswith("rdsel_"):return
    diff=data.split("_")[1]
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
        c.close();conn.close();return
    p_hp=char["hp"]
    p_atk=char["atk"]
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
    kb=[[InlineKeyboardButton("👊 Attack",callback_data=f"rfd_{uid}_atk"),
         InlineKeyboardButton("🔥 Skill",callback_data=f"rfd_{uid}_skill"),
         InlineKeyboardButton("🏃 Run",callback_data=f"rfd_{uid}_run")]]
    txt=(f"{monster} 출현!\n몬스터HP:{mhp},내HP:{p_hp}\n행동선택:")
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
        await q.answer("이 전투는 당신이 아님!",show_alert=True)
        return
    st=rpg_fight_state.get(uid)
    if not st or st["phase"]!="ongoing":
        await q.answer("전투종료 or 없음.")
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
        dmg_p=random.randint(p_atk-2,p_atk+2) if p_atk>2 else p_atk
        if dmg_p<0:dmg_p=0
        m_hp-=dmg_p
        dmg_m=0
        if m_hp>0:
            dmg_m=random.randint(m_atk-2,m_atk+2) if m_atk>2 else m_atk
            if dmg_m<0:dmg_m=0
            p_hp-=dmg_m
        st["m_hp"]=m_hp
        st["p_hp"]=p_hp
        if p_hp<=0:
            st["phase"]="end"
            p_hp=1
            st["p_hp"]=1
            await q.edit_message_text(f"{monster}에게 패배..HP->1\n전투종료!")
            return
        elif m_hp<=0:
            st["phase"]="end"
            await rpg_fight_victory(uid,monster,q,dmg_p,dmg_m,m_hp,p_hp)
            return
        else:
            kb=[[InlineKeyboardButton("👊 Attack",callback_data=f"rfd_{uid}_atk"),
                 InlineKeyboardButton("🔥 Skill",callback_data=f"rfd_{uid}_skill"),
                 InlineKeyboardButton("🏃 Run",callback_data=f"rfd_{uid}_run")]]
            txt=(f"{monster}HP:{m_hp},내HP:{p_hp}\n(내공격:{dmg_p},몬공:{dmg_m})\n행동선택:")
            await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    elif action=="skill":
        # 배운스킬
        if not skills:
            await q.edit_message_text("배운스킬없음.")
            return
        kb=[]
        for s in skills:
            sid=s["skill_id"]
            nm=s["name"]
            kb.append([InlineKeyboardButton(nm, callback_data=f"rfd_{uid}_useSkill_{sid}")])
        kb.append([InlineKeyboardButton("뒤로",callback_data=f"rfd_{uid}_back")])
        txt=(f"스킬선택.\n{monster}HP:{m_hp},내HP:{p_hp}")
        await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    elif action=="back":
        kb=[[InlineKeyboardButton("👊 Attack",callback_data=f"rfd_{uid}_atk"),
             InlineKeyboardButton("🔥 Skill",callback_data=f"rfd_{uid}_skill"),
             InlineKeyboardButton("🏃 Run",callback_data=f"rfd_{uid}_run")]]
        txt=(f"{monster}HP:{m_hp},내HP:{p_hp}\n행동선택:")
        await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    else:
        # skill
        if action.startswith("useSkill"):
            sid_str=action.split("_")[1] if "_" in action else None
            if not sid_str:
                await q.answer("스킬ID오류",show_alert=True)
                return
            try:
                sid=int(sid_str)
            except:
                await q.answer("스킬ID파싱오류",show_alert=True)
                return
            skill=None
            for s in skills:
                if s["skill_id"]==sid:
                    skill=s;break
            if not skill:
                await q.answer("해당스킬없음",show_alert=True)
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
                p_hp=1
                st["p_hp"]=1
                await q.edit_message_text("스킬쓰다 사망..HP->1\n전투종료!")
                return
            elif m_hp<=0:
                st["phase"]="end"
                await rpg_fight_victory(uid,monster,q,var_dmg,dmg_m,m_hp,p_hp,True)
                return
            else:
                kb=[[InlineKeyboardButton("👊 Attack",callback_data=f"rfd_{uid}_atk"),
                     InlineKeyboardButton("🔥 Skill",callback_data=f"rfd_{uid}_skill"),
                     InlineKeyboardButton("🏃 Run",callback_data=f"rfd_{uid}_run")]]
                txt=(f"{monster}HP:{m_hp},내HP:{p_hp}\n"
                     f"(스킬사용 dmg:{var_dmg}, heal:{var_heal},몬공:{dmg_m})\n"
                     "행동선택:")
                await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await q.answer("action오류",show_alert=True)

async def rpg_fight_victory(uid:int, monster:str, query, dmg_p:int, dmg_m:int, m_hp:int, p_hp:int, skillUsed=False):
    # 전투승리 보상
    reward_exp=30
    reward_gold=50
    conn=get_db_conn()
    c=conn.cursor()
    c.execute("SELECT level,exp,gold,hp,max_hp,atk,skill_points FROM rpg_characters WHERE user_id=%s",(uid,))
    row=c.fetchone()
    if not row:
        c.close();conn.close()
        await query.edit_message_text(f"{monster} 처치!\n(캐릭터없어서 보상X)\n전투종료!")
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
    c.execute("""
    UPDATE rpg_characters
    SET exp=%s,gold=%s,level=%s,skill_points=%s,hp=%s,max_hp=%s,atk=%s
    WHERE user_id=%s
    """,(xp,gold,lv,sp,hp,mhp,atk,uid))
    conn.commit()
    c.close();conn.close()
    lu_txt=""
    if lvup_count>0:
        lu_txt=f"\n레벨 {lvup_count}회 상승!"
    txt=(f"{monster} 처치!\n"
         f"획득:EXP+{reward_exp},GOLD+{reward_gold}{lu_txt}\n"
         "전투종료!")
    await query.edit_message_text(txt)

###############################
# 10. 인라인 메뉴(호출자 제한)
###############################
async def menu_callback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    parts=data.split("|",1)
    if len(parts)<2:
        await q.answer("콜백에러",show_alert=True)
        return
    owner_id_str=parts[0]
    cmd=parts[1]
    uid_str=str(q.from_user.id)
    if uid_str!=owner_id_str:
        await q.answer("이건 당신 메뉴가 아님!", show_alert=True)
        return
    await q.answer()

    if cmd=="menu_games":
        kb=[
          [InlineKeyboardButton("마피아", callback_data=f"{owner_id_str}|menu_mafia")],
          [InlineKeyboardButton("RPG", callback_data=f"{owner_id_str}|menu_rpg")],
          [InlineKeyboardButton("뒤로", callback_data=f"{owner_id_str}|menu_back_main")]
        ]
        await q.edit_message_text("게임 메뉴",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_mafia":
        txt=("/마피아시작\n/참가 <세션ID>\n/마피아강제시작 <세션ID>\n"
             "/살해 /치료 /조사 /투표(그룹)")
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_games")]]
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_rpg":
        txt=("/rpg생성\n/rpg직업선택\n/rpg상태\n/던전(턴제+스킬)\n/상점\n/인벤토리\n"
             "/스킬목록 /스킬습득 <스킬ID>")
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
        await q.edit_message_text("공지:/announce <메시지>(관리자)",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_group_vote":
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_group")]]
        await q.edit_message_text("투표:/vote <주제>",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_subscribe":
        s=is_subscribed_db(int(uid_str))
        stat="구독자 ✅" if s else "비구독 ❌"
        toggle="구독해지" if s else "구독하기"
        kb=[
          [InlineKeyboardButton(toggle,callback_data=f"{owner_id_str}|menu_sub_toggle")],
          [InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]
        ]
        await q.edit_message_text(f"현재상태:{stat}", reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_sub_toggle":
        s=is_subscribed_db(int(uid_str))
        set_subscribe(int(uid_str),not s)
        nowtxt="구독자 ✅" if not s else "비구독 ❌"
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_subscribe")]]
        await q.edit_message_text(f"이제 {nowtxt} 되었습니다.",reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_ranking":
        txt=get_daily_ranking_text()
        kb=[[InlineKeyboardButton("뒤로",callback_data=f"{owner_id_str}|menu_back_main")]]
        await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
    elif cmd=="menu_back_main":
        kb=[
          [InlineKeyboardButton("🎮 게임", callback_data=f"{owner_id_str}|menu_games"),
           InlineKeyboardButton("🔧 그룹관리", callback_data=f"{owner_id_str}|menu_group")],
          [InlineKeyboardButton("💳 구독", callback_data=f"{owner_id_str}|menu_subscribe"),
           InlineKeyboardButton("📊 채팅랭킹", callback_data=f"{owner_id_str}|menu_ranking")]
        ]
        await q.edit_message_text("메인메뉴로 복귀",reply_markup=InlineKeyboardMarkup(kb))
    else:
        await q.edit_message_text("알수없는메뉴.")

###############################
# 11. 일반 텍스트(명령어X)
###############################
async def text_message_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await filter_bad_words_and_spam_and_links(update, context)
    if update.message:
        increment_daily_chat_count(update.effective_user.id)

###############################
# 12. 스케줄러
###############################
def schedule_jobs():
    sch=BackgroundScheduler(timezone=str(KST))
    sch.add_job(reset_daily_chat_count,'cron',hour=0,minute=0)
    sch.start()

###############################
# 13. main()
###############################
def main():
    init_db()
    schedule_jobs()

    app=ApplicationBuilder().token(BOT_TOKEN).build()

    # (1) 영문명령어
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("adminsecret", admin_secret_command))
    app.add_handler(CommandHandler("announce", announce_command))
    app.add_handler(CommandHandler("subscribe_toggle", subscribe_toggle_command))
    app.add_handler(CommandHandler("vote", vote_command))

    # (2) 한글명령어(Regex)
    import re
    app.add_handler(MessageHandler(filters.Regex(r"^/시작(\s.*)?$"), hangeul_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/도움말(\s.*)?$"), hangeul_help_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/랭킹(\s.*)?$"), hangeul_ranking_command))

    app.add_handler(MessageHandler(filters.Regex(r"^/마피아시작(\s.*)?$"), hangeul_mafia_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/참가(\s.*)?$"), hangeul_mafia_join_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아강제시작(\s.*)?$"), hangeul_mafia_force_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/살해(\s.*)?$"), hangeul_mafia_kill_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/치료(\s.*)?$"), hangeul_mafia_doctor_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/조사(\s.*)?$"), hangeul_mafia_police_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/투표(\s.*)?$"), hangeul_mafia_vote_command))

    app.add_handler(MessageHandler(filters.Regex(r"^/rpg생성(\s.*)?$"), hangeul_rpg_create_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg직업선택(\s.*)?$"), hangeul_rpg_set_job_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg상태(\s.*)?$"), hangeul_rpg_status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/던전(\s.*)?$"), hangeul_rpg_dungeon_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/상점(\s.*)?$"), hangeul_rpg_shop_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/인벤토리(\s.*)?$"), hangeul_rpg_inventory_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/스킬목록(\s.*)?$"), hangeul_rpg_skill_list_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/스킬습득(\s.*)?$"), hangeul_rpg_skill_learn_command))

    # (3) 콜백핸들러
    app.add_handler(CallbackQueryHandler(vote_callback_handler, pattern="^vote_(yes|no)\\|"))
    app.add_handler(CallbackQueryHandler(rpg_dungeon_callback, pattern="^rdsel_.*"))
    app.add_handler(CallbackQueryHandler(rpg_fight_action_callback, pattern="^rfd_.*"))
    app.add_handler(CallbackQueryHandler(rpg_job_callback_handler, pattern="^rpgjob_.*"))
    app.add_handler(CallbackQueryHandler(rpg_shop_callback, pattern="^rpgshop_buy_.*"))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^.*\\|menu_.*"))

    # 환영/퇴장
    app.add_handler(ChatMemberHandler(welcome_message, ChatMemberHandler.CHAT_MEMBER))

    # 일반 텍스트
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    logger.info("봇 시작!")
    app.run_polling()

# 실행부
if __name__=="__main__":
    main()