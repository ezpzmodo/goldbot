import os
import logging
import datetime
import pytz
import random
import asyncio

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatMemberUpdated
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler,
    filters, ContextTypes
)

# PostgreSQL 연동
import psycopg2
from psycopg2.extras import RealDictCursor

# APScheduler (비동기 스케줄)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

########################################
# 0. 환경 변수 및 기본 설정
########################################
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_URL = os.environ.get("DATABASE_URL", "")
SECRET_ADMIN_KEY = os.environ.get("SECRET_ADMIN_KEY", "MY_SUPER_SECRET")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

# 한국 시간대 (매일 랭킹 리셋 등에 사용)
KST = pytz.timezone("Asia/Seoul")

# 로깅
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

########################################
# 1. DB 연결 함수 및 마이그레이션
########################################
def get_db_conn():
    """PostgreSQL 연결(매번 새 커넥션, 실제 운영에선 커넥션 풀 사용 권장)."""
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    """
    테이블 생성 + 필요한 칼럼이 없으면 추가(간단 마이그레이션).
    *AI 기능 제거 버전*
    """
    conn = get_db_conn()
    cur = conn.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id BIGINT PRIMARY KEY,
      username TEXT,
      is_subscribed BOOLEAN DEFAULT FALSE,
      is_admin BOOLEAN DEFAULT FALSE,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    # Mafia
    cur.execute("""
    CREATE TABLE IF NOT EXISTS mafia_sessions (
      session_id TEXT PRIMARY KEY,
      status TEXT,
      group_id BIGINT,
      created_at TIMESTAMP DEFAULT NOW(),
      day_duration INT DEFAULT 60,
      night_duration INT DEFAULT 30
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS mafia_players (
      session_id TEXT,
      user_id BIGINT,
      role TEXT,   -- 'Mafia','Police','Doctor','Citizen','dead'
      is_alive BOOLEAN DEFAULT TRUE,
      vote_target BIGINT DEFAULT 0,
      heal_target BIGINT DEFAULT 0,
      investigate_target BIGINT DEFAULT 0,
      PRIMARY KEY (session_id, user_id)
    );
    """)

    # RPG
    cur.execute("""
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
    cur.execute("""
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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rpg_learned_skills (
      user_id BIGINT,
      skill_id INT,
      PRIMARY KEY (user_id, skill_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rpg_items (
      item_id SERIAL PRIMARY KEY,
      name TEXT,
      price INT,
      atk_bonus INT DEFAULT 0,
      hp_bonus INT DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rpg_inventory (
      user_id BIGINT,
      item_id INT,
      quantity INT DEFAULT 1,
      PRIMARY KEY (user_id, item_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rpg_parties (
      party_id SERIAL PRIMARY KEY,
      leader_id BIGINT,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rpg_party_members (
      party_id INT,
      user_id BIGINT,
      PRIMARY KEY (party_id, user_id)
    );
    """)

    # 일일 채팅 랭킹
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_chat_count (
      user_id BIGINT,
      date_str TEXT,
      count INT DEFAULT 0,
      PRIMARY KEY (user_id, date_str)
    );
    """)

    conn.commit()
    cur.close()
    conn.close()

########################################
# 2. 유저/구독/관리자 유틸
########################################
def ensure_user_in_db(user_id: int, username: str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("""
        INSERT INTO users (user_id, username)
        VALUES (%s, %s)
        """, (user_id, username))
    else:
        if row["username"] != username:
            cur.execute("""
            UPDATE users SET username=%s WHERE user_id=%s
            """, (username, user_id))
    conn.commit()
    cur.close()
    conn.close()

def is_admin_db(user_id: int) -> bool:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row and row["is_admin"]

def set_admin(user_id: int, value: bool):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin=%s WHERE user_id=%s", (value, user_id))
    conn.commit()
    cur.close()
    conn.close()

def is_subscribed_db(user_id: int) -> bool:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT is_subscribed FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row and row["is_subscribed"]

def set_subscribe(user_id: int, value: bool):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_subscribed=%s WHERE user_id=%s", (value, user_id))
    conn.commit()
    cur.close()
    conn.close()

########################################
# 3. 그룹 관리(불량단어, 스팸, 일일채팅)
########################################
BAD_WORDS = ["나쁜말1", "나쁜말2"]  # 예시
SPAM_THRESHOLD = 5
user_message_times = {}  # user_id -> list of timestamps

async def welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """그룹에 새 멤버 들어오면 환영, 나가면 안내."""
    chat_member_update: ChatMemberUpdated = update.chat_member
    if chat_member_update.new_chat_member.status == "member":
        user = chat_member_update.new_chat_member.user
        await context.bot.send_message(
            chat_id=chat_member_update.chat.id,
            text=f"환영합니다, {user.mention_html()}님!",
            parse_mode="HTML"
        )
    elif chat_member_update.new_chat_member.status in ("left","kicked"):
        user = chat_member_update.new_chat_member.user
        await context.bot.send_message(
            chat_id=chat_member_update.chat.id,
            text=f"{user.full_name}님이 나갔습니다."
        )

async def filter_bad_words_and_spam_and_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """불량단어, 링크, 스팸 처리."""
    message = update.message
    if not message:
        return
    text = message.text.lower()
    user_id = update.effective_user.id

    # 불량단어 필터
    for bad in BAD_WORDS:
        if bad in text:
            await message.delete()
            return

    # 링크 차단(관리자 제외)
    if ("http://" in text or "https://" in text) and (not is_admin_db(user_id)):
        await message.delete()
        return

    # 스팸(5초안에 10개이상)
    now_ts = datetime.datetime.now().timestamp()
    if user_id not in user_message_times:
        user_message_times[user_id] = []
    user_message_times[user_id].append(now_ts)
    threshold_time = now_ts - 5
    user_message_times[user_id] = [t for t in user_message_times[user_id] if t >= threshold_time]
    if len(user_message_times[user_id]) >= 10:
        await message.delete()
        return

def increment_daily_chat_count(user_id: int):
    """매 메시지마다 +1"""
    now = datetime.datetime.now(tz=KST)
    date_str = now.strftime("%Y-%m-%d")
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO daily_chat_count(user_id,date_str,count)
    VALUES(%s,%s,1)
    ON CONFLICT(user_id,date_str)
    DO UPDATE SET count=daily_chat_count.count+1
    """,(user_id,date_str))
    conn.commit()
    cur.close()
    conn.close()

def reset_daily_chat_count():
    """매일 0시 전날 기록 삭제."""
    now = datetime.datetime.now(tz=KST)
    yesterday = now - datetime.timedelta(days=1)
    y_str = yesterday.strftime("%Y-%m-%d")

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_chat_count WHERE date_str=%s",(y_str,))
    conn.commit()
    cur.close()
    conn.close()

def get_daily_ranking_text():
    """오늘 날짜의 채팅 랭킹 top 10"""
    now = datetime.datetime.now(tz=KST)
    date_str = now.strftime("%Y-%m-%d")

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT dc.user_id, dc.count, u.username
    FROM daily_chat_count dc
    LEFT JOIN users u ON u.user_id=dc.user_id
    WHERE dc.date_str=%s
    ORDER BY dc.count DESC
    LIMIT 10
    """,(date_str,))
    rows=cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return f"오늘({date_str}) 채팅 기록이 없습니다."
    msg = f"=== 오늘({date_str}) 채팅랭킹 ===\n"
    rank=1
    for r in rows:
        uname = r["username"] if r["username"] else str(r["user_id"])
        cnt = r["count"]
        msg += f"{rank}위: {uname} ({cnt}회)\n"
        rank += 1
    return msg

########################################
# 4. 영문 명령어 -> CommandHandler
########################################
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    ensure_user_in_db(user_id, username)

    text = (
        "다기능 봇입니다.\n"
        "아래 메뉴를 통해 마피아, RPG, 그룹 관리, 구독 등 다양한 기능을 활용하세요.\n"
        "AI 기능은 제공되지 않습니다."
    )
    keyboard = [
        [InlineKeyboardButton("🎮 게임", callback_data="menu_games")],
        [InlineKeyboardButton("🔧 그룹관리", callback_data="menu_group")],
        [InlineKeyboardButton("💳 구독", callback_data="menu_subscribe")],
        [InlineKeyboardButton("📊 채팅랭킹", callback_data="menu_ranking")],
    ]
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """[도움말 - 영문명령어]
/start : 봇 시작
/help : 도움말
/adminsecret <키> : 관리자 권한 획득
/announce <메시지> : 공지(관리자 전용)
/subscribe_toggle : 구독 토글
/vote <주제> : 투표 생성

(한글 명령어는 Regex로 처리, 아래 참고)
"""
    await update.message.reply_text(msg)

async def admin_secret_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("비밀키를 입력하세요. 예) /adminsecret MYKEY")
        return
    if args[0] == SECRET_ADMIN_KEY:
        set_admin(update.effective_user.id, True)
        await update.message.reply_text("관리자 권한 획득!")
    else:
        await update.message.reply_text("비밀키 불일치.")

async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin_db(user_id):
        await update.message.reply_text("관리자 전용.")
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("공지할 내용을 입력하세요.")
        return
    await update.message.reply_text(f"[공지]\n{msg}")

async def subscribe_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur_val = is_subscribed_db(user_id)
    set_subscribe(user_id, not cur_val)
    if not cur_val:
        await update.message.reply_text("구독 ON!")
    else:
        await update.message.reply_text("구독 해제!")

########################################
# 5. 한글 명령어 -> MessageHandler + Regex
########################################
# (기본)
async def hangeul_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def hangeul_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)

async def hangeul_ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = get_daily_ranking_text()
    await update.message.reply_text(txt)

# (마피아)
async def hangeul_mafia_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_start_command(update, context)

async def hangeul_mafia_join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_join_command(update, context)

async def hangeul_mafia_force_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_force_start_command(update, context)

async def hangeul_mafia_kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_kill_command(update, context)

async def hangeul_mafia_doctor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_doctor_command(update, context)

async def hangeul_mafia_police_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_police_command(update, context)

async def hangeul_mafia_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mafia_vote_command(update, context)

# (RPG)
async def hangeul_rpg_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_create_command(update, context)

async def hangeul_rpg_set_job_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_set_job_command(update, context)

async def hangeul_rpg_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_status_command(update, context)

async def hangeul_rpg_dungeon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_dungeon_command(update, context)

async def hangeul_rpg_shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_shop_command(update, context)

async def hangeul_rpg_inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_inventory_command(update, context)

async def hangeul_rpg_skill_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_skill_list_command(update, context)

async def hangeul_rpg_skill_learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rpg_skill_learn_command(update, context)

########################################
# 6. 마피아 (영문 함수 본체, 호출은 위에서)
########################################
MAFIA_DEFAULT_DAY_DURATION = 60
MAFIA_DEFAULT_NIGHT_DURATION = 30
mafia_tasks = {}  # session_id -> asyncio.Task

async def mafia_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"):
        await update.message.reply_text("이 명령은 그룹에서만 사용 가능합니다.")
        return
    group_id = update.effective_chat.id
    session_id = f"{group_id}_{int(update.message.date.timestamp())}"

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO mafia_sessions(session_id,status,group_id,day_duration,night_duration)
    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
    """,(session_id,"waiting",group_id,MAFIA_DEFAULT_DAY_DURATION,MAFIA_DEFAULT_NIGHT_DURATION))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"마피아 세션 생성: {session_id}\n"
        f"/참가 {session_id} 로 참가하세요.\n"
        f"/마피아강제시작 {session_id} 로 시작."
    )

async def mafia_join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("사용법: /참가 <세션ID>")
        return
    session_id = args[0]
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    ensure_user_in_db(user_id, username)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess = cur.fetchone()
    if not sess:
        await update.message.reply_text("존재하지 않는 세션입니다.")
        cur.close()
        conn.close()
        return
    if sess["status"]!="waiting":
        await update.message.reply_text("이미 시작된 세션입니다.")
        cur.close()
        conn.close()
        return

    cur.execute("SELECT * FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,user_id))
    row = cur.fetchone()
    if row:
        await update.message.reply_text("이미 참가중입니다.")
        cur.close()
        conn.close()
        return

    cur.execute("""
    INSERT INTO mafia_players(session_id,user_id,role)
    VALUES(%s,%s,%s)
    """,(session_id,user_id,"none"))
    conn.commit()

    cur.execute("SELECT COUNT(*) as c FROM mafia_players WHERE session_id=%s",(session_id,))
    count = cur.fetchone()["c"]
    cur.close()
    conn.close()

    await update.message.reply_text(f"참가 완료! 현재 {count}명 참여중.")

async def mafia_force_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("사용법: /마피아강제시작 <세션ID>")
        return
    session_id = args[0]

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess = cur.fetchone()
    if not sess or sess["status"]!="waiting":
        await update.message.reply_text("세션이 없거나 이미 시작됨.")
        cur.close()
        conn.close()
        return

    # 참가자
    cur.execute("SELECT user_id FROM mafia_players WHERE session_id=%s",(session_id,))
    rows = cur.fetchall()
    players = [r["user_id"] for r in rows]
    if len(players)<5:
        await update.message.reply_text("최소 5명 필요(마피아/경찰/의사 각1, 시민 2이상).")
        cur.close()
        conn.close()
        return

    random.shuffle(players)
    mafia_id = players[0]
    police_id = players[1]
    doctor_id = players[2]
    for i, pid in enumerate(players):
        if pid == mafia_id:
            role = "Mafia"
        elif pid == police_id:
            role = "Police"
        elif pid == doctor_id:
            role = "Doctor"
        else:
            role = "Citizen"
        cur.execute("""
        UPDATE mafia_players
        SET role=%s,is_alive=TRUE,vote_target=0,heal_target=0,investigate_target=0
        WHERE session_id=%s AND user_id=%s
        """,(role, session_id, pid))

    cur.execute("UPDATE mafia_sessions SET status='night' WHERE session_id=%s",(session_id,))
    conn.commit()

    group_id = sess["group_id"]
    day_dur = sess["day_duration"]
    night_dur = sess["night_duration"]

    cur.close()
    conn.close()

    await update.message.reply_text(
        f"마피아 게임 시작! (세션:{session_id})\n"
        "첫 번째 밤이 시작되었습니다."
    )

    # 개별 역할 안내(개인 채팅)
    for pid in players:
        conn2 = get_db_conn()
        c2 = conn2.cursor()
        c2.execute("SELECT role FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,pid))
        r2 = c2.fetchone()
        c2.close()
        conn2.close()

        role_name = r2["role"]
        if role_name=="Mafia":
            rtext = "[마피아] 밤에 /살해 <세션ID> <유저ID>"
        elif role_name=="Police":
            rtext = "[경찰] 밤에 /조사 <세션ID> <유저ID>"
        elif role_name=="Doctor":
            rtext = "[의사] 밤에 /치료 <세션ID> <유저ID>"
        else:
            rtext = "[시민] (특별 명령 없음)"
        try:
            await context.bot.send_message(pid, text=f"당신은 {rtext}")
        except:
            pass

    # 낮/밤 자동 진행
    if session_id in mafia_tasks:
        mafia_tasks[session_id].cancel()
    mafia_tasks[session_id] = asyncio.create_task(mafia_cycle(session_id, group_id, day_dur, night_dur, context))

async def mafia_cycle(session_id, group_id, day_dur, night_dur, context: ContextTypes.DEFAULT_TYPE):
    """
    밤 -> 낮 -> 밤 -> 낮... 자동 반복.
    """
    while True:
        # 밤 대기
        await asyncio.sleep(night_dur)
        await resolve_night_actions(session_id, group_id, context)
        # 낮 전환
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE mafia_sessions SET status='day' WHERE session_id=%s",(session_id,))
        conn.commit()
        cur.close()
        conn.close()
        try:
            await context.bot.send_message(group_id, text=f"밤이 끝났습니다. 낮({day_dur}초) 시작!\n/투표 <세션ID> <유저ID>")
        except:
            pass

        # 낮 대기
        await asyncio.sleep(day_dur)
        ended = await resolve_day_vote(session_id, group_id, context)
        if ended:
            break

        # 다시 밤
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE mafia_sessions SET status='night' WHERE session_id=%s",(session_id,))
        conn.commit()
        cur.close()
        conn.close()
        try:
            await context.bot.send_message(group_id, text=f"낮이 끝났습니다. 밤({night_dur}초) 시작!")
        except:
            pass

        if check_mafia_win_condition(session_id):
            break

def check_mafia_win_condition(session_id: str):
    """마피아/시민 생존자 체크 -> 승리조건."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT role,is_alive FROM mafia_players WHERE session_id=%s",(session_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    alive_mafia = 0
    alive_citizen = 0
    for r in rows:
        if not r["is_alive"]:
            continue
        if r["role"]=="Mafia":
            alive_mafia+=1
        else:
            alive_citizen+=1
    # 마피아=0 -> 시민 승
    # 시민=0 -> 마피아 승
    return (alive_mafia==0 or alive_citizen==0)

async def resolve_night_actions(session_id, group_id, context: ContextTypes.DEFAULT_TYPE):
    """
    밤에 마피아/의사/경찰 행동 처리
    """
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id,role,is_alive,vote_target,heal_target,investigate_target
    FROM mafia_players
    WHERE session_id=%s
    """,(session_id,))
    rows = cur.fetchall()

    mafia_kill_target = None
    doctor_heals = {}
    police_investigates = {}

    for r in rows:
        if r["role"]=="Mafia" and r["is_alive"]:
            if r["vote_target"]!=0:
                mafia_kill_target = r["vote_target"]
        elif r["role"]=="Doctor" and r["is_alive"]:
            if r["heal_target"]!=0:
                doctor_heals[r["user_id"]] = r["heal_target"]
        elif r["role"]=="Police" and r["is_alive"]:
            if r["investigate_target"]!=0:
                police_investigates[r["user_id"]] = r["investigate_target"]

    final_dead = None
    if mafia_kill_target:
        # 의사 치료 확인
        healed = any(ht==mafia_kill_target for ht in doctor_heals.values())
        if not healed:
            # 죽임
            cur.execute("""
            UPDATE mafia_players
            SET is_alive=FALSE, role='dead'
            WHERE session_id=%s AND user_id=%s
            """,(session_id, mafia_kill_target))
            final_dead = mafia_kill_target

    # 경찰 조사 -> DM
    for pol_id, suspect_id in police_investigates.items():
        cur.execute("""
        SELECT role,is_alive FROM mafia_players
        WHERE session_id=%s AND user_id=%s
        """,(session_id,suspect_id))
        srow = cur.fetchone()
        if srow:
            role_info = srow["role"]
            try:
                await context.bot.send_message(pol_id, text=f"[조사결과] {suspect_id} : {role_info}")
            except:
                pass

    # 액션 리셋
    cur.execute("""
    UPDATE mafia_players
    SET vote_target=0, heal_target=0, investigate_target=0
    WHERE session_id=%s
    """,(session_id,))
    conn.commit()
    cur.close()
    conn.close()

    if final_dead:
        try:
            await context.bot.send_message(group_id, text=f"밤 사이에 {final_dead} 님이 사망했습니다.")
        except:
            pass

async def resolve_day_vote(session_id, group_id, context: ContextTypes.DEFAULT_TYPE):
    """
    낮에 시민들이 /투표 <세션ID> <유저ID>
    """
    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT user_id,vote_target
    FROM mafia_players
    WHERE session_id=%s AND is_alive=TRUE AND vote_target<>0
    """,(session_id,))
    votes = cur.fetchall()
    if not votes:
        cur.close()
        conn.close()
        try:
            await context.bot.send_message(group_id, text="투표가 없었습니다.")
        except:
            pass
        if check_mafia_win_condition(session_id):
            await context.bot.send_message(group_id, text="게임 종료!")
            return True
        return False

    vote_count = {}
    for v in votes:
        tgt = v["vote_target"]
        vote_count[tgt] = vote_count.get(tgt,0)+1

    sorted_votes = sorted(vote_count.items(), key=lambda x: x[1], reverse=True)
    top_user, top_cnt = sorted_votes[0]

    # 처형
    cur.execute("""
    UPDATE mafia_players
    SET is_alive=FALSE, role='dead'
    WHERE session_id=%s AND user_id=%s
    """,(session_id, top_user))
    conn.commit()
    cur.close()
    conn.close()

    try:
        await context.bot.send_message(group_id, text=f"{top_user} 님이 {top_cnt}표로 처형됨.")
    except:
        pass

    if check_mafia_win_condition(session_id):
        await context.bot.send_message(group_id, text="게임 종료!")
        return True
    return False

async def mafia_kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인 채팅(1:1 DM)에서만 사용 가능합니다.")
        return
    args = context.args
    if len(args)<2:
        await update.message.reply_text("사용법: /살해 <세션ID> <유저ID>")
        return
    session_id, target_str = args[0], args[1]
    try:
        target_id = int(target_str)
    except:
        await update.message.reply_text("유효한 타겟 ID가 아닙니다.")
        return

    user_id = update.effective_user.id
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT role,is_alive FROM mafia_players
    WHERE session_id=%s AND user_id=%s
    """,(session_id,user_id))
    row = cur.fetchone()
    if not row or row["role"]!="Mafia" or not row["is_alive"]:
        await update.message.reply_text("마피아가 아니거나 이미 사망했습니다.")
        cur.close()
        conn.close()
        return

    cur.execute("""
    UPDATE mafia_players
    SET vote_target=%s
    WHERE session_id=%s AND user_id=%s
    """,(target_id, session_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"{target_id} 님을 살해 타겟으로 설정했습니다.")

async def mafia_doctor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인 채팅에서만.")
        return
    args = context.args
    if len(args)<2:
        await update.message.reply_text("사용법: /치료 <세션ID> <유저ID>")
        return
    session_id, tgt_str = args[0], args[1]
    try:
        tgt_id = int(tgt_str)
    except:
        await update.message.reply_text("유효한 유저 ID가 아님.")
        return

    user_id = update.effective_user.id
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT role,is_alive FROM mafia_players
    WHERE session_id=%s AND user_id=%s
    """,(session_id,user_id))
    row = cur.fetchone()
    if not row or row["role"]!="Doctor" or not row["is_alive"]:
        await update.message.reply_text("의사가 아니거나 사망 상태입니다.")
        cur.close()
        conn.close()
        return

    cur.execute("""
    UPDATE mafia_players
    SET heal_target=%s
    WHERE session_id=%s AND user_id=%s
    """,(tgt_id, session_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"{tgt_id} 님 치료 대상으로 설정.")

async def mafia_police_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type!="private":
        await update.message.reply_text("개인 채팅에서만.")
        return
    args = context.args
    if len(args)<2:
        await update.message.reply_text("사용법: /조사 <세션ID> <유저ID>")
        return
    session_id, tgt_str = args[0], args[1]
    try:
        tgt_id = int(tgt_str)
    except:
        await update.message.reply_text("유효한 ID가 아닙니다.")
        return

    user_id = update.effective_user.id
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT role,is_alive FROM mafia_players
    WHERE session_id=%s AND user_id=%s
    """,(session_id,user_id))
    row = cur.fetchone()
    if not row or row["role"]!="Police" or not row["is_alive"]:
        await update.message.reply_text("경찰이 아니거나 사망 상태.")
        cur.close()
        conn.close()
        return

    cur.execute("""
    UPDATE mafia_players
    SET investigate_target=%s
    WHERE session_id=%s AND user_id=%s
    """,(tgt_id, session_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"{tgt_id} 님 조사 대상으로 설정.")

async def mafia_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args)<2:
        await update.message.reply_text("사용법: /투표 <세션ID> <유저ID>")
        return
    session_id, tgt_str = args[0], args[1]
    try:
        tgt_id = int(tgt_str)
    except:
        await update.message.reply_text("유효한 ID가 아님.")
        return

    user_id = update.effective_user.id
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status FROM mafia_sessions WHERE session_id=%s",(session_id,))
    sess_row = cur.fetchone()
    if not sess_row or sess_row["status"]!="day":
        await update.message.reply_text("지금은 낮이 아닙니다.")
        cur.close()
        conn.close()
        return

    cur.execute("SELECT is_alive FROM mafia_players WHERE session_id=%s AND user_id=%s",(session_id,user_id))
    r = cur.fetchone()
    if not r or not r["is_alive"]:
        await update.message.reply_text("당신은 이미 죽었거나 참가하지 않았습니다.")
        cur.close()
        conn.close()
        return

    cur.execute("""
    UPDATE mafia_players
    SET vote_target=%s
    WHERE session_id=%s AND user_id=%s
    """,(tgt_id, session_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"{tgt_id} 님에게 투표 완료.")

########################################
# 7. RPG
########################################
async def rpg_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    uname = update.effective_user.username or ""
    ensure_user_in_db(user_id, uname)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(user_id,))
    row = cur.fetchone()
    if row:
        await update.message.reply_text("이미 캐릭터가 존재합니다.")
        cur.close()
        conn.close()
        return
    cur.execute("""
    INSERT INTO rpg_characters(user_id,job,level,exp,hp,max_hp,atk,gold,skill_points)
    VALUES(%s,%s,1,0,100,100,10,100,0)
    """,(user_id,"none"))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text("캐릭터 생성 완료! /rpg직업선택 로 직업을 골라보세요.")

async def rpg_set_job_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("전사", callback_data="rpg_job_warrior")],
        [InlineKeyboardButton("마법사", callback_data="rpg_job_mage")],
        [InlineKeyboardButton("도적", callback_data="rpg_job_thief")],
    ]
    await update.message.reply_text("직업을 선택하세요:", reply_markup=InlineKeyboardMarkup(keyboard))

async def rpg_job_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    await query.answer()

    if data.startswith("rpg_job_"):
        job = data.split("_")[2]
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(user_id,))
        row = cur.fetchone()
        if not row:
            await query.edit_message_text("먼저 /rpg생성 을 해주세요.")
            cur.close()
            conn.close()
            return
        if row["job"]!="none":
            await query.edit_message_text("이미 직업이 설정되어 있습니다.")
            cur.close()
            conn.close()
            return

        if job=="warrior":
            hp=120; atk=12
        elif job=="mage":
            hp=80; atk=15
        else:
            hp=100; atk=10

        cur.execute("""
        UPDATE rpg_characters
        SET job=%s, hp=%s, max_hp=%s, atk=%s
        WHERE user_id=%s
        """,(job,hp,hp,atk,user_id))
        conn.commit()
        cur.close()
        conn.close()

        await query.edit_message_text(f"{job} 직업 선택 완료!")

async def rpg_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(user_id,))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text("캐릭터가 없습니다. /rpg생성 먼저.")
        cur.close()
        conn.close()
        return
    job=row["job"]
    lv=row["level"]
    exp=row["exp"]
    hp=row["hp"]
    max_hp=row["max_hp"]
    atk=row["atk"]
    gold=row["gold"]
    sp=row["skill_points"]
    msg=(
        f"[캐릭터]\n"
        f"직업:{job}\n"
        f"Lv:{lv}, EXP:{exp}/{lv*100}\n"
        f"HP:{hp}/{max_hp}, ATK:{atk}\n"
        f"Gold:{gold}, 스킬포인트:{sp}"
    )
    await update.message.reply_text(msg)
    cur.close()
    conn.close()

async def rpg_dungeon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("쉬움", callback_data="rpg_dungeon_easy")],
        [InlineKeyboardButton("보통", callback_data="rpg_dungeon_normal")],
        [InlineKeyboardButton("어려움", callback_data="rpg_dungeon_hard")],
    ]
    await update.message.reply_text("던전 난이도 선택:", reply_markup=InlineKeyboardMarkup(kb))

async def rpg_dungeon_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    data=query.data
    user_id=query.from_user.id
    await query.answer()

    if data.startswith("rpg_dungeon_"):
        diff=data.split("_")[2]
        if diff=="easy":
            mhp=50; matk=5; rexp=20; rgold=30
        elif diff=="normal":
            mhp=100; matk=10; rexp=50; rgold=60
        else:
            mhp=200; matk=20; rexp=100; rgold=120

        conn=get_db_conn()
        cur=conn.cursor()
        cur.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(user_id,))
        row=cur.fetchone()
        if not row:
            await query.edit_message_text("캐릭터가 없습니다. /rpg생성 먼저.")
            cur.close()
            conn.close()
            return

        php=row["hp"]
        patk=row["atk"]
        plevel=row["level"]
        pexp=row["exp"]
        pmax=row["max_hp"]
        pgold=row["gold"]
        psp=row["skill_points"]

        while php>0 and mhp>0:
            mhp-=patk
            if mhp<=0: break
            php-=matk

        msg=""
        if php<=0:
            php=1
            msg="패배... HP=1로 회복.\n"
        else:
            msg="승리!\n"
            pexp+=rexp
            pgold+=rgold
            leveled_up=0
            while pexp>=(plevel*100):
                pexp-=(plevel*100)
                plevel+=1
                psp+=1
                pmax+=20
                php=pmax
                patk+=5
                leveled_up+=1
            if leveled_up>0:
                msg+=f"{leveled_up}번 레벨업! 레벨:{plevel}, 스킬포인트+{leveled_up}\n"
            msg+=f"EXP+{rexp}, GOLD+{rgold}\n"

        cur.execute("""
        UPDATE rpg_characters
        SET hp=%s,exp=%s,level=%s,gold=%s,skill_points=%s,atk=%s,max_hp=%s
        WHERE user_id=%s
        """,(php,pexp,plevel,pgold,psp,patk,pmax,user_id))
        conn.commit()
        cur.close()
        conn.close()

        await query.edit_message_text(msg)

async def rpg_shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn=get_db_conn()
    cur=conn.cursor()
    cur.execute("SELECT item_id,name,price,atk_bonus,hp_bonus FROM rpg_items ORDER BY price ASC")
    items=cur.fetchall()
    cur.close()
    conn.close()

    if not items:
        await update.message.reply_text("상점에 아이템이 없습니다.")
        return

    text="[상점 목록]\n"
    kb=[]
    for it in items:
        text += f"{it['item_id']}. {it['name']} (가격:{it['price']}, ATK+{it['atk_bonus']}, HP+{it['hp_bonus']})\n"
        kb.append([InlineKeyboardButton(f"{it['name']} 구매", callback_data=f"rpg_shop_buy_{it['item_id']}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def rpg_shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    data=query.data
    user_id=query.from_user.id
    await query.answer()
    if data.startswith("rpg_shop_buy_"):
        iid=data.split("_")[3]
        try:
            item_id=int(iid)
        except:
            await query.edit_message_text("아이템 ID 오류.")
            return

        conn=get_db_conn()
        cur=conn.cursor()
        cur.execute("SELECT gold FROM rpg_characters WHERE user_id=%s",(user_id,))
        row=cur.fetchone()
        if not row:
            await query.edit_message_text("캐릭터 없음.")
            cur.close()
            conn.close()
            return
        p_gold=row["gold"]

        cur.execute("SELECT * FROM rpg_items WHERE item_id=%s",(item_id,))
        irow=cur.fetchone()
        if not irow:
            await query.edit_message_text("해당 아이템 없음.")
            cur.close()
            conn.close()
            return

        price=irow["price"]
        if p_gold<price:
            await query.edit_message_text("골드 부족.")
            cur.close()
            conn.close()
            return

        new_gold=p_gold-price
        cur.execute("UPDATE rpg_characters SET gold=%s WHERE user_id=%s",(new_gold,user_id))
        cur.execute("""
        INSERT INTO rpg_inventory(user_id,item_id,quantity)
        VALUES(%s,%s,1)
        ON CONFLICT(user_id,item_id)
        DO UPDATE SET quantity=rpg_inventory.quantity+1
        """,(user_id,item_id))
        conn.commit()
        cur.close()
        conn.close()

        await query.edit_message_text(f"{irow['name']} 구매 완료! (-{price} gold)")

async def rpg_inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id
    conn=get_db_conn()
    cur=conn.cursor()
    cur.execute("SELECT * FROM rpg_characters WHERE user_id=%s",(user_id,))
    crow=cur.fetchone()
    if not crow:
        await update.message.reply_text("캐릭터가 없습니다.")
        cur.close()
        conn.close()
        return
    p_gold=crow["gold"]

    text=f"[인벤토리]\nGold:{p_gold}\n"
    cur.execute("""
    SELECT inv.item_id,inv.quantity,it.name,it.atk_bonus,it.hp_bonus
    FROM rpg_inventory inv
    JOIN rpg_items it ON it.item_id=inv.item_id
    WHERE inv.user_id=%s
    """,(user_id,))
    inv=cur.fetchall()
    cur.close()
    conn.close()

    if not inv:
        text+="(아이템 없음)"
    else:
        for i in inv:
            text+=f"{i['name']} x{i['quantity']} (ATK+{i['atk_bonus']}, HP+{i['hp_bonus']})\n"
    await update.message.reply_text(text)

async def rpg_skill_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id
    conn=get_db_conn()
    cur=conn.cursor()
    cur.execute("SELECT job,level,skill_points FROM rpg_characters WHERE user_id=%s",(user_id,))
    row=cur.fetchone()
    if not row:
        await update.message.reply_text("캐릭터가 없습니다.")
        cur.close()
        conn.close()
        return
    job=row["job"]
    lv=row["level"]
    sp=row["skill_points"]

    cur.execute("SELECT * FROM rpg_skills WHERE job=%s ORDER BY required_level ASC",(job,))
    skills=cur.fetchall()
    text=f"[{job} 스킬목록]\n스킬포인트:{sp}\n"
    for s in skills:
        text += (f"ID:{s['skill_id']}, {s['name']}, LvReq:{s['required_level']}, "
                 f"dmg:{s['damage']}, heal:{s['heal']}\n")
    cur.close()
    conn.close()
    await update.message.reply_text(text)

async def rpg_skill_learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("사용법: /스킬습득 <스킬ID>")
        return
    try:
        sid=int(args[0])
    except:
        await update.message.reply_text("유효하지 않은 스킬ID.")
        return

    user_id=update.effective_user.id
    conn=get_db_conn()
    cur=conn.cursor()
    cur.execute("SELECT job,level,skill_points FROM rpg_characters WHERE user_id=%s",(user_id,))
    crow=cur.fetchone()
    if not crow:
        await update.message.reply_text("캐릭터 없음.")
        cur.close()
        conn.close()
        return
    job=crow["job"]
    lv=crow["level"]
    sp=crow["skill_points"]

    cur.execute("SELECT * FROM rpg_skills WHERE skill_id=%s AND job=%s",(sid,job))
    srow=cur.fetchone()
    if not srow:
        await update.message.reply_text("없는 스킬이거나 직업 불일치.")
        cur.close()
        conn.close()
        return
    if lv<srow["required_level"]:
        await update.message.reply_text("레벨이 부족합니다.")
        cur.close()
        conn.close()
        return
    if sp<1:
        await update.message.reply_text("스킬포인트가 부족합니다.")
        cur.close()
        conn.close()
        return

    # 이미 배운 스킬?
    cur.execute("SELECT * FROM rpg_learned_skills WHERE user_id=%s AND skill_id=%s",(user_id,sid))
    lr=cur.fetchone()
    if lr:
        await update.message.reply_text("이미 습득한 스킬.")
        cur.close()
        conn.close()
        return

    cur.execute("INSERT INTO rpg_learned_skills(user_id,skill_id) VALUES(%s,%s)",(user_id,sid))
    cur.execute("UPDATE rpg_characters SET skill_points=skill_points-1 WHERE user_id=%s",(user_id,))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text("스킬 습득 완료!")

########################################
# 8. 투표(영문 명령어) & 콜백
########################################
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("사용법: /vote <주제>")
        return
    kb = [
        [InlineKeyboardButton("👍", callback_data=f"vote_yes|{topic}"),
         InlineKeyboardButton("👎", callback_data=f"vote_no|{topic}")]
    ]
    await update.message.reply_text(
        f"[투표]\n{topic}", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def vote_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    parts = data.split("|",1)
    if len(parts)<2:
        return
    vote_type, topic=parts
    user = query.from_user
    if vote_type=="vote_yes":
        await query.edit_message_text(f"[투표] {topic}\n\n{user.first_name}님이 👍 선택!")
    else:
        await query.edit_message_text(f"[투표] {topic}\n\n{user.first_name}님이 👎 선택!")

########################################
# 9. 인라인 메뉴 콜백
########################################
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data=="menu_games":
        kb = [
            [InlineKeyboardButton("마피아", callback_data="menu_mafia")],
            [InlineKeyboardButton("RPG", callback_data="menu_rpg")],
            [InlineKeyboardButton("뒤로", callback_data="menu_back")]
        ]
        await query.edit_message_text("게임 메뉴", reply_markup=InlineKeyboardMarkup(kb))

    elif data=="menu_group":
        kb = [
            [InlineKeyboardButton("공지(관리자)", callback_data="menu_group_announce")],
            [InlineKeyboardButton("투표/설문", callback_data="menu_group_vote")],
            [InlineKeyboardButton("뒤로", callback_data="menu_back")]
        ]
        await query.edit_message_text("그룹관리", reply_markup=InlineKeyboardMarkup(kb))

    elif data=="menu_subscribe":
        user_id=query.from_user.id
        sub=is_subscribed_db(user_id)
        txt="구독자 ✅" if sub else "비구독 ❌"
        toggle="구독해지" if sub else "구독하기"
        kb = [
            [InlineKeyboardButton(toggle, callback_data="menu_sub_toggle")],
            [InlineKeyboardButton("뒤로", callback_data="menu_back")]
        ]
        await query.edit_message_text(f"현재 상태:{txt}", reply_markup=InlineKeyboardMarkup(kb))

    elif data=="menu_ranking":
        txt = get_daily_ranking_text()
        await query.edit_message_text(txt)

    elif data=="menu_mafia":
        txt = """[마피아]
/마피아시작 (그룹)
/참가 <세션ID>
/마피아강제시작 <세션ID>
(마피아DM) /살해 <세션ID> <유저ID>
(의사DM)  /치료 <세션ID> <유저ID>
(경찰DM)  /조사 <세션ID> <유저ID>
(그룹)   /투표 <세션ID> <유저ID>
"""
        await query.edit_message_text(txt)

    elif data=="menu_rpg":
        txt = """[RPG]
/rpg생성
/rpg직업선택
/rpg상태
/던전
/상점
/스킬목록
/스킬습득 <스킬ID>
/인벤토리
"""
        await query.edit_message_text(txt)

    elif data=="menu_sub_toggle":
        user_id=query.from_user.id
        c=is_subscribed_db(user_id)
        set_subscribe(user_id, not c)
        msg="구독자 ✅" if not c else "비구독 ❌"
        await query.edit_message_text(f"이제 {msg} 가 되었습니다.")

    elif data=="menu_back":
        # 메인 메뉴 복귀
        await start_command(update, context)

    elif data=="menu_group_announce":
        await query.edit_message_text("공지: /announce <메시지> (관리자용)")

    elif data=="menu_group_vote":
        await query.edit_message_text("투표: /vote <주제>")

    else:
        await query.edit_message_text("알 수 없는 메뉴.")

########################################
# 10. 일반 텍스트 핸들러
########################################
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """명령어 아닌 일반 메시지 -> 불량단어/스팸 필터, 일일 카운트."""
    await filter_bad_words_and_spam_and_links(update, context)
    if update.message:
        increment_daily_chat_count(update.effective_user.id)

########################################
# 11. 스케줄러 (매일 0시 랭킹 리셋)
########################################
def schedule_jobs(app):
    scheduler=AsyncIOScheduler(timezone=KST)
    scheduler.add_job(reset_daily_chat_count, 'cron', hour=0, minute=0)
    scheduler.start()

########################################
# 12. main()
########################################
async def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    schedule_jobs(app)

    # -------------------------------
    # 1) 영문 명령어 -> CommandHandler
    # -------------------------------
    app.add_handler(CommandHandler("start", start_command))           # /start
    app.add_handler(CommandHandler("help", help_command))             # /help
    app.add_handler(CommandHandler("adminsecret", admin_secret_command))
    app.add_handler(CommandHandler("announce", announce_command))
    app.add_handler(CommandHandler("subscribe_toggle", subscribe_toggle_command))
    app.add_handler(CommandHandler("vote", vote_command))             # /vote

    # -------------------------------
    # 2) 한글 명령어 -> MessageHandler + Regex
    # -------------------------------
    # 예: /시작
    app.add_handler(MessageHandler(filters.Regex(r"^/시작(\s+.*)?$"), hangeul_start_command))
    # /도움말
    app.add_handler(MessageHandler(filters.Regex(r"^/도움말(\s+.*)?$"), hangeul_help_command))
    # /랭킹
    app.add_handler(MessageHandler(filters.Regex(r"^/랭킹(\s+.*)?$"), hangeul_ranking_command))

    # 마피아 (한글)
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아시작(\s+.*)?$"), hangeul_mafia_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/참가(\s+.*)?$"), hangeul_mafia_join_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/마피아강제시작(\s+.*)?$"), hangeul_mafia_force_start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/살해(\s+.*)?$"), hangeul_mafia_kill_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/치료(\s+.*)?$"), hangeul_mafia_doctor_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/조사(\s+.*)?$"), hangeul_mafia_police_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/투표(\s+.*)?$"), hangeul_mafia_vote_command))

    # RPG (한글)
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg생성(\s+.*)?$"), hangeul_rpg_create_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg직업선택(\s+.*)?$"), hangeul_rpg_set_job_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/rpg상태(\s+.*)?$"), hangeul_rpg_status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/던전(\s+.*)?$"), hangeul_rpg_dungeon_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/상점(\s+.*)?$"), hangeul_rpg_shop_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/인벤토리(\s+.*)?$"), hangeul_rpg_inventory_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/스킬목록(\s+.*)?$"), hangeul_rpg_skill_list_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/스킬습득(\s+.*)?$"), hangeul_rpg_skill_learn_command))

    # -------------------------------
    # 3) 콜백 핸들러(투표, RPG, 인라인 메뉴)
    # -------------------------------
    app.add_handler(CallbackQueryHandler(vote_callback_handler, pattern="^vote_(yes|no)\\|"))
    app.add_handler(CallbackQueryHandler(rpg_dungeon_callback, pattern="^rpg_dungeon_"))
    app.add_handler(CallbackQueryHandler(rpg_job_callback_handler, pattern="^rpg_job_"))
    app.add_handler(CallbackQueryHandler(rpg_shop_callback, pattern="^rpg_shop_buy_"))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^menu_.*"))

    # -------------------------------
    # 4) 그룹 환영/퇴장
    # -------------------------------
    app.add_handler(ChatMemberHandler(welcome_message, ChatMemberHandler.CHAT_MEMBER))

    # -------------------------------
    # 5) 일반 텍스트(명령어 제외)
    # -------------------------------
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    logger.info("봇 시작!")
    app.run_polling()


if __name__=="__main__":
    asyncio.run(main())