#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت لعبة الذئب لتطبيق تيليجرام (Werewolf Telegram Bot)
يعمل باستخدام مكتبة python-telegram-bot (الإصدار 20+) وقاعدة بيانات محلية SQLite3.
يدعم النشر المباشر على منصة Render (Web Service المجاني أو Background Worker).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set, Tuple

try:
    from telegram import (
        BotCommand,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeDefault,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
    )
    from telegram.constants import ChatType, ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:
    print(
        "تنبيه: مكتبة python-telegram-bot غير مثبتة.\n"
        "لتشغيل البوت نفذ الأمر:\n"
        'pip install "python-telegram-bot>=20.0"'
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
DB_PATH = os.getenv("WEREWOLF_DB_PATH", "werewolf_local.db")

JOIN_TIME_SECONDS = 90
NIGHT_TIME_SECONDS = 45
DAY_TIME_SECONDS = 55
LYNCH_TIME_SECONDS = 45
MIN_PLAYERS = 4
MAX_PLAYERS = 36


# =====================================================================
# 0. خادم فتح المنفذ لمنصة Render (Port Binding Server for Render Web Service)
# =====================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Werewolf Telegram Bot is Alive and Running!")

    def log_message(self, format: str, *args) -> None:
        return


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("✅ تم فتح المنفذ %s بنجاح لمنصة Render (0.0.0.0:%s)", port, port)


# =====================================================================
# 1. قاعدة البيانات المحلية (SQLite3)
# =====================================================================
class LocalDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        try:
            self.init_db()
        except sqlite3.OperationalError:
            self.db_path = os.path.join(tempfile.gettempdir(), "werewolf_local.db")
            self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    games_played INTEGER DEFAULT 0,
                    games_won INTEGER DEFAULT 0,
                    games_survived INTEGER DEFAULT 0,
                    team_wins INTEGER DEFAULT 0,
                    updated_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS nextgame_waitlist (
                    chat_id INTEGER,
                    user_id INTEGER,
                    full_name TEXT,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS game_history (
                    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    mode TEXT,
                    team_size INTEGER DEFAULT 0,
                    player_count INTEGER,
                    winner_members TEXT,
                    finished_at TEXT
                )
                """
            )
            conn.commit()

    def upsert_player(self, user_id: int, username: str, full_name: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO players (user_id, username, full_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username or "", full_name, now),
            )
            conn.commit()

    def record_result(self, user_id: int, won: bool, survived: bool) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE players
                SET games_played = games_played + 1,
                    games_won = games_won + ?,
                    games_survived = games_survived + ?,
                    team_wins = team_wins + ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    1 if won else 0,
                    1 if survived else 0,
                    1 if won else 0,
                    datetime.utcnow().isoformat(),
                    user_id,
                ),
            )
            conn.commit()

    def get_player_stats(self, user_id: int) -> Optional[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT * FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()

    def get_leaderboard(self, limit: int = 10) -> List[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute(
                """
                SELECT full_name, games_played, games_won, games_survived, team_wins
                FROM players
                ORDER BY games_won DESC, games_played DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def log_match(
        self,
        chat_id: int,
        mode: str,
        team_size: int,
        player_count: int,
        winner_members: str,
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO game_history (chat_id, mode, team_size, player_count, winner_members, finished_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    mode,
                    team_size,
                    player_count,
                    winner_members,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()

    def add_nextgame(self, chat_id: int, user_id: int, full_name: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO nextgame_waitlist (chat_id, user_id, full_name) VALUES (?, ?, ?)",
                (chat_id, user_id, full_name),
            )
            conn.commit()

    def pop_nextgame_list(self, chat_id: int) -> List[sqlite3.Row]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT user_id, full_name FROM nextgame_waitlist WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
            conn.execute("DELETE FROM nextgame_waitlist WHERE chat_id = ?", (chat_id,))
            conn.commit()
            return rows


# =====================================================================
# 2. تعريف الأدوار الـ 17 المعتمدة حصرياً (نصوص مختصرة مطابقة للأصلي)
# =====================================================================
@dataclass(frozen=True)
class RoleInfo:
    key: str
    name_ar: str
    emoji: str
    faction: str  # "village", "wolves", "neutral"
    description: str


ROLES: Dict[str, RoleInfo] = {
    "werewolf": RoleInfo(
        "werewolf",
        "الذئب",
        "🐺",
        "wolves",
        "أنت الذئب 🐺! استيقظ كل ليلة لافتراس ضحية.",
    ),
    "alphawolf": RoleInfo(
        "alphawolf",
        "ذئب ألفا",
        "⚡🐺",
        "wolves",
        "أنت ذئب ألفا ⚡🐺! هجومك الليلي قد يحوّل الضحية إلى ذئب (25%).",
    ),
    "wolfcub": RoleInfo(
        "wolfcub",
        "الجرو المدلل",
        "🐶",
        "wolves",
        "أنت الجرو المدلل 🐶! إذا متّ، تفترس الذئاب ضحيتين في الليلة التالية.",
    ),
    "lycan": RoleInfo(
        "lycan",
        "المستذئب المخادع",
        "🌕🐺",
        "wolves",
        "أنت المستذئب المخادع 🌕🐺! ذئب، لكن العرّاف يراك كأنك العرّاف.",
    ),
    "tanner": RoleInfo(
        "tanner",
        "الدباغ",
        "👺",
        "neutral",
        "أنت الدباغ 👺! هدفك أن تعدمك القرية نهاراً.",
    ),
    "hunter": RoleInfo(
        "hunter",
        "الصياد",
        "🎯",
        "village",
        "أنت الصياد 🎯! عند موتك تطلق رصاصتك الأخيرة على أحد الأحياء.",
    ),
    "gunner": RoleInfo(
        "gunner",
        "القناص",
        "🔫",
        "village",
        "أنت القناص 🔫! تملك رصاصتين لقنص أي لاعب نهاراً.",
    ),
    "mayor": RoleInfo(
        "mayor",
        "العمدة",
        "🎖️",
        "village",
        "أنت العمدة 🎖️! عند إعلان هويتك نهاراً يُحتسب صوتك بصوتين.",
    ),
    "piper": RoleInfo(
        "piper",
        "العازف",
        "🪈",
        "neutral",
        "أنت العازف 🪈! اسحر لاعبين كل ليلة لتفوز عند سحر الجميع.",
    ),
    "pacifist": RoleInfo(
        "pacifist",
        "المسالم",
        "☮️",
        "village",
        "أنت المسالم ☮️! يمكنك نهاراً منع الإعدام لمرة واحدة.",
    ),
    "cupid": RoleInfo(
        "cupid",
        "ملاك الحب",
        "💘",
        "village",
        "أنت ملاك الحب 💘! اربط لاعبين بالحب في الليلة الأولى، إن مات أحدهما مات الآخر.",
    ),
    "guardian": RoleInfo(
        "guardian",
        "الملاك الحارس",
        "👼",
        "village",
        "أنت الملاك الحارس 👼! احمِ لاعباً كل ليلة من الذئاب والسفاح.",
    ),
    "chemist": RoleInfo(
        "chemist",
        "الكيميائي",
        "🧪",
        "village",
        "أنت الكيميائي 🧪! زُر لاعباً ليلاً ليشرب أحدكما السم (50%).",
    ),
    "detective": RoleInfo(
        "detective",
        "المحقق",
        "🕵️",
        "village",
        "أنت المحقق 🕵️! اكشف دور لاعب نهاراً (احتمال 40% أن تكشفك الذئاب).",
    ),
    "seer": RoleInfo(
        "seer",
        "العراف",
        "👳",
        "village",
        "أنت العراف 👳! اكشف الدور الحقيقي للاعب كل ليلة.",
    ),
    "sorcerer": RoleInfo(
        "sorcerer",
        "الساحر",
        "🔮",
        "wolves",
        "أنت الساحر 🔮! ابحث ليلاً عن العرّاف أو الذئاب.",
    ),
    "blacksmith": RoleInfo(
        "blacksmith",
        "الحداد",
        "⚒️",
        "village",
        "أنت الحداد ⚒️! انثر الفضة نهاراً لمنع هجوم الذئاب الليلة التالية.",
    ),
    "serialkiller": RoleInfo(
        "serialkiller",
        "السفاح",
        "🔪",
        "neutral",
        "أنت السفاح 🔪! اقتل لاعباً كل ليلة.",
    ),
    "prince": RoleInfo(
        "prince",
        "الأمير",
        "👑",
        "village",
        "أنت الأمير 👑! تنجو تلقائياً من الإعدام في المرة الأولى.",
    ),
    "troublemaker": RoleInfo(
        "troublemaker",
        "المشاغب",
        "😈",
        "village",
        "أنت المشاغب 😈! أشعل الفوضى نهاراً ليتم إعدام لاعبين اليوم.",
    ),
}

WOLF_PACK_KEYS = {"werewolf", "alphawolf", "wolfcub", "lycan"}


# =====================================================================
# 3. حالة اللاعب والجولة (Player & Session Models)
# =====================================================================
@dataclass
class GamePlayer:
    user_id: int
    username: str
    full_name: str
    role_key: str = "seer"
    alive: bool = True
    team_id: int = 0
    night_targets: List[int] = field(default_factory=list)
    lynch_vote: Optional[int] = None
    bullets: int = 0
    mayor_revealed: bool = False
    prince_saved_once: bool = False
    pacifist_used: bool = False
    blacksmith_used: bool = False
    troublemaker_used: bool = False
    investigated_today: bool = False
    lover_id: Optional[int] = None
    charmed: bool = False

    @property
    def role(self) -> RoleInfo:
        return ROLES[self.role_key]


@dataclass
class GameSession:
    chat_id: int
    chat_title: str
    creator_id: int
    mode: str = "normal"
    team_size: int = 1  # 1 = لعب مفرد طبيعي، 2..6 = طور الفرق
    phase: str = "waiting_mode"
    day_count: int = 0
    join_deadline: float = 0.0
    players: Dict[int, GamePlayer] = field(default_factory=dict)
    teams: Dict[int, List[int]] = field(default_factory=dict)
    wolves_double_kill_tonight: bool = False
    silver_spread_tonight: bool = False
    silver_active_this_night: bool = False
    peace_day_active: bool = False
    double_lynch_today: bool = False
    lynched_tanner_id: Optional[int] = None
    task: Optional[asyncio.Task] = None

    @property
    def is_teams_mode(self) -> bool:
        return self.team_size >= 2

    def alive_players(self) -> List[GamePlayer]:
        return [p for p in self.players.values() if p.alive]

    def alive_wolves(self) -> List[GamePlayer]:
        return [
            p
            for p in self.players.values()
            if p.alive and p.role_key in WOLF_PACK_KEYS
        ]

    def alive_sk(self) -> List[GamePlayer]:
        return [
            p
            for p in self.players.values()
            if p.alive and p.role_key == "serialkiller"
        ]

    def alive_pipers(self) -> List[GamePlayer]:
        return [
            p for p in self.players.values() if p.alive and p.role_key == "piper"
        ]

    def alive_team_ids(self) -> Set[int]:
        return {p.team_id for p in self.alive_players() if p.team_id > 0}


ACTIVE_GAMES: Dict[int, GameSession] = {}
DB = LocalDatabase()


# =====================================================================
# 4. عرض اللاعبين والفرق بأسلوب مختصر مطابق للبوت الأصلي (بدون أسماء للفرق)
# =====================================================================
def format_players_or_teams(session: GameSession, show_roles: bool = False) -> str:
    alive_count = len(session.alive_players())
    total_count = len(session.players)

    if session.is_teams_mode and session.teams:
        lines = [f"👥 **اللاعبون ({alive_count}/{total_count}):**"]
        for idx, (_, member_ids) in enumerate(session.teams.items(), start=1):
            parts = []
            for uid in member_ids:
                p = session.players[uid]
                icon = "🙂" if p.alive else "💀"
                r_txt = (
                    f" ({p.role.emoji} {p.role.name_ar})"
                    if (show_roles or not p.alive)
                    else ""
                )
                parts.append(f"{icon} {p.full_name}{r_txt}")
            lines.append(f"{idx}. " + " ، ".join(parts))
        return "\n".join(lines)

    lines = [f"👥 **اللاعبون ({alive_count}/{total_count}):**"]
    for idx, p in enumerate(session.players.values(), start=1):
        icon = "🙂" if p.alive else "💀"
        r_txt = (
            f" - {p.role.emoji} {p.role.name_ar}"
            if (show_roles or not p.alive)
            else ""
        )
        lines.append(f"{idx}. {icon} {p.full_name}{r_txt}")
    return "\n".join(lines)


def are_enemies(session: GameSession, p1: GamePlayer, p2: GamePlayer) -> bool:
    if p1.user_id == p2.user_id:
        return False
    if session.is_teams_mode:
        return p1.team_id != p2.team_id
    return True


# =====================================================================
# 5. توزيع الأدوار والفرق العشوائية
# =====================================================================
def assign_roles_and_teams(session: GameSession) -> None:
    player_list = list(session.players.values())
    n = len(player_list)

    if session.mode == "chaos":
        pool = list(ROLES.keys())
        chosen_roles = [random.choice(pool) for _ in range(n)]
        if not any(r in WOLF_PACK_KEYS or r == "serialkiller" for r in chosen_roles):
            chosen_roles[0] = random.choice(list(WOLF_PACK_KEYS))
    else:
        chosen_roles: List[str] = []
        wolf_count = max(1, n // 4)
        wolf_types = ["werewolf", "alphawolf", "wolfcub", "lycan"]
        for i in range(wolf_count):
            chosen_roles.append(wolf_types[i % len(wolf_types)])

        village_pool = [
            "seer",
            "guardian",
            "detective",
            "gunner",
            "hunter",
            "mayor",
            "prince",
            "blacksmith",
            "pacifist",
            "chemist",
            "troublemaker",
            "cupid",
        ]
        random.shuffle(village_pool)
        village_pool.remove("seer")
        village_pool.remove("guardian")
        ordered_village = ["seer", "guardian"] + village_pool

        if n >= 6:
            chosen_roles.append(random.choice(["tanner", "piper", "sorcerer"]))
        if n >= 8:
            chosen_roles.append("serialkiller")
        if n >= 10:
            chosen_roles.append(random.choice(["piper", "tanner"]))

        idx = 0
        while len(chosen_roles) < n:
            chosen_roles.append(ordered_village[idx % len(ordered_village)])
            idx += 1

        chosen_roles = chosen_roles[:n]

    random.shuffle(chosen_roles)
    random.shuffle(player_list)

    for player, r_key in zip(player_list, chosen_roles):
        player.role_key = r_key
        if r_key == "gunner":
            player.bullets = 2

    session.teams.clear()
    if session.is_teams_mode:
        tsize = session.team_size if session.team_size in (2, 3, 4, 5, 6) else 2
        session.team_size = tsize
        shuffled = list(session.players.values())
        random.shuffle(shuffled)
        team_idx = 0
        for i in range(0, len(shuffled), tsize):
            team_id = team_idx + 1
            members = shuffled[i : i + tsize]
            session.teams[team_id] = [m.user_id for m in members]
            for m in members:
                m.team_id = team_id
            team_idx += 1
    else:
        for idx, p in enumerate(session.players.values(), start=1):
            p.team_id = idx
            session.teams[idx] = [p.user_id]


# =====================================================================
# 6. إدارة موت اللاعب (مختصر ومطابق للأصلي)
# =====================================================================
async def handle_player_death(
    context: ContextTypes.DEFAULT_TYPE,
    session: GameSession,
    victim: GamePlayer,
    cause_text: str,
    events_list: Optional[List[str]] = None,
) -> None:
    if not victim.alive:
        return
    victim.alive = False

    msg = f"{cause_text} كان دوره **{victim.role.emoji} {victim.role.name_ar}**."
    if events_list is not None:
        events_list.append(msg)
    else:
        await context.bot.send_message(
            chat_id=session.chat_id, text=msg, parse_mode=ParseMode.MARKDOWN
        )

    if victim.role_key == "wolfcub":
        session.wolves_double_kill_tonight = True
        cub_msg = "🐶 مات **الجرو المدلل**! ستفترس الذئاب ضحيتين الليلة القادمة!"
        if events_list is not None:
            events_list.append(cub_msg)
        else:
            await context.bot.send_message(
                chat_id=session.chat_id, text=cub_msg, parse_mode=ParseMode.MARKDOWN
            )

    if victim.role_key == "hunter":
        enemy_candidates = [
            p for p in session.alive_players() if are_enemies(session, victim, p)
        ]
        if not enemy_candidates:
            enemy_candidates = [
                p for p in session.alive_players() if p.user_id != victim.user_id
            ]
        if enemy_candidates:
            shot = random.choice(enemy_candidates)
            hunter_cause = f"🎯 أطلق الصياد **{victim.full_name}** رصاصته الأخيرة فقتل **{shot.full_name}**!"
            await handle_player_death(
                context, session, shot, hunter_cause, events_list
            )

    if victim.lover_id:
        partner = session.players.get(victim.lover_id)
        if partner and partner.alive:
            lover_cause = f"💔 مات **{partner.full_name}** حزناً على حبيبه **{victim.full_name}**!"
            await handle_player_death(
                context, session, partner, lover_cause, events_list
            )


# =====================================================================
# 7. الحلقة الرئيسية للعبة (Game Loop)
# =====================================================================
async def run_game_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    session = ACTIVE_GAMES.get(chat_id)
    if not session:
        return

    join_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🐺 انضمام",
                    callback_data=f"join_game:{chat_id}",
                )
            ]
        ]
    )

    while session.phase == "joining":
        remaining = int(session.join_deadline - asyncio.get_event_loop().time())
        if remaining <= 0:
            break
        if remaining in (60, 30, 10):
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ تبقى **{remaining}** ثانية للانضمام! ({len(session.players)} لاعب)",
                reply_markup=join_kb,
                parse_mode=ParseMode.MARKDOWN,
            )
        await asyncio.sleep(1)

    if len(session.players) < MIN_PLAYERS:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ لم يكتمل العدد الكافي ({MIN_PLAYERS} لاعبين على الأقل).",
        )
        ACTIVE_GAMES.pop(chat_id, None)
        return

    assign_roles_and_teams(session)

    if session.is_teams_mode and len(session.teams) < 2:
        session.team_size = max(2, len(session.players) // 2)
        assign_roles_and_teams(session)

    # إرسال الأدوار في الخاص + كشف أدوار أعضاء الفريق الواحد + تفعيل محادثة البوت
    for p in session.players.values():
        role_lines = [f"{p.role.description}"]

        if session.is_teams_mode:
            teammate_ids = [
                uid for uid in session.teams.get(p.team_id, []) if uid != p.user_id
            ]
            if teammate_ids:
                role_lines.append("\n👥 **أعضاء فريقك وأدوارهم:**")
                for tid in teammate_ids:
                    tm = session.players[tid]
                    role_lines.append(
                        f"• **{tm.full_name}** — {tm.role.emoji} **{tm.role.name_ar}**"
                    )
                role_lines.append(
                    "\n💬 *اكتب أي رسالة هنا في خاص البوت ليتم إرسالها لأعضاء فريقك فوراً.*"
                )
        else:
            # في اللعب المفرد: الذئاب تعرف بعضها وتتحادث في الخاص مثل البوت الأصلي
            if p.role_key in WOLF_PACK_KEYS:
                other_wolves = [
                    w
                    for w in session.players.values()
                    if w.role_key in WOLF_PACK_KEYS and w.user_id != p.user_id
                ]
                if other_wolves:
                    role_lines.append("\n🐺 **الذئاب معك:**")
                    for w in other_wolves:
                        role_lines.append(
                            f"• **{w.full_name}** ({w.role.emoji} {w.role.name_ar})"
                        )
                    role_lines.append(
                        "\n💬 *اكتب أي رسالة هنا للتحدث مع الذئاب.*"
                    )

        try:
            await context.bot.send_message(
                chat_id=p.user_id,
                text="\n".join(role_lines),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.warning("تعذر إرسال الدور بالخاص للاعب %s", p.full_name)

    await context.bot.send_message(
        chat_id=chat_id,
        text="🐺 بدأت اللعبة! تفقدوا الخاص لمعرفة أدواركم.\n\n"
        + format_players_or_teams(session, show_roles=False),
        parse_mode=ParseMode.MARKDOWN,
    )

    while session.phase != "ended":
        session.day_count += 1
        session.phase = "night"
        session.silver_active_this_night = session.silver_spread_tonight
        session.silver_spread_tonight = False

        for p in session.alive_players():
            p.night_targets.clear()
            p.investigated_today = False

        night_note = (
            "\n⚒️ الفضة المنتشرة تمنع الذئاب من الهجوم الليلة!"
            if session.silver_active_this_night
            else ""
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🌙 حلّ الليل ({session.day_count})... نيام جميعاً ({NIGHT_TIME_SECONDS} ثانية).{night_note}",
            parse_mode=ParseMode.MARKDOWN,
        )

        await send_night_menus(context, session)
        await asyncio.sleep(NIGHT_TIME_SECONDS)

        if chat_id not in ACTIVE_GAMES:
            return

        night_events = await resolve_night_actions(context, session)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"☀️ حلّ الصباح (اليوم {session.day_count})!\n"
                + (
                    "\n".join(night_events)
                    if night_events
                    else "✨ مرّت الليلة بسلام ولم يمت أحد."
                )
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

        if await check_and_handle_winner(context, session):
            return

        await context.bot.send_message(
            chat_id=chat_id,
            text=format_players_or_teams(session, show_roles=False),
            parse_mode=ParseMode.MARKDOWN,
        )

        session.phase = "day"
        session.peace_day_active = False
        session.double_lynch_today = False

        await send_day_menus(context, session)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🗣️ وقت النقاش ({DAY_TIME_SECONDS} ثانية)...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await asyncio.sleep(DAY_TIME_SECONDS)

        if chat_id not in ACTIVE_GAMES:
            return
        if await check_and_handle_winner(context, session):
            return

        if session.peace_day_active:
            await context.bot.send_message(
                chat_id=chat_id,
                text="☮️ بفضل **المسالم**، لن يُعدم أحد اليوم!",
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        session.phase = "lynch"
        for p in session.alive_players():
            p.lynch_vote = None

        await send_lynch_menus(context, session)
        lynch_note = (
            "\n😈 سيتم إعدام شخصين اليوم!"
            if session.double_lynch_today
            else ""
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚖️ وقت التصويت للإعدام ({LYNCH_TIME_SECONDS} ثانية)! تفقدوا الخاص.{lynch_note}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await asyncio.sleep(LYNCH_TIME_SECONDS)

        if chat_id not in ACTIVE_GAMES:
            return

        await resolve_lynch_votes(context, session)

        if await check_and_handle_winner(context, session):
            return


# =====================================================================
# 8. محادثة الفريق في خاص البوت (Team Private Chat Relay)
# =====================================================================
def find_active_player_session(
    user_id: int,
) -> Optional[Tuple[GameSession, GamePlayer]]:
    for session in ACTIVE_GAMES.values():
        if session.phase in ("night", "day", "lynch") and user_id in session.players:
            return session, session.players[user_id]
    return None


async def handle_private_team_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """إعادة توجيه رسائل اللاعب في خاص البوت إلى أعضاء فريقه (أو الذئاب/العشاق)."""
    msg = update.message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return

    found = find_active_player_session(user.id)
    if not found:
        return

    session, sender = found
    text_content = msg.text.strip()
    if not text_content:
        return

    recipients: Dict[int, GamePlayer] = {}

    if session.is_teams_mode:
        # في طور الفرق: يتحدث أعضاء الفريق الواحد (الأحياء والأموات في نفس الفريق) مع بعضهم طوال اللعبة
        for uid in session.teams.get(sender.team_id, []):
            if uid != sender.user_id and uid in session.players:
                recipients[uid] = session.players[uid]
    else:
        # في اللعب المفرد الطبيعي: الذئاب الأحياء يتحادثون مع بعضهم
        if sender.alive and sender.role_key in WOLF_PACK_KEYS:
            for w in session.alive_wolves():
                if w.user_id != sender.user_id:
                    recipients[w.user_id] = w

    # إذا كان اللاعب مرتبطاً بسهم الحب، تصل رسالته أيضاً لحبيبه
    if sender.alive and sender.lover_id and sender.lover_id in session.players:
        lover = session.players[sender.lover_id]
        if lover.alive and lover.user_id != sender.user_id:
            recipients[lover.user_id] = lover

    if not recipients:
        return

    state_tag = "" if sender.alive else " (💀)"
    out_msg = (
        f"💬 **{sender.full_name}** ({sender.role.emoji} {sender.role.name_ar}){state_tag}:\n"
        f"{text_content}"
    )

    for r_uid in recipients:
        try:
            await context.bot.send_message(
                chat_id=r_uid, text=out_msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass


# =====================================================================
# 9. إرسال القوائم التفاعلية ومعالجة الليل والنهار والتصويت
# =====================================================================
def build_target_keyboard(
    action_prefix: str, chat_id: int, candidates: List[GamePlayer]
) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=p.full_name,
                callback_data=f"{action_prefix}:{chat_id}:{p.user_id}",
            )
        ]
        for p in candidates
    ]
    return InlineKeyboardMarkup(buttons)


async def send_night_menus(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> None:
    alive = session.alive_players()
    for p in alive:
        try:
            if p.role_key in WOLF_PACK_KEYS and not session.silver_active_this_night:
                targets = [
                    t
                    for t in alive
                    if are_enemies(session, p, t)
                    and (session.is_teams_mode or t.role_key not in WOLF_PACK_KEYS)
                ]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🐺 من تريد افتراسه الليلة؟",
                        reply_markup=build_target_keyboard(
                            "night_wolf", session.chat_id, targets
                        ),
                    )
            elif p.role_key == "seer":
                targets = [t for t in alive if t.user_id != p.user_id]
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="👳 من تريد كشف دوره؟",
                    reply_markup=build_target_keyboard(
                        "night_seer", session.chat_id, targets
                    ),
                )
            elif p.role_key == "sorcerer":
                targets = [t for t in alive if t.user_id != p.user_id]
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="🔮 من تريد فحصه الليلة؟",
                    reply_markup=build_target_keyboard(
                        "night_sorcerer", session.chat_id, targets
                    ),
                )
            elif p.role_key == "guardian":
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="👼 من تريد حمايته الليلة؟",
                    reply_markup=build_target_keyboard(
                        "night_ga", session.chat_id, alive
                    ),
                )
            elif p.role_key == "serialkiller":
                targets = [t for t in alive if are_enemies(session, p, t)]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🔪 من تريد قتله الليلة؟",
                        reply_markup=build_target_keyboard(
                            "night_sk", session.chat_id, targets
                        ),
                    )
            elif p.role_key == "chemist":
                targets = [t for t in alive if are_enemies(session, p, t)]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🧪 من تريد زيارته بالجرعتين؟",
                        reply_markup=build_target_keyboard(
                            "night_chemist", session.chat_id, targets
                        ),
                    )
            elif p.role_key == "piper":
                targets = [
                    t
                    for t in alive
                    if are_enemies(session, p, t) and not t.charmed
                ]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🪈 اختر لاعبين لسحرهما:",
                        reply_markup=build_target_keyboard(
                            "night_piper", session.chat_id, targets
                        ),
                    )
            elif p.role_key == "cupid" and session.day_count == 1:
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="💘 اختر لاعبين للربط بينهما بالحب:",
                    reply_markup=build_target_keyboard(
                        "night_cupid", session.chat_id, alive
                    ),
                )
        except Exception:
            pass


async def resolve_night_actions(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> List[str]:
    events: List[str] = []
    protected_ids: Set[int] = set()

    if session.day_count == 1:
        for p in session.alive_players():
            if p.role_key == "cupid" and len(p.night_targets) >= 2:
                u1, u2 = p.night_targets[0], p.night_targets[1]
                if u1 in session.players and u2 in session.players and u1 != u2:
                    p1, p2 = session.players[u1], session.players[u2]
                    p1.lover_id = u2
                    p2.lover_id = u1
                    for lover, other in ((p1, p2), (p2, p1)):
                        try:
                            await context.bot.send_message(
                                chat_id=lover.user_id,
                                text=f"💘 ضربك سهم الحب مع **{other.full_name}** ({other.role.emoji} {other.role.name_ar})!",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except Exception:
                            pass

    for p in session.alive_players():
        if p.role_key == "guardian" and p.night_targets:
            protected_ids.add(p.night_targets[0])

    for p in session.alive_players():
        if p.role_key == "seer" and p.night_targets:
            target = session.players.get(p.night_targets[0])
            if target:
                shown_role = (
                    ROLES["seer"] if target.role_key == "lycan" else target.role
                )
                try:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text=f"👳 رأيت أن **{target.full_name}** هو **{shown_role.emoji} {shown_role.name_ar}**!",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    for p in session.alive_players():
        if p.role_key == "sorcerer" and p.night_targets:
            target = session.players.get(p.night_targets[0])
            if target:
                if target.role_key == "seer":
                    res = "👳 إنه **العرّاف**!"
                elif target.role_key in WOLF_PACK_KEYS:
                    res = f"🐺 إنه **{target.role.name_ar}**!"
                else:
                    res = "ℹ️ ليس العرّاف ولا من الذئاب."
                try:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text=f"🔮 فحص **{target.full_name}**: {res}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    for p in session.alive_players():
        if p.role_key == "piper" and p.night_targets:
            charmed_names = []
            for tid in p.night_targets[:2]:
                target = session.players.get(tid)
                if target and target.alive:
                    target.charmed = True
                    charmed_names.append(target.full_name)
            if charmed_names:
                events.append(f"🪈 سحر العازف: {', '.join(charmed_names)}!")
            enemies = [
                e for e in session.alive_players() if are_enemies(session, p, e)
            ]
            if enemies and all(e.charmed for e in enemies):
                events.append(
                    f"🪈 سحر العازف **{p.full_name}** الجميع وقضى عليهم!"
                )
                for e in list(enemies):
                    await handle_player_death(
                        context,
                        session,
                        e,
                        f"🪈 مات **{e.full_name}** بسحر العازف!",
                        events,
                    )

    for p in session.alive_players():
        if p.role_key == "chemist" and p.night_targets and p.alive:
            target = session.players.get(p.night_targets[0])
            if target and target.alive:
                if random.random() < 0.5:
                    await handle_player_death(
                        context,
                        session,
                        target,
                        f"🧪 سمّم الكيميائي **{target.full_name}** فمات!",
                        events,
                    )
                else:
                    await handle_player_death(
                        context,
                        session,
                        p,
                        f"🧪 شرب الكيميائي **{p.full_name}** سمه بنفسه ومات!",
                        events,
                    )

    if not session.silver_active_this_night:
        wolf_targets: List[int] = []
        for w in session.alive_wolves():
            wolf_targets.extend(w.night_targets)

        max_kills = 2 if session.wolves_double_kill_tonight else 1
        session.wolves_double_kill_tonight = False

        unique_voted = list(dict.fromkeys(wolf_targets))[:max_kills]
        has_alpha = any(w.role_key == "alphawolf" for w in session.alive_wolves())

        for victim_id in unique_voted:
            victim = session.players.get(victim_id)
            if not victim or not victim.alive:
                continue
            if victim_id in protected_ids:
                events.append(f"👼 أنقذ الملاك الحارس **{victim.full_name}** من الذئاب!")
            elif has_alpha and random.random() < 0.25:
                victim.role_key = "werewolf"
                try:
                    await context.bot.send_message(
                        chat_id=victim.user_id,
                        text="⚡🐺 عضّك **ذئب ألفا** فتحولت إلى **ذئب**!",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
                events.append("🐾 عضّ ذئب ألفا أحد اللاعبين وحوّله إلى ذئب!")
            else:
                await handle_player_death(
                    context,
                    session,
                    victim,
                    f"🐺 افترست الذئاب **{victim.full_name}**!",
                    events,
                )

    for sk in session.alive_sk():
        if sk.night_targets:
            target = session.players.get(sk.night_targets[0])
            if target and target.alive:
                if target.user_id in protected_ids:
                    events.append(f"👼 حمى الملاك الحارس **{target.full_name}** من السفاح!")
                else:
                    await handle_player_death(
                        context,
                        session,
                        target,
                        f"🔪 قُتل **{target.full_name}** على يد السفاح!",
                        events,
                    )

    return events


async def send_day_menus(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> None:
    alive = session.alive_players()
    for p in alive:
        try:
            if p.role_key == "gunner" and p.bullets > 0:
                targets = [t for t in alive if are_enemies(session, p, t)]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text=f"🔫 من تريد قنصه؟ (الرصاص المتبقي: {p.bullets})",
                        reply_markup=build_target_keyboard(
                            "day_gunner", session.chat_id, targets
                        ),
                    )
            elif p.role_key == "detective":
                targets = [t for t in alive if t.user_id != p.user_id]
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="🕵️ من تريد التحقيق حوله؟",
                    reply_markup=build_target_keyboard(
                        "day_detective", session.chat_id, targets
                    ),
                )
            elif p.role_key == "mayor" and not p.mayor_revealed:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="🎖️ إعلان العمدة",
                                callback_data=f"day_mayor:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="🎖️ هل تريد الكشف عن نفسك كعمدة؟",
                    reply_markup=kb,
                )
            elif p.role_key == "pacifist" and not p.pacifist_used:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="☮️ منع الإعدام اليوم",
                                callback_data=f"day_pacifist:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="☮️ هل تريد منع الإعدام اليوم؟",
                    reply_markup=kb,
                )
            elif p.role_key == "blacksmith" and not p.blacksmith_used:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="⚒️ نثر الفضة",
                                callback_data=f"day_blacksmith:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="⚒️ هل تريد نثر الفضة الليلة؟",
                    reply_markup=kb,
                )
            elif p.role_key == "troublemaker" and not p.troublemaker_used:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="😈 إثارة الشغب (إعدام شخصين)",
                                callback_data=f"day_troublemaker:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="😈 هل تريد إثارة الشغب اليوم؟",
                    reply_markup=kb,
                )
        except Exception:
            pass


async def send_lynch_menus(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> None:
    alive = session.alive_players()
    for p in alive:
        targets = [t for t in alive if t.user_id != p.user_id]
        try:
            await context.bot.send_message(
                chat_id=p.user_id,
                text="⚖️ من تريد إعدامه؟",
                reply_markup=build_target_keyboard(
                    "lynch_vote", session.chat_id, targets
                ),
            )
        except Exception:
            pass


async def resolve_lynch_votes(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> None:
    tally: Dict[int, int] = {}
    for p in session.alive_players():
        if p.lynch_vote:
            weight = 2 if (p.role_key == "mayor" and p.mayor_revealed) else 1
            tally[p.lynch_vote] = tally.get(p.lynch_vote, 0) + weight

    if not tally:
        await context.bot.send_message(
            chat_id=session.chat_id,
            text="⚖️ لم يصوّت أحد، لم يُعدم أحد اليوم.",
        )
        return

    sorted_candidates = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    lynch_limit = 2 if session.double_lynch_today else 1

    if len(sorted_candidates) >= 2 and lynch_limit == 1:
        if sorted_candidates[0][1] == sorted_candidates[1][1]:
            await context.bot.send_message(
                chat_id=session.chat_id,
                text="⚖️ تعادلت الأصوات! لم يُعدم أحد.",
            )
            return

    for uid, vcount in sorted_candidates[:lynch_limit]:
        target = session.players.get(uid)
        if not target or not target.alive:
            continue

        if target.role_key == "prince" and not target.prince_saved_once:
            target.prince_saved_once = True
            await context.bot.send_message(
                chat_id=session.chat_id,
                text=f"👑 كشف **{target.full_name}** أنه **الأمير**، فأُلغي إعدامه!",
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        await handle_player_death(
            context,
            session,
            target,
            f"🔥 أعدمت القرية **{target.full_name}** ({vcount} أصوات)!",
        )

        if target.role_key == "tanner":
            if session.is_teams_mode:
                enemies = [
                    e for e in session.alive_players() if e.team_id != target.team_id
                ]
                if enemies:
                    victim = random.choice(enemies)
                    await handle_player_death(
                        context,
                        session,
                        victim,
                        f"👺 أخذ الدباغ **{target.full_name}** معه **{victim.full_name}**!",
                    )
            else:
                session.lynched_tanner_id = target.user_id


# =====================================================================
# 10. التحقق من الفوز (لعب مفرد طبيعي أو طور الفرق)
# =====================================================================
async def check_and_handle_winner(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> bool:
    alive = session.alive_players()

    # --- أولاً: طور الفرق (تنتهي فقط عند بقاء فريق واحد على قيد الحياة) ---
    if session.is_teams_mode:
        alive_teams = session.alive_team_ids()
        if not alive or len(alive_teams) == 0:
            await declare_winner_and_end(
                context,
                session,
                winner_ids=set(),
                reason_banner="💀 مات الجميع! انتهت اللعبة بالتعادل.",
            )
            return True

        if len(alive_teams) == 1:
            winning_tid = next(iter(alive_teams))
            team_uids = set(session.teams[winning_tid])
            names = " ، ".join(
                session.players[uid].full_name for uid in session.teams[winning_tid]
            )
            await declare_winner_and_end(
                context,
                session,
                winner_ids=team_uids,
                reason_banner=f"🏆 انتهت اللعبة! الفائزون: **({names})**!",
            )
            return True
        return False

    # --- ثانياً: اللعب المفرد الطبيعي (كما في البوت الأصلي) ---
    if session.lynched_tanner_id:
        tanner = session.players[session.lynched_tanner_id]
        await declare_winner_and_end(
            context,
            session,
            winner_ids={tanner.user_id},
            reason_banner=f"👺 أعدمت القرية الدباغ **{tanner.full_name}** ففاز باللعبة!",
        )
        return True

    if not alive:
        await declare_winner_and_end(
            context,
            session,
            winner_ids=set(),
            reason_banner="💀 مات الجميع! لا فائز.",
        )
        return True

    if len(alive) == 2:
        p1, p2 = alive[0], alive[1]
        if p1.lover_id == p2.user_id:
            await declare_winner_and_end(
                context,
                session,
                winner_ids={p1.user_id, p2.user_id},
                reason_banner=f"💘 فاز العاشقان **{p1.full_name}** و **{p2.full_name}**!",
            )
            return True

    wolves = session.alive_wolves()
    sks = session.alive_sk()
    pipers = session.alive_pipers()

    if sks and len(alive) == 1:
        sk = sks[0]
        await declare_winner_and_end(
            context,
            session,
            winner_ids={sk.user_id},
            reason_banner=f"🔪 فاز السفاح **{sk.full_name}**!",
        )
        return True

    if pipers and len(alive) == 1:
        piper = pipers[0]
        await declare_winner_and_end(
            context,
            session,
            winner_ids={piper.user_id},
            reason_banner=f"🪈 فاز العازف **{piper.full_name}**!",
        )
        return True

    if not wolves and not sks and not pipers:
        village_winners = {
            p.user_id
            for p in session.players.values()
            if p.role.faction == "village"
        }
        await declare_winner_and_end(
            context,
            session,
            winner_ids=village_winners,
            reason_banner="🏡 انتصر القرويون!",
        )
        return True

    if wolves and not sks and not pipers:
        non_wolves = [
            p
            for p in alive
            if p.role_key not in WOLF_PACK_KEYS and p.role_key != "sorcerer"
        ]
        if len(wolves) >= len(non_wolves):
            wolf_winners = {
                p.user_id
                for p in session.players.values()
                if p.role_key in WOLF_PACK_KEYS or p.role_key == "sorcerer"
            }
            await declare_winner_and_end(
                context,
                session,
                winner_ids=wolf_winners,
                reason_banner="🐺 انتصرت الذئاب!",
            )
            return True

    return False


async def declare_winner_and_end(
    context: ContextTypes.DEFAULT_TYPE,
    session: GameSession,
    winner_ids: Set[int],
    reason_banner: str,
) -> None:
    session.phase = "ended"
    ACTIVE_GAMES.pop(session.chat_id, None)

    lines = [reason_banner, ""]

    if session.is_teams_mode:
        for idx, (_, member_ids) in enumerate(session.teams.items(), start=1):
            parts = []
            for uid in member_ids:
                p = session.players[uid]
                won = uid in winner_ids
                DB.record_result(p.user_id, won, p.alive)
                icon = "🙂" if p.alive else "💀"
                w_icon = "🏆" if won else "❌"
                parts.append(
                    f"{icon} **{p.full_name}** ({p.role.emoji} {p.role.name_ar}) {w_icon}"
                )
            lines.append(f"{idx}. " + " ، ".join(parts))
    else:
        for p in session.players.values():
            won = p.user_id in winner_ids
            DB.record_result(p.user_id, won, p.alive)
            icon = "🙂" if p.alive else "💀"
            w_txt = "🏆 فائز" if won else "خاسر"
            lines.append(
                f"{icon} **{p.full_name}** - {p.role.emoji} {p.role.name_ar} ({w_txt})"
            )

    winner_names_str = (
        " ، ".join(session.players[uid].full_name for uid in winner_ids)
        if winner_ids
        else "تعادل"
    )
    DB.log_match(
        session.chat_id,
        session.mode,
        session.team_size,
        len(session.players),
        winner_names_str,
    )

    await context.bot.send_message(
        chat_id=session.chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# =====================================================================
# 11. معالج الأزرار التفاعلية (CallbackQueryHandler)
# =====================================================================
async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    action = parts[0]

    if action == "set_team_size":
        chat_id = int(parts[1])
        chosen_size = int(parts[2])
        session = ACTIVE_GAMES.get(chat_id)
        if not session or session.phase != "waiting_mode":
            await query.edit_message_text("⚠️ انتهت صلاحية هذا الخيار.")
            return

        session.team_size = chosen_size
        session.phase = "joining"
        session.join_deadline = asyncio.get_event_loop().time() + JOIN_TIME_SECONDS

        mode_label = (
            "لعب مفرد طبيعي"
            if chosen_size == 1
            else f"طور الفرق ({chosen_size} لكل فريق)"
        )
        join_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="🐺 انضمام",
                        callback_data=f"join_game:{chat_id}",
                    )
                ]
            ]
        )
        await query.edit_message_text(
            f"🐺 بدأت لعبة جديدة ({mode_label}) بواسطة **{query.from_user.full_name}**!\n"
            "اضغط على زر الانضمام أو أرسل `/join`.",
            reply_markup=join_kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        session.task = asyncio.create_task(run_game_loop(context, chat_id))
        return

    if action == "join_game":
        chat_id = int(parts[1])
        await process_player_join(update, context, chat_id, from_button=True)
        return

    if len(parts) < 3:
        return

    chat_id = int(parts[1])
    target_id = int(parts[2])
    user_id = query.from_user.id

    session = ACTIVE_GAMES.get(chat_id)
    if not session or user_id not in session.players:
        await query.edit_message_text("❌ اللعبة غير نشطة.")
        return

    actor = session.players[user_id]
    target = session.players.get(target_id)
    if not actor.alive or not target or not target.alive:
        await query.edit_message_text("❌ غير متاح.")
        return

    if action in (
        "night_wolf",
        "night_seer",
        "night_sorcerer",
        "night_ga",
        "night_sk",
        "night_chemist",
        "night_piper",
        "night_cupid",
    ):
        if session.phase != "night":
            await query.edit_message_text("⌛ انتهى الليل!")
            return

        max_targets = 1
        if action in ("night_piper", "night_cupid"):
            max_targets = 2
        elif action == "night_wolf" and session.wolves_double_kill_tonight:
            max_targets = 2

        if target_id not in actor.night_targets:
            actor.night_targets.append(target_id)

        if len(actor.night_targets) >= max_targets:
            chosen_names = " و ".join(
                session.players[uid].full_name for uid in actor.night_targets
            )
            await query.edit_message_text(
                f"✅ اخترت: **{chosen_names}**",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.answer(
                f"اخترت {target.full_name}، اختر الشخص الثاني الآن!",
                show_alert=True,
            )

    elif action == "day_gunner":
        if session.phase != "day" or actor.bullets <= 0:
            await query.edit_message_text("❌ غير متاح.")
            return
        actor.bullets -= 1
        await query.edit_message_text(f"🔫 أطلقت النار على {target.full_name}!")
        await handle_player_death(
            context,
            session,
            target,
            f"🔫 أطلق القناص **{actor.full_name}** النار على **{target.full_name}** فقتله!",
        )

    elif action == "day_detective":
        if session.phase != "day" or actor.investigated_today:
            await query.edit_message_text("❌ حققت اليوم بالفعل.")
            return
        actor.investigated_today = True
        await query.edit_message_text(
            f"🕵️ **{target.full_name}** هو **{target.role.emoji} {target.role.name_ar}**!",
            parse_mode=ParseMode.MARKDOWN,
        )
        if random.random() < 0.40:
            for w in session.alive_wolves():
                if are_enemies(session, actor, w):
                    try:
                        await context.bot.send_message(
                            chat_id=w.user_id,
                            text=f"🚨 كشفت أن **{actor.full_name}** هو **المحقق**!",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

    elif action == "day_mayor":
        if session.phase != "day" or actor.mayor_revealed:
            return
        actor.mayor_revealed = True
        await query.edit_message_text("🎖️ أعلنت نفسك عمدة!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎖️ كشف **{actor.full_name}** أنه **العمدة**! صوته الآن بصوتين.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "day_pacifist":
        if session.phase != "day" or actor.pacifist_used:
            return
        actor.pacifist_used = True
        session.peace_day_active = True
        await query.edit_message_text("☮️ تم منع الإعدام اليوم!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"☮️ ألقى المسالم **{actor.full_name}** خطابه! لن يُعدم أحد اليوم.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "day_blacksmith":
        if session.phase != "day" or actor.blacksmith_used:
            return
        actor.blacksmith_used = True
        session.silver_spread_tonight = True
        await query.edit_message_text("⚒️ نثرت الفضة!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚒️ نثر الحداد **{actor.full_name}** الفضة! لا هجوم للذئاب الليلة.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "day_troublemaker":
        if session.phase != "day" or actor.troublemaker_used:
            return
        actor.troublemaker_used = True
        session.double_lynch_today = True
        await query.edit_message_text("😈 أثرت الشغب!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"😈 أثار المشاغب **{actor.full_name}** الفوضى! سيُعدم شخصان اليوم.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "lynch_vote":
        if session.phase != "lynch":
            await query.edit_message_text("⌛ انتهى التصويت!")
            return
        actor.lynch_vote = target_id
        await query.edit_message_text(
            f"⚖️ صوّتت على: **{target.full_name}**", parse_mode=ParseMode.MARKDOWN
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🗳️ صوّت **{actor.full_name}** على إعدام **{target.full_name}**.",
            parse_mode=ParseMode.MARKDOWN,
        )


# =====================================================================
# 12. أوامر البوت (متاحة لجميع الأعضاء مثل البوت الأصلي)
# =====================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    DB.upsert_player(user.id, user.username or "", user.full_name)

    text = (
        f"أهلاً بك يا **{user.full_name}** في بوت لعبة الذئب 🐺!\n\n"
        "• `/startgame` — بدء لعبة عادية\n"
        "• `/startchaos` — بدء لعبة فوضى\n"
        "• `/startteams` — بدء لعبة فرق\n"
        "• `/join` — الانضمام للعبة\n"
        "• `/flee` — الانسحاب قبل البدء\n"
        "• `/players` أو `/teams` — قائمة اللاعبين والفرق\n"
        "• `/extend` — تمديد الوقت\n"
        "• `/forcestart` — بدء اللعبة فوراً\n"
        "• `/killgame` — إنهاء اللعبة\n"
        "• `/nextgame` — تنبيهك باللعبة القادمة\n"
        "• `/rolelist` — قائمة الأدوار\n"
        "• `/stats` — إحصائياتك\n"
        "• `/top` — المتصدرون"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def prompt_game_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        await update.message.reply_text("⚠️ هذا الأمر يعمل داخل المجموعات فقط.")
        return

    if chat.id in ACTIVE_GAMES:
        await update.message.reply_text("⚠️ توجد لعبة جارية بالفعل! أرسل `/join` للانضمام.")
        return

    if context.args and context.args[0].isdigit():
        size = int(context.args[0])
        if size in (1, 2, 3, 4, 5, 6):
            await initiate_game(update, context, mode=mode, team_size=size)
            return

    DB.upsert_player(user.id, user.username or "", user.full_name)
    session = GameSession(
        chat_id=chat.id,
        chat_title=chat.title or "القرية",
        creator_id=user.id,
        mode=mode,
        phase="waiting_mode",
    )
    session.players[user.id] = GamePlayer(
        user_id=user.id, username=user.username or "", full_name=user.full_name
    )
    ACTIVE_GAMES[chat.id] = session

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👤 لعب مفرد طبيعي (بدون فرق)",
                    callback_data=f"set_team_size:{chat.id}:1",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 فرق (2)", callback_data=f"set_team_size:{chat.id}:2"
                ),
                InlineKeyboardButton(
                    "👥 فرق (3)", callback_data=f"set_team_size:{chat.id}:3"
                ),
                InlineKeyboardButton(
                    "👥 فرق (4)", callback_data=f"set_team_size:{chat.id}:4"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👥 فرق (5)", callback_data=f"set_team_size:{chat.id}:5"
                ),
                InlineKeyboardButton(
                    "👥 فرق (6)", callback_data=f"set_team_size:{chat.id}:6"
                ),
            ],
        ]
    )
    await update.message.reply_text(
        "🐺 اختر نظام اللعب لهذه الجولة:",
        reply_markup=kb,
    )


async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await prompt_game_mode(update, context, mode="normal")


async def cmd_startchaos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await prompt_game_mode(update, context, mode="chaos")


async def cmd_startteams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await prompt_game_mode(update, context, mode="teams")


async def initiate_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    team_size: int = 1,
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    DB.upsert_player(user.id, user.username or "", user.full_name)
    session = GameSession(
        chat_id=chat.id,
        chat_title=chat.title or "القرية",
        creator_id=user.id,
        mode=mode,
        team_size=team_size,
        phase="joining",
        join_deadline=asyncio.get_event_loop().time() + JOIN_TIME_SECONDS,
    )
    session.players[user.id] = GamePlayer(
        user_id=user.id, username=user.username or "", full_name=user.full_name
    )
    ACTIVE_GAMES[chat.id] = session

    waitlist = DB.pop_nextgame_list(chat.id)
    if waitlist:
        mentions = ", ".join(
            f"[{r['full_name']}](tg://user?id={r['user_id']})" for r in waitlist
        )
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"🔔 نداء للجولة الجديدة: {mentions}",
            parse_mode=ParseMode.MARKDOWN,
        )

    mode_label = (
        "لعب مفرد طبيعي"
        if team_size == 1
        else f"طور الفرق ({team_size} لكل فريق)"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🐺 انضمام",
                    callback_data=f"join_game:{chat.id}",
                )
            ]
        ]
    )
    await update.message.reply_text(
        f"🐺 بدأت لعبة جديدة ({mode_label}) بواسطة **{user.full_name}**!\n"
        "اضغط على زر الانضمام أو أرسل `/join`.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    session.task = asyncio.create_task(run_game_loop(context, chat_id))


async def process_player_join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    from_button: bool = False,
) -> None:
    user = update.effective_user
    if not user:
        return

    session = ACTIVE_GAMES.get(chat_id)
    if not session or session.phase != "joining":
        msg = "⚠️ لا توجد لعبة مفتوحة للانضمام."
        if from_button and update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(msg)
        return

    if user.id in session.players:
        msg = "✅ أنت منضم بالفعل!"
        if from_button and update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(msg)
        return

    if len(session.players) >= MAX_PLAYERS:
        await context.bot.send_message(chat_id=chat_id, text="❌ اكتمل العدد الأقصى!")
        return

    DB.upsert_player(user.id, user.username or "", user.full_name)
    session.players[user.id] = GamePlayer(
        user_id=user.id, username=user.username or "", full_name=user.full_name
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ انضم **{user.full_name}**! ({len(session.players)} لاعب)",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await process_player_join(update, context, update.effective_chat.id)


async def cmd_flee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.id not in ACTIVE_GAMES:
        return
    session = ACTIVE_GAMES[chat.id]
    if session.phase != "joining":
        await update.message.reply_text("❌ لا يمكنك الهروب بعد بدء اللعبة!")
        return
    if user.id in session.players:
        del session.players[user.id]
        await update.message.reply_text(
            f"🏃 هرب **{user.full_name}** من القرية! ({len(session.players)} متبقٍ)",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        return
    session = ACTIVE_GAMES[chat.id]
    if session.phase == "joining":
        session.join_deadline += 30
        remaining = int(session.join_deadline - asyncio.get_event_loop().time())
        await update.message.reply_text(
            f"⏱️ تم التمديد 30 ثانية! (المتبقي: {remaining} ثانية)."
        )


async def cmd_forcestart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        return
    session = ACTIVE_GAMES[chat.id]
    if session.phase == "joining":
        session.join_deadline = 0


async def cmd_killgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        return
    session = ACTIVE_GAMES.pop(chat.id)
    session.phase = "ended"
    if session.task:
        session.task.cancel()
    await update.message.reply_text("🛑 تم إلغاء اللعبة.")


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        await update.message.reply_text("ℹ️ لا توجد لعبة جارية.")
        return
    session = ACTIVE_GAMES[chat.id]
    await update.message.reply_text(
        format_players_or_teams(session, show_roles=False),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_nextgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        return
    DB.add_nextgame(chat.id, user.id, user.full_name)
    await update.message.reply_text(
        f"🔔 سيتم تنبيهك في الجولة القادمة يا **{user.full_name}**.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_rolelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📖 **أدوار اللعبة:**"]
    for r in ROLES.values():
        lines.append(f"{r.emoji} **{r.name_ar}**: {r.description}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    DB.upsert_player(user.id, user.username or "", user.full_name)
    row = DB.get_player_stats(user.id)
    if not row:
        return
    played = row["games_played"]
    won = row["games_won"]
    rate = round((won / played) * 100, 1) if played > 0 else 0.0
    text = (
        f"📊 **{row['full_name']}**\n"
        f"🎮 اللعب: **{played}** | 🏆 الفوز: **{won}** ({rate}%) | 🙂 النجاة: **{row['games_survived']}**"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = DB.get_leaderboard(10)
    if not rows:
        await update.message.reply_text("📊 لا توجد إحصائيات بعد.")
        return
    lines = ["🏆 **المتصدرون:**"]
    for idx, r in enumerate(rows, start=1):
        lines.append(
            f"{idx}. **{r['full_name']}** — 🏆 {r['games_won']} | 🎮 {r['games_played']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# =====================================================================
# 13. تسجيل قائمة أوامر السلاش (/) تلقائياً في تيليجرام
# =====================================================================
async def setup_bot_commands(app: Application) -> None:
    commands = [
        BotCommand("startgame", "بدء لعبة جديدة (مفرد أو فرق)"),
        BotCommand("startchaos", "بدء لعبة فوضى عشوائية"),
        BotCommand("startteams", "بدء لعبة طور الفرق"),
        BotCommand("join", "الانضمام إلى اللعبة"),
        BotCommand("flee", "الانسحاب من اللعبة قبل البدء"),
        BotCommand("players", "عرض اللاعبين والفرق"),
        BotCommand("teams", "عرض الفرق وأعضائها"),
        BotCommand("extend", "تمديد وقت الانضمام"),
        BotCommand("forcestart", "بدء اللعبة فوراً"),
        BotCommand("killgame", "إلغاء اللعبة الحالية"),
        BotCommand("nextgame", "تنبيهك عند بدء اللعبة القادمة"),
        BotCommand("rolelist", "عرض قائمة الأدوار"),
        BotCommand("stats", "عرض إحصائياتك"),
        BotCommand("top", "قائمة المتصدرين"),
    ]
    try:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        logger.info("✅ تم تسجيل قائمة أوامر السلاش (/) بنجاح.")
    except Exception as exc:
        logger.warning("تعذر تسجيل أوامر السلاش تلقائياً: %s", exc)


# =====================================================================
# 14. نقطة تشغيل البوت (Main Entrypoint)
# =====================================================================
def main() -> None:
    start_health_server()
    DB.init_db()

    if BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.warning("يرجى وضع التوكن الخاص ببوتك في BOT_TOKEN لتشغيل البوت.")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_bot_commands)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("startchaos", cmd_startchaos))
    app.add_handler(CommandHandler("startteams", cmd_startteams))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("flee", cmd_flee))
    app.add_handler(CommandHandler("extend", cmd_extend))
    app.add_handler(CommandHandler("forcestart", cmd_forcestart))
    app.add_handler(CommandHandler("killgame", cmd_killgame))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("teams", cmd_players))
    app.add_handler(CommandHandler("nextgame", cmd_nextgame))
    app.add_handler(CommandHandler("rolelist", cmd_rolelist))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("leaderboard", cmd_top))

    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # محادثة أعضاء الفريق الواحد داخل خاص البوت
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_private_team_chat,
        )
    )

    logger.info("🚀 بوت لعبة الذئب يعمل الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
