#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت لعبة الذئب لتطبيق تيليجرام (Werewolf Telegram Bot - Teams Survival Edition)
يعمل باستخدام مكتبة python-telegram-bot (الإصدار 20+) وقاعدة بيانات محلية SQLite3.

القواعد الأساسية في هذه النسخة:
1. الأدوار المعتمدة حصرياً (17 فئة / 20 دوراً شاملاً أنواع الذئاب):
   - الذئاب بأنواعها: الذئب العادي (Werewolf)، ذئب ألفا (Alpha Wolf)، الجرو المدلل (Wolf Cub)، المستذئب المخادع (Lycan)
   - الدباغ (Tanner)، الصياد (Hunter)، القناص (Gunner)، العمدة (Mayor)، العازف (Pied Piper)،
     المسالم (Pacifist)، ملاك الحب (Cupid)، الملاك الحارس (Guardian Angel)، الكيميائي (Chemist)،
     المحقق (Detective)، العراف (Seer)، الساحر (Sorcerer)، الحداد (Blacksmith)،
     السفاح (Serial Killer)، الأمير (Prince)، المشاغب (Troublemaker).

2. نظام الفرق العشوائية وشرط الفوز الحصري (بدون تسمية الفرق):
   - في بداية كل كيم، يختار الأدمن حجم الفريق (شخصين 2، أو 3، أو 4، أو 5، أو 6 أشخاص).
   - يتم تقسيم اللاعبين عشوائياً إلى فرق دون إعطاء أسماء للفرق، وتُعرض قوائم أعضاء كل فريق علناً في المجموعة
     عند بدء الجولة وفي كل صباح وعند استخدام /teams أو /players.
   - لا تنتهي اللعبة بموت جميع الذئاب أو بقاء القرويين فقط!
   - تنتهي اللعبة حصرياً عندما يبقى فريق واحد فقط على قيد الحياة (سواء بقي جميع أعضائه أو عضو واحد منه)،
     وعندها يفوز جميع أعضاء هذا الفريق (الأحياء والأموات، أياً كانت أدوارهم) وتخسر جميع الفرق الأخرى.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

try:
    from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ChatType, ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
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
# 1. قاعدة البيانات المحلية (SQLite3)
# =====================================================================
class LocalDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
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
# 2. تعريف الأدوار المعتمدة حصرياً (17 فئة / 20 دوراً شاملاً أنواع الذئاب)
# =====================================================================
@dataclass(frozen=True)
class RoleInfo:
    key: str
    name_ar: str
    emoji: str
    faction: str  # "village", "wolves", "neutral"
    description: str


ROLES: Dict[str, RoleInfo] = {
    # --- 1. الذئاب بأنواعها ---
    "werewolf": RoleInfo(
        "werewolf",
        "الذئب",
        "🐺",
        "wolves",
        "تستيقظ ليلاً لاختيار ضحية من الفرق الأخرى وافتراسها.",
    ),
    "alphawolf": RoleInfo(
        "alphawolf",
        "ذئب ألفا",
        "⚡🐺",
        "wolves",
        "زعيم الذئاب! عند مهاجمة ضحية ليلاً، توجد فرصة 25% لتحويلها إلى ذئب بدلاً من قتلها.",
    ),
    "wolfcub": RoleInfo(
        "wolfcub",
        "الجرو المدلل",
        "🐶",
        "wolves",
        "جرو الذئاب! إذا تم قتلك أو إعدامك، تهاجم الذئاب ضحيتين في الليلة التالية.",
    ),
    "lycan": RoleInfo(
        "lycan",
        "المستذئب المخادع",
        "🌕🐺",
        "wolves",
        "ذئب متخفٍّ! تهاجم ليلاً، لكن إذا كشفك العرّاف يراك كأنك العرّاف.",
    ),
    # --- 2. الدباغ ---
    "tanner": RoleInfo(
        "tanner",
        "الدباغ",
        "👺",
        "neutral",
        "مخادع القرية! إذا صوّتت القرية على إعدامك نهاراً، تفجّر مفاجأة وتأخذ معك أحد لاعبي الفرق الأخرى عشوائياً!",
    ),
    # --- 3. الصياد ---
    "hunter": RoleInfo(
        "hunter",
        "الصياد",
        "🎯",
        "village",
        "إذا قُتلت أو أُعدمت، تطلق رصاصتك الأخيرة لتردي أحد لاعبي الفرق الأخرى معك.",
    ),
    # --- 4. القناص ---
    "gunner": RoleInfo(
        "gunner",
        "القناص",
        "🔫",
        "village",
        "تمتلك رصاصتين فضيتين، يمكنك في النهار قنص أي لاعب من الفرق الأخرى مباشرة.",
    ),
    # --- 5. العمدة ---
    "mayor": RoleInfo(
        "mayor",
        "العمدة",
        "🎖️",
        "village",
        "يمكنك في النهار إبراز وسامك الرسمي ليصبح صوتك في الإعدام محسوباً بصوتين دائماً.",
    ),
    # --- 6. العازف ---
    "piper": RoleInfo(
        "piper",
        "العازف",
        "🪈",
        "neutral",
        "تعزف ألحانك كل ليلة لسحر لاعبين اثنين؛ وعندما تسحر جميع الأحياء من الفرق الأخرى تقضي عليهم دفعة واحدة!",
    ),
    # --- 7. المسالم ---
    "pacifist": RoleInfo(
        "pacifist",
        "المسالم",
        "☮️",
        "village",
        "يمكنك لمرة واحدة في المباراة إلقاء خطاب سلام نهاراً يمنع إعدام أي شخص في ذلك اليوم.",
    ),
    # --- 8. ملاك الحب ---
    "cupid": RoleInfo(
        "cupid",
        "ملاك الحب",
        "💘",
        "village",
        "تختار في الليلة الأولى لاعبين ليرتبطا بسهم الحب؛ إذا مات أحدهما يموت الآخر حزناً عليه فوراً.",
    ),
    # --- 9. الملاك الحارس ---
    "guardian": RoleInfo(
        "guardian",
        "الملاك الحارس",
        "👼",
        "village",
        "تختار كل ليلة لاعباً (من فريقك أو غيره) لتحرسه وتحميه من هجوم الذئاب أو غدر السفاح.",
    ),
    # --- 10. الكيميائي ---
    "chemist": RoleInfo(
        "chemist",
        "الكيميائي",
        "🧪",
        "village",
        "تزور لاعباً ليلاً ومعك كأس مسمومة وكأس سليمة؛ يختار هو إحداهما وتشرب أنت الأخرى (50% موت لأحدكما)!",
    ),
    # --- 11. المحقق ---
    "detective": RoleInfo(
        "detective",
        "المحقق",
        "🕵️",
        "village",
        "تتحقق نهاراً من الدور السري لأي لاعب، لكن هناك احتمال 40% أن تلمحك الذئاب وتعرف هويتك.",
    ),
    # --- 12. العراف ---
    "seer": RoleInfo(
        "seer",
        "العراف",
        "👳",
        "village",
        "تستبصر كل ليلة لتكشف الدور الحقيقي لأحد اللاعبين في القرية.",
    ),
    # --- 13. الساحر ---
    "sorcerer": RoleInfo(
        "sorcerer",
        "الساحر",
        "🔮",
        "wolves",
        "تكشف كل ليلة لاعباً لمعرفة ما إذا كان هو العرّاف أو من الذئاب.",
    ),
    # --- 14. الحداد ---
    "blacksmith": RoleInfo(
        "blacksmith",
        "الحداد",
        "⚒️",
        "village",
        "يمكنك نهاراً لمرة واحدة نثر غبار الفضة لمنع جميع الذئاب من الهجوم في الليلة التالية.",
    ),
    # --- 15. السفاح ---
    "serialkiller": RoleInfo(
        "serialkiller",
        "السفاح",
        "🔪",
        "neutral",
        "تخرج كل ليلة لقتل لاعب من الفرق الأخرى لمساعدة فريقك على البقاء.",
    ),
    # --- 16. الأمير ---
    "prince": RoleInfo(
        "prince",
        "الأمير",
        "👑",
        "village",
        "إذا صوّتت القرية على إعدامك لأول مرة، تكشف خاتمك الملكي ويُلغى إعدامك.",
    ),
    # --- 17. المشاغب ---
    "troublemaker": RoleInfo(
        "troublemaker",
        "المشاغب",
        "😈",
        "village",
        "يمكنك نهاراً لمرة واحدة إثارة الشغب في القرية ليتم إعدام أعلى لاعبين حصولاً على الأصوات في ذلك اليوم!",
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
    # رقم الفريق فقط لتحديد الزملاء بدون أي أسماء للفرق
    team_id: int = 1
    # حالات القدرات
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

    @property
    def display_name(self) -> str:
        return self.full_name


@dataclass
class GameSession:
    chat_id: int
    chat_title: str
    creator_id: int
    mode: str = "normal"  # "normal", "chaos", "teams"
    team_size: int = 2  # يختاره الأدمن: 2 أو 3 أو 4 أو 5 أو 6
    phase: str = "waiting_team_size"  # "waiting_team_size", "joining", "night", "day", "lynch", "ended"
    day_count: int = 0
    join_deadline: float = 0.0
    players: Dict[int, GamePlayer] = field(default_factory=dict)
    teams: Dict[int, List[int]] = field(default_factory=dict)
    # تأثيرات الجولات
    wolves_double_kill_tonight: bool = False
    silver_spread_tonight: bool = False
    silver_active_this_night: bool = False
    peace_day_active: bool = False
    double_lynch_today: bool = False
    task: Optional[asyncio.Task] = None

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
# 4. دوال مساعدة: التحقق من الأدمن + عرض الفرق بأعضاء كل فريق فقط بدون أسماء فرق
# =====================================================================
async def is_user_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int
) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False


def format_teams_board(session: GameSession, show_roles: bool = False) -> str:
    """عرض أعضاء كل فريق مباشرة بأسماء اللاعبين فقط بدون إعطاء أي أسماء للفرق."""
    lines = ["👥 **توزيع الفرق وأعضائها في هذه الجولة:**\n"]
    for idx, (_, member_ids) in enumerate(session.teams.items(), start=1):
        member_parts = []
        for uid in member_ids:
            p = session.players[uid]
            state_icon = "🟢" if p.alive else "💀"
            role_part = (
                f" ({p.role.emoji} {p.role.name_ar})"
                if (show_roles or not p.alive)
                else ""
            )
            member_parts.append(f"{state_icon} **{p.full_name}**{role_part}")
        lines.append(f"{idx}. " + " ، ".join(member_parts))

    lines.append(
        "\n⚔️ *تستمر اللعبة حتى يبقى فريق واحد فقط على قيد الحياة (أو أي عضو منه)، وحينها يفوز جميع أعضائه وتخسر بقية الفرق!*"
    )
    return "\n".join(lines)


# =====================================================================
# 5. توزيع الأدوار والفرق العشوائية (Roles & Random Teams Assignment)
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

    # تقسيم اللاعبين عشوائياً إلى فرق بحسب اختيار الأدمن (2، 3، 4، 5، 6) بدون تسمية الفرق
    tsize = session.team_size if session.team_size in (2, 3, 4, 5, 6) else 2
    session.team_size = tsize
    shuffled = list(session.players.values())
    random.shuffle(shuffled)

    session.teams.clear()

    team_idx = 0
    for i in range(0, len(shuffled), tsize):
        team_id = team_idx + 1
        members = shuffled[i : i + tsize]
        session.teams[team_id] = [m.user_id for m in members]
        for m in members:
            m.team_id = team_id
        team_idx += 1


# =====================================================================
# 6. إدارة منطق موت اللاعب (يشمل العاشقين والجرو المدلل والصياد)
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

    msg = (
        f"{cause_text}\n"
        f"كان دور **{victim.full_name}**: {victim.role.emoji} **{victim.role.name_ar}**."
    )
    if events_list is not None:
        events_list.append(msg)
    else:
        await context.bot.send_message(
            chat_id=session.chat_id, text=msg, parse_mode=ParseMode.MARKDOWN
        )

    # 1. إذا مات الجرو المدلل (Wolf Cub)
    if victim.role_key == "wolfcub":
        session.wolves_double_kill_tonight = True
        cub_msg = "🐶💢 قُتل **الجرو المدلل**! ستفترس الذئاب **ضحيتين** في الليلة القادمة!"
        if events_list is not None:
            events_list.append(cub_msg)
        else:
            await context.bot.send_message(
                chat_id=session.chat_id, text=cub_msg, parse_mode=ParseMode.MARKDOWN
            )

    # 2. إذا مات الصياد (Hunter): يصيب لاعباً عشوائياً من الفرق الأخرى!
    if victim.role_key == "hunter":
        enemy_candidates = [
            p
            for p in session.alive_players()
            if p.user_id != victim.user_id and p.team_id != victim.team_id
        ]
        if not enemy_candidates:
            enemy_candidates = [
                p for p in session.alive_players() if p.user_id != victim.user_id
            ]
        if enemy_candidates:
            shot = random.choice(enemy_candidates)
            hunter_cause = (
                f"🎯 قبل أن يلفظ الصياد **{victim.full_name}** أنفاسه الأخيرة، أطلق رصاصته على "
                f"**{shot.full_name}** وأرداه قتيلاً!"
            )
            await handle_player_death(
                context, session, shot, hunter_cause, events_list
            )

    # 3. إذا كان مرتبطاً بسهم ملاك الحب (Cupid Lover)
    if victim.lover_id:
        partner = session.players.get(victim.lover_id)
        if partner and partner.alive:
            lover_cause = (
                f"💔 لم يتحمل **{partner.full_name}** فراق حبيبه **{victim.full_name}**، "
                "فمات حزناً عليه فوراً!"
            )
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

    while session.phase == "joining":
        remaining = int(session.join_deadline - asyncio.get_event_loop().time())
        if remaining <= 0:
            break
        if remaining in (60, 30, 10):
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏳ تبقى **{remaining} ثانية** على إغلاق الانضمام!\n"
                    f"المنضمون حالياً: **{len(session.players)}** لاعبين.\n"
                    "أرسل `/join` للانضمام."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        await asyncio.sleep(1)

    if len(session.players) < MIN_PLAYERS:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ أُلغيت الجولة لعدم اكتمال الحد الأدنى ({MIN_PLAYERS} لاعبين).",
        )
        ACTIVE_GAMES.pop(chat_id, None)
        return

    # توزيع الأدوار والفرق العشوائية
    assign_roles_and_teams(session)

    # ضمان وجود فريقين على الأقل إذا كان عدد اللاعبين صغيراً
    if len(session.teams) < 2:
        session.team_size = max(1, len(session.players) // 2)
        assign_roles_and_teams(session)

    # 1. عرض أعضاء كل فريق علناً في المجموعة عند بداية اللعبة (بدون أسماء للفرق)
    await context.bot.send_message(
        chat_id=chat_id,
        text=format_teams_board(session, show_roles=False),
        parse_mode=ParseMode.MARKDOWN,
    )

    # 2. إرسال الأدوار السرية وأسماء زملاء الفريق في الخاص لكل لاعب
    for p in session.players.values():
        teammates = [
            session.players[uid].full_name
            for uid in session.teams.get(p.team_id, [])
            if uid != p.user_id
        ]
        teammates_str = (
            " ، ".join(teammates) if teammates else "أنت وحدك في هذا الفريق"
        )
        role_msg = (
            f"🎭 **دورك السري في قرية ({session.chat_title}):**\n"
            f"{p.role.emoji} **{p.role.name_ar}**\n\n"
            f"📜 {p.role.description}\n\n"
            f"🤝 **أعضاء فريقك:** {teammates_str}\n"
            "🏆 *تذكير: تنتهي اللعبة فقط عندما يبقى فريق واحد على قيد الحياة (أو أي عضو منه)، وحينها يفوز أعضاء فريقك جميعاً!*"
        )
        try:
            await context.bot.send_message(
                chat_id=p.user_id, text=role_msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            logger.warning("تعذر إرسال الدور بالخاص للاعب %s", p.full_name)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔔 **انطلقت المباراة ({len(session.players)} لاعبين موزعين على {len(session.teams)} فرق)!**\n"
            "تفقدوا رسائلكم الخاصة لمعرفة أدواركم السرية، ويمكنكم في أي وقت إرسال `/teams` أو `/players` لعرض الفرق وأعضائها."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # حلقة تعاقب الليل والنهار حتى يبقى فريق واحد فقط على قيد الحياة
    while session.phase != "ended":
        session.day_count += 1
        session.phase = "night"
        session.silver_active_this_night = session.silver_spread_tonight
        session.silver_spread_tonight = False

        for p in session.alive_players():
            p.night_targets.clear()
            p.investigated_today = False

        night_note = (
            "\n⚒️✨ *غبار الفضة الذي نثره الحداد يملأ القرية ويمنع هجوم الذئاب الليلة!*"
            if session.silver_active_this_night
            else ""
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🌙 **حلّ الليل ({session.day_count})** على القرية... ({NIGHT_TIME_SECONDS} ثانية)"
                f"{night_note}"
            ),
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
                f"☀️ **أشرقت شمس اليوم ({session.day_count})!**\n\n"
                + (
                    "\n".join(night_events)
                    if night_events
                    else "✨ مرّت الليلة بهدوء ولم يُقتل أحد!"
                )
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

        # التحقق مما إذا بقي فريق واحد فقط على قيد الحياة
        if await check_and_handle_winner(context, session):
            return

        # عرض الفرق وأعضائها الأحياء والأموات في المجموعة كل صباح
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_teams_board(session, show_roles=False),
            parse_mode=ParseMode.MARKDOWN,
        )

        # ----------------- مرحلة النهار (Day Phase) -----------------
        session.phase = "day"
        session.peace_day_active = False
        session.double_lynch_today = False

        await send_day_menus(context, session)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🗣️ **وقت النقاش النهاري ({DAY_TIME_SECONDS} ثانية):**\n"
                "نسّق مع أعضاء فريقك! يمكن للقناص والمحقق والعمدة والمسالم والحداد والمشاغب استخدام قدراتهم الآن عبر الخاص."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await asyncio.sleep(DAY_TIME_SECONDS)

        if chat_id not in ACTIVE_GAMES:
            return
        if await check_and_handle_winner(context, session):
            return

        # ----------------- مرحلة التصويت والإعدام (Lynch Phase) -----------------
        if session.peace_day_active:
            await context.bot.send_message(
                chat_id=chat_id,
                text="☮️ بفضل خطاب **المسالم**، تم إلغاء التصويت والإعدام لهذا اليوم!",
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        session.phase = "lynch"
        for p in session.alive_players():
            p.lynch_vote = None

        await send_lynch_menus(context, session)
        lynch_note = (
            "\n😈 *بسبب شغب المشاغب، سيتم إعدام أعلى لاعبين حصولاً على الأصوات اليوم!*"
            if session.double_lynch_today
            else ""
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚖️ **بدأت مرحلة التصويت للإعدام ({LYNCH_TIME_SECONDS} ثانية)!**"
                f"{lynch_note}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await asyncio.sleep(LYNCH_TIME_SECONDS)

        if chat_id not in ACTIVE_GAMES:
            return

        await resolve_lynch_votes(context, session)

        if await check_and_handle_winner(context, session):
            return


# =====================================================================
# 8. إرسال القوائم التفاعلية ومعالجة الليل والنهار والتصويت
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
            # 1. الذئاب (يستهدفون لاعبي الفرق الأخرى فقط)
            if p.role_key in WOLF_PACK_KEYS and not session.silver_active_this_night:
                targets = [t for t in alive if t.team_id != p.team_id]
                if targets:
                    kill_count = 2 if session.wolves_double_kill_tonight else 1
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text=f"🐺 اختر ضحية من الفرق الأخرى لافتراسها الليلة (العدد المتاح: {kill_count}):",
                        reply_markup=build_target_keyboard(
                            "night_wolf", session.chat_id, targets
                        ),
                    )
            # 2. العراف
            elif p.role_key == "seer":
                targets = [t for t in alive if t.user_id != p.user_id]
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="👳 اختر لاعباً لكشف دوره الحقيقي:",
                    reply_markup=build_target_keyboard(
                        "night_seer", session.chat_id, targets
                    ),
                )
            # 3. الساحر (Sorcerer)
            elif p.role_key == "sorcerer":
                targets = [t for t in alive if t.user_id != p.user_id]
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="🔮 اختر لاعباً للتحقق مما إذا كان هو العرّاف أو من الذئاب:",
                    reply_markup=build_target_keyboard(
                        "night_sorcerer", session.chat_id, targets
                    ),
                )
            # 4. الملاك الحارس
            elif p.role_key == "guardian":
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="👼 اختر لاعباً لحراسته وحمايته الليلة:",
                    reply_markup=build_target_keyboard(
                        "night_ga", session.chat_id, alive
                    ),
                )
            # 5. السفاح
            elif p.role_key == "serialkiller":
                targets = [t for t in alive if t.team_id != p.team_id]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🔪 اختر ضحيتك الليلية من الفرق الأخرى:",
                        reply_markup=build_target_keyboard(
                            "night_sk", session.chat_id, targets
                        ),
                    )
            # 6. الكيميائي
            elif p.role_key == "chemist":
                targets = [t for t in alive if t.team_id != p.team_id]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🧪 اختر لاعباً من الفرق الأخرى لتشرب معه إحدى الجرعتين الليلة:",
                        reply_markup=build_target_keyboard(
                            "night_chemist", session.chat_id, targets
                        ),
                    )
            # 7. العازف (Pied Piper)
            elif p.role_key == "piper":
                targets = [
                    t
                    for t in alive
                    if t.team_id != p.team_id and not t.charmed
                ]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text="🪈 اختر حتى لاعبين اثنين من الفرق الأخرى لسحرهما بموسيقاك:",
                        reply_markup=build_target_keyboard(
                            "night_piper", session.chat_id, targets
                        ),
                    )
            # 8. ملاك الحب (في الليلة الأولى فقط)
            elif p.role_key == "cupid" and session.day_count == 1:
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="💘 اختر العاشق الأول ثم العاشق الثاني للربط بينهما بسهم الحب:",
                    reply_markup=build_target_keyboard(
                        "night_cupid", session.chat_id, alive
                    ),
                )
        except Exception as exc:
            logger.debug("تعذر إرسال قائمة الليل للاعب %s: %s", p.user_id, exc)


async def resolve_night_actions(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> List[str]:
    events: List[str] = []
    protected_ids: Set[int] = set()

    # 1. ربط العاشقين من ملاك الحب في الليلة الأولى
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
                                text=(
                                    f"💘 أصابك سهم **ملاك الحب**! أنت الآن مرتبط عاطفياً مع "
                                    f"**{other.full_name}**؛ إذا مات أحدكما سيموت الآخر فوراً!"
                                ),
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except Exception:
                            pass

    # 2. حماية الملاك الحارس
    for p in session.alive_players():
        if p.role_key == "guardian" and p.night_targets:
            protected_ids.add(p.night_targets[0])

    # 3. كشف العرّاف (المستذئب Lycan يظهر كأنه العرّاف لخداعه)
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
                        text=(
                            f"👳 رؤيتك تكشف أن **{target.full_name}** هو: "
                            f"{shown_role.emoji} **{shown_role.name_ar}**!"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    # 4. كشف الساحر (Sorcerer)
    for p in session.alive_players():
        if p.role_key == "sorcerer" and p.night_targets:
            target = session.players.get(p.night_targets[0])
            if target:
                if target.role_key == "seer":
                    res = "👳 نعم! إنه **العرّاف (Seer)**!"
                elif target.role_key in WOLF_PACK_KEYS:
                    res = f"🐺 إنه من الذئاب ({target.role.name_ar})!"
                else:
                    res = "ℹ️ ليس هو العرّاف وليس من الذئاب."
                try:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text=f"🔮 نتيجة فحصك السحري لـ **{target.full_name}**:\n{res}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    # 5. العازف (Pied Piper): يسحر لاعبين، وإذا سحر كل الأحياء من الفرق الأخرى يقضي عليهم!
    for p in session.alive_players():
        if p.role_key == "piper" and p.night_targets:
            charmed_names = []
            for tid in p.night_targets[:2]:
                target = session.players.get(tid)
                if target and target.alive:
                    target.charmed = True
                    charmed_names.append(target.full_name)
            if charmed_names:
                events.append(
                    f"🪈 عزف **العازف** ألحانه في الظلام وسحر كلاً من: {', '.join(charmed_names)}!"
                )
            enemies = [
                e for e in session.alive_players() if e.team_id != p.team_id
            ]
            if enemies and all(e.charmed for e in enemies):
                events.append(
                    f"🪈💥 اكتملت سيمفونية **العازف ({p.full_name})** وقضت ألحانه السحرية على جميع المسحورين من الفرق الأخرى!"
                )
                for e in list(enemies):
                    await handle_player_death(
                        context,
                        session,
                        e,
                        f"🪈 سقط **{e.full_name}** صريعاً بتأثير لحن العازف القاتل!",
                        events,
                    )

    # 6. الكيميائي (Chemist)
    for p in session.alive_players():
        if p.role_key == "chemist" and p.night_targets and p.alive:
            target = session.players.get(p.night_targets[0])
            if target and target.alive:
                if random.random() < 0.5:
                    await handle_player_death(
                        context,
                        session,
                        target,
                        f"🧪 زار الكيميائي **{p.full_name}** اللاعب **{target.full_name}**، وشرب الضحية الجرعة المسمومة!",
                        events,
                    )
                else:
                    await handle_player_death(
                        context,
                        session,
                        p,
                        f"🧪 أجبر الكيميائي **{p.full_name}** اللاعب **{target.full_name}** على اختيار جرعة، لكن الكيميائي شرب السم بنفسه ومات!",
                        events,
                    )

    # 7. هجوم الذئاب
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
                events.append(
                    f"👼 هاجمت الذئاب **{victim.full_name}**، لكن **الملاك الحارس** أنقذه!"
                )
            elif has_alpha and random.random() < 0.25:
                victim.role_key = "werewolf"
                try:
                    await context.bot.send_message(
                        chat_id=victim.user_id,
                        text="⚡🐺 عضّك **ذئب ألفا** فتحولت إلى **ذئب** (مع بقائك في نفس فريقك)!",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
                events.append(
                    f"🐾 عضّ ذئب ألفا أحد اللاعبين ليلاً وحوّله إلى ذئب!"
                )
            else:
                await handle_player_death(
                    context,
                    session,
                    victim,
                    f"🩸 افترست الذئاب **{victim.full_name}** في ظلام الليل!",
                    events,
                )

    # 8. هجوم السفاح (Serial Killer)
    for sk in session.alive_sk():
        if sk.night_targets:
            target = session.players.get(sk.night_targets[0])
            if target and target.alive:
                if target.user_id in protected_ids:
                    events.append(
                        f"👼 تصدى الملاك الحارس لهجوم **السفاح** على **{target.full_name}**!"
                    )
                else:
                    await handle_player_death(
                        context,
                        session,
                        target,
                        f"🔪 غدر **السفاح** باللاعب **{target.full_name}** وطعنه في الظلام!",
                        events,
                    )

    return events


async def send_day_menus(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> None:
    alive = session.alive_players()
    for p in alive:
        try:
            # 1. القناص
            if p.role_key == "gunner" and p.bullets > 0:
                targets = [t for t in alive if t.team_id != p.team_id]
                if targets:
                    await context.bot.send_message(
                        chat_id=p.user_id,
                        text=f"🔫 لديك ({p.bullets}) رصاصة. اختر لاعباً من الفرق الأخرى لقنصه نهاراً:",
                        reply_markup=build_target_keyboard(
                            "day_gunner", session.chat_id, targets
                        ),
                    )
            # 2. المحقق
            elif p.role_key == "detective":
                targets = [t for t in alive if t.user_id != p.user_id]
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="🕵️ اختر لاعباً للتحقيق في هويته نهاراً:",
                    reply_markup=build_target_keyboard(
                        "day_detective", session.chat_id, targets
                    ),
                )
            # 3. العمدة
            elif p.role_key == "mayor" and not p.mayor_revealed:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="🎖️ إعلان هويتي كعمدة (صوت مضاعف)",
                                callback_data=f"day_mayor:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="🎖️ هل تريد الكشف عن هويتك كعمدة القرية؟",
                    reply_markup=kb,
                )
            # 4. المسالم
            elif p.role_key == "pacifist" and not p.pacifist_used:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="☮️ إلقاء خطاب السلام (إلغاء الإعدام اليوم)",
                                callback_data=f"day_pacifist:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="☮️ هل ترغب في استخدام قدرتك لإلغاء الإعدام هذا اليوم؟",
                    reply_markup=kb,
                )
            # 5. الحداد
            elif p.role_key == "blacksmith" and not p.blacksmith_used:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="⚒️ نثر الفضة (منع هجوم الذئاب الليلة القادمة)",
                                callback_data=f"day_blacksmith:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="⚒️ هل تريد نثر الفضة اليوم لحماية القرية في الليلة القادمة؟",
                    reply_markup=kb,
                )
            # 6. المشاغب
            elif p.role_key == "troublemaker" and not p.troublemaker_used:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="😈 إثارة الشغب (إعدام شخصين اليوم!)",
                                callback_data=f"day_troublemaker:{session.chat_id}:{p.user_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=p.user_id,
                    text="😈 هل تريد إثارة الفوضى ليتم إعدام لاعبين اثنين اليوم؟",
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
                text="⚖️ صوّت على الشخص الذي تريد إعدامه اليوم:",
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
            text="⚖️ لم يصوّت أحد اليوم! لن يتم إعدام أي شخص.",
        )
        return

    sorted_candidates = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    lynch_limit = 2 if session.double_lynch_today else 1

    if len(sorted_candidates) >= 2 and lynch_limit == 1:
        if sorted_candidates[0][1] == sorted_candidates[1][1]:
            await context.bot.send_message(
                chat_id=session.chat_id,
                text="⚖️ تعادلت الأصوات! لن يتم إعدام أحد.",
            )
            return

    for uid, vcount in sorted_candidates[:lynch_limit]:
        target = session.players.get(uid)
        if not target or not target.alive:
            continue

        # قدرة الأمير (Prince): ينجو من الإعدام في المرة الأولى
        if target.role_key == "prince" and not target.prince_saved_once:
            target.prince_saved_once = True
            await context.bot.send_message(
                chat_id=session.chat_id,
                text=(
                    f"👑 رفع **{target.full_name}** خاتمه الملكي معلناً أنه **الأمير (Prince)**!\n"
                    "تم إلغاء حكم الإعدام بحقه هذه المرة!"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        await handle_player_death(
            context,
            session,
            target,
            f"🔥 قررت القرية بأغلبية (**{vcount} أصوات**) إعدام **{target.full_name}**!",
        )

        # إذا أُعدم الدباغ (Tanner)، ينتقم لفريقه بقتل لاعب عشوائي من الفرق الأخرى وتستمر اللعبة حتى يبقى فريق واحد!
        if target.role_key == "tanner":
            enemies = [
                e for e in session.alive_players() if e.team_id != target.team_id
            ]
            if enemies:
                victim = random.choice(enemies)
                await handle_player_death(
                    context,
                    session,
                    victim,
                    f"👺💥 فجّر الدباغ **{target.full_name}** مفاجأته عند إعدامه وأخذ معه **{victim.full_name}**!",
                )


# =====================================================================
# 9. التحقق من شرط الفوز الحصري: بقاء فريق واحد فقط على قيد الحياة!
# =====================================================================
async def check_and_handle_winner(
    context: ContextTypes.DEFAULT_TYPE, session: GameSession
) -> bool:
    """
    تنتهي اللعبة فقط عندما:
    - يبقى فريق واحد فقط على قيد الحياة (سواء بقي كل أعضائه أو عضو واحد منه).
    - أو يموت الجميع دون بقاء أي فريق.
    ولا تنتهي بموت الذئاب أو بقاء القرويين طالما هناك أكثر من فريق على قيد الحياة!
    """
    alive = session.alive_players()
    alive_teams = session.alive_team_ids()

    if not alive or len(alive_teams) == 0:
        await declare_winner_and_end(
            context,
            session,
            winning_team_id=0,
            reason_banner="💀 سقط جميع اللاعبين في القرية! انتهت المعركة بالتعادل دون بقاء أي فريق.",
        )
        return True

    if len(alive_teams) == 1:
        winning_tid = next(iter(alive_teams))
        all_team_members = [
            session.players[uid].full_name for uid in session.teams[winning_tid]
        ]
        surviving_members = [
            p.full_name for p in alive if p.team_id == winning_tid
        ]
        await declare_winner_and_end(
            context,
            session,
            winning_team_id=winning_tid,
            reason_banner=(
                f"🏆 **انتهت المعركة بفوز الفريق المكون من: ({' ، '.join(all_team_members)})!**\n"
                f"الناجون من الفريق حتى النهاية: **{' ، '.join(surviving_members)}** بعد القضاء على جميع الفرق الأخرى!"
            ),
        )
        return True

    return False


async def declare_winner_and_end(
    context: ContextTypes.DEFAULT_TYPE,
    session: GameSession,
    winning_team_id: int,
    reason_banner: str,
) -> None:
    session.phase = "ended"
    ACTIVE_GAMES.pop(session.chat_id, None)

    lines = [reason_banner]

    if winning_team_id > 0:
        winning_names = " ، ".join(
            session.players[uid].full_name
            for uid in session.teams[winning_team_id]
        )
        lines.append(
            f"\n🎉 **الأعضاء الفائزون بالمباراة: ({winning_names})**\n"
            "✅ فاز جميع أعضاء هذا الفريق (الأحياء والأموات، أياً كانت أدوارهم)، وخسرت جميع الفرق البقية!\n"
        )
        winner_label_db = winning_names
    else:
        winner_label_db = "تعادل"

    lines.append("📋 **كشف الفرق والأدوار والنتائج النهائية:**\n")

    for idx, (t_id, member_ids) in enumerate(session.teams.items(), start=1):
        is_winning_team = (winning_team_id > 0) and (t_id == winning_team_id)
        team_result_badge = "🏆 **[فائز]**" if is_winning_team else "❌ **[خاسر]**"
        member_names_header = " ، ".join(
            session.players[uid].full_name for uid in member_ids
        )
        lines.append(f"{idx}. **({member_names_header})** {team_result_badge}:")

        for uid in member_ids:
            p = session.players[uid]
            DB.record_result(p.user_id, is_winning_team, p.alive)
            state_str = "🟢 حي" if p.alive else "💀 ميت"
            win_str = "🏆 فائز" if is_winning_team else "❌ خاسر"
            lines.append(
                f"   • **{p.full_name}**: {p.role.emoji} {p.role.name_ar} — ({state_str} | {win_str})"
            )
        lines.append("")

    DB.log_match(
        session.chat_id,
        session.mode,
        session.team_size,
        len(session.players),
        winner_label_db,
    )

    await context.bot.send_message(
        chat_id=session.chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# =====================================================================
# 10. معالج الأزرار التفاعلية (CallbackQueryHandler)
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

    # اختيار الأدمن لحجم الفريق (2، 3، 4، 5، 6) في بداية كل كيم
    if action == "set_team_size":
        chat_id = int(parts[1])
        chosen_size = int(parts[2])
        user = query.from_user
        session = ACTIVE_GAMES.get(chat_id)
        if not session or session.phase != "waiting_team_size":
            await query.edit_message_text("⚠️ لم تعد هذه الجلسة بانتظار اختيار حجم الفريق.")
            return

        if user.id != session.creator_id and not await is_user_admin(
            context, chat_id, user.id
        ):
            await query.answer(
                "⚠️ فقط الأدمن أو منشئ الجولة يمكنه اختيار حجم الفريق!",
                show_alert=True,
            )
            return

        session.team_size = chosen_size
        session.phase = "joining"
        session.join_deadline = asyncio.get_event_loop().time() + JOIN_TIME_SECONDS

        join_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text=f"🐺 انضم الآن (فرق من {chosen_size} لاعبين)!",
                        callback_data=f"join_game:{chat_id}",
                    )
                ]
            ]
        )
        await query.edit_message_text(
            f"📣 **بدأت مرحلة التسجيل في الجولة (حجم كل فريق: {chosen_size} لاعبين)!**\n"
            f"⏱️ وقت الانضمام: **{JOIN_TIME_SECONDS} ثانية**.\n"
            "اضغط على الزر أدناه أو أرسل `/join` للمشاركة.",
            reply_markup=join_kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        session.task = asyncio.create_task(run_game_loop(context, chat_id))
        return

    # زر الانضمام للعبة
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
        await query.edit_message_text("❌ هذه المباراة لم تعد نشطة.")
        return

    actor = session.players[user_id]
    target = session.players.get(target_id)
    if not actor.alive or not target or not target.alive:
        await query.edit_message_text("❌ لا يمكنك تنفيذ هذا الإجراء حالياً.")
        return

    # --- قدرات الليل ---
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
            await query.edit_message_text("⌛ انتهى وقت الليل!")
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
                f"✅ تم تسجيل اختيارك: **{chosen_names}**",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.answer(
                f"تم اختيار {target.full_name}. اختر الهدف الثاني الآن!",
                show_alert=True,
            )

    # --- قدرات النهار ---
    elif action == "day_gunner":
        if session.phase != "day" or actor.bullets <= 0:
            await query.edit_message_text("❌ لا يمكنك القنص الآن.")
            return
        actor.bullets -= 1
        await query.edit_message_text(f"🔫 أطلقت النار على {target.full_name}!")
        await handle_player_death(
            context,
            session,
            target,
            f"💥🔫 صوّب القناص **{actor.full_name}** بندقيته وأطلق رصاصة فضية قتلت **{target.full_name}**!",
        )

    elif action == "day_detective":
        if session.phase != "day" or actor.investigated_today:
            await query.edit_message_text("❌ لقد حققت بالفعل هذا النهار.")
            return
        actor.investigated_today = True
        await query.edit_message_text(
            f"🕵️ نتيجة تحقيقك السري: **{target.full_name}** هو {target.role.emoji} **{target.role.name_ar}**!",
            parse_mode=ParseMode.MARKDOWN,
        )
        if random.random() < 0.40:
            for w in session.alive_wolves():
                if w.team_id != actor.team_id:
                    try:
                        await context.bot.send_message(
                            chat_id=w.user_id,
                            text=f"🚨 انتبه! لمحتَ **{actor.full_name}** يتجسس وعرفت أنه **المحقق (Detective)**!",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

    elif action == "day_mayor":
        if session.phase != "day" or actor.mayor_revealed:
            return
        actor.mayor_revealed = True
        await query.edit_message_text("🎖️ تم إعلان هويتك كعمدة!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎖️📜 أبرز **{actor.full_name}** خاتمه الرسمي معلناً أنه **العمدة (Mayor)**!\n"
                "سيُحتسب صوته في الإعدام **بصوتين**!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "day_pacifist":
        if session.phase != "day" or actor.pacifist_used:
            return
        actor.pacifist_used = True
        session.peace_day_active = True
        await query.edit_message_text("☮️ ألقيت خطاب السلام!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"☮️🕊️ ألقى المسالم **{actor.full_name}** خطاباً مؤثراً عن السلام!\n"
                "قررت القرية عدم إعدام أي شخص في هذا اليوم!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "day_blacksmith":
        if session.phase != "day" or actor.blacksmith_used:
            return
        actor.blacksmith_used = True
        session.silver_spread_tonight = True
        await query.edit_message_text("⚒️ تم نثر الفضة لحماية القرية الليلة القادمة!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚒️✨ خرج الحداد **{actor.full_name}** ونثر غبار الفضة في أرجاء القرية!\n"
                "لن تتمكن الذئاب من مهاجمة أحد في الليلة القادمة!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "day_troublemaker":
        if session.phase != "day" or actor.troublemaker_used:
            return
        actor.troublemaker_used = True
        session.double_lynch_today = True
        await query.edit_message_text("😈 أشعلت الشغب في القرية!")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"😈🔥 أثار المشاغب **{actor.full_name}** الفوضى والغضب في القرية!\n"
                "سيتم اليوم إعدام **لاعبين اثنين** بدلاً من لاعب واحد!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    # --- التصويت على الإعدام ---
    elif action == "lynch_vote":
        if session.phase != "lynch":
            await query.edit_message_text("⌛ انتهى وقت التصويت!")
            return
        actor.lynch_vote = target_id
        await query.edit_message_text(
            f"⚖️ صوّتت ضد: **{target.full_name}**", parse_mode=ParseMode.MARKDOWN
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🗳️ صوّت **{actor.full_name}** على إعدام **{target.full_name}**.",
            parse_mode=ParseMode.MARKDOWN,
        )


# =====================================================================
# 11. أوامر البوت (Commands)
# =====================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    DB.upsert_player(user.id, user.username or "", user.full_name)

    text = (
        f"أهلاً بك يا **{user.full_name}** في **بوت لعبة الذئب (طور الفرق والأدوار الـ 17)**! 🐺\n\n"
        "📌 **أوامر تشغيل المباريات في المجموعات:**\n"
        "• `/startgame` أو `/startteams` — بدء جولة جديدة (يختار الأدمن حجم كل فريق: 2 أو 3 أو 4 أو 5 أو 6)\n"
        "• `/startchaos` — بدء جولة فوضى عشوائية الأدوار مع فرق\n"
        "• `/join` — الانضمام إلى الجولة\n"
        "• `/flee` — الانسحاب قبل البدء\n"
        "• `/teams` أو `/players` — عرض الفرق وأعضائها في المجموعة\n"
        "• `/extend` — تمديد وقت الانضمام 30 ثانية\n"
        "• `/forcestart` — بدء الجولة فوراً\n"
        "• `/killgame` — إلغاء الجولة الحالية\n"
        "• `/nextgame` — تنبيهك عند بدء الجولة القادمة\n\n"
        "📖 **أوامر الأدوار والإحصائيات:**\n"
        "• `/rolelist` — عرض قائمة الأدوار الـ 17 المعتمدة وشرحها\n"
        "• `/stats` — عرض إحصائياتك في قاعدة البيانات المحلية\n"
        "• `/top` — لوحة صدارة اللاعبين"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def prompt_team_size_or_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str
) -> None:
    """يطلب من الأدمن اختيار حجم الفريق (2، 3، 4، 5، 6) لكل كيم قبل فتح باب الانضمام."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        await update.message.reply_text("⚠️ يجب استخدام هذا الأمر داخل مجموعة!")
        return

    if not await is_user_admin(context, chat.id, user.id):
        await update.message.reply_text(
            "⚠️ فقط مشرفو المجموعة (Admins) يمكنهم بدء الجولة واختيار عدد أفراد كل فريق!"
        )
        return

    if chat.id in ACTIVE_GAMES:
        await update.message.reply_text("⚠️ توجد جولة نشطة بالفعل في هذه المجموعة!")
        return

    # إذا كتب الأدمن حجم الفريق مباشرة بعد الأمر مثل: /startgame 3 أو /startteams 4
    if context.args and context.args[0].isdigit():
        size = int(context.args[0])
        if size in (2, 3, 4, 5, 6):
            await initiate_game(update, context, mode=mode, team_size=size)
            return

    DB.upsert_player(user.id, user.username or "", user.full_name)
    session = GameSession(
        chat_id=chat.id,
        chat_title=chat.title or "القرية",
        creator_id=user.id,
        mode=mode,
        phase="waiting_team_size",
    )
    session.players[user.id] = GamePlayer(
        user_id=user.id, username=user.username or "", full_name=user.full_name
    )
    ACTIVE_GAMES[chat.id] = session

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 شخصين (2)", callback_data=f"set_team_size:{chat.id}:2"
                ),
                InlineKeyboardButton(
                    "👥 3 أشخاص", callback_data=f"set_team_size:{chat.id}:3"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👥 4 أشخاص", callback_data=f"set_team_size:{chat.id}:4"
                ),
                InlineKeyboardButton(
                    "👥 5 أشخاص", callback_data=f"set_team_size:{chat.id}:5"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👥 6 أشخاص", callback_data=f"set_team_size:{chat.id}:6"
                ),
            ],
        ]
    )
    await update.message.reply_text(
        f"👥 **إعداد جولة جديدة** بواسطة المشرف **{user.full_name}**:\n"
        "اختر عدد اللاعبين في كل فريق لهذا الكيم:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await prompt_team_size_or_start(update, context, mode="normal")


async def cmd_startchaos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await prompt_team_size_or_start(update, context, mode="chaos")


async def cmd_startteams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await prompt_team_size_or_start(update, context, mode="teams")


async def initiate_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    team_size: int = 2,
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
            text=f"🔔 **نداء للمنتظرين في الجولة الجديدة:**\n{mentions}",
            parse_mode=ParseMode.MARKDOWN,
        )

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=f"🐺 انضم الآن (فرق من {team_size} لاعبين)!",
                    callback_data=f"join_game:{chat.id}",
                )
            ]
        ]
    )
    await update.message.reply_text(
        f"📣 بدأ المشرف **{user.full_name}** جولة جديدة (**حجم كل فريق: {team_size} لاعبين**)!\n"
        f"⏱️ وقت الانضمام: **{JOIN_TIME_SECONDS} ثانية**.\n"
        "اضغط على الزر أدناه أو أرسل `/join` للانضمام.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    session.task = asyncio.create_task(run_game_loop(context, chat.id))


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
        msg = "⚠️ لا توجد جولة مفتوحة للانضمام حالياً."
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
        await context.bot.send_message(chat_id=chat_id, text="❌ اكتمل الحد الأقصى للاعبين!")
        return

    DB.upsert_player(user.id, user.username or "", user.full_name)
    session.players[user.id] = GamePlayer(
        user_id=user.id, username=user.username or "", full_name=user.full_name
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ انضم **{user.full_name}** إلى القرية! (العدد الحالي: **{len(session.players)}**)",
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
        await update.message.reply_text("❌ لا يمكنك الانسحاب بعد بدء الجولة!")
        return
    if user.id in session.players:
        del session.players[user.id]
        await update.message.reply_text(
            f"🏃‍♂️ انسحب **{user.full_name}** قبل بدء المعركة! (المتبقون: {len(session.players)})",
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
            f"⏱️ تم تمديد وقت الانضمام 30 ثانية! (المتبقي: **{remaining} ثانية**).",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_forcestart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        return
    session = ACTIVE_GAMES[chat.id]
    if session.phase == "joining":
        session.join_deadline = 0
        await update.message.reply_text("🚀 تم بدء الجولة فوراً!")


async def cmd_killgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        return
    session = ACTIVE_GAMES.pop(chat.id)
    session.phase = "ended"
    if session.task:
        session.task.cancel()
    await update.message.reply_text("🛑 تم إلغاء الجولة الحالية.")


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض الفرق وأعضاء كل فريق في المجموعة بدون أسماء للفرق."""
    chat = update.effective_chat
    if not chat or chat.id not in ACTIVE_GAMES:
        await update.message.reply_text("ℹ️ لا توجد جولة نشطة حالياً.")
        return
    session = ACTIVE_GAMES[chat.id]
    if session.teams:
        await update.message.reply_text(
            format_teams_board(session, show_roles=False),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        lines = [f"👥 **اللاعبون المنضمون حالياً ({len(session.players)}):**"]
        for idx, p in enumerate(session.players.values(), start=1):
            lines.append(f"{idx}. **{p.full_name}**")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_nextgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        return
    DB.add_nextgame(chat.id, user.id, user.full_name)
    await update.message.reply_text(
        f"🔔 تم تسجيلك يا **{user.full_name}** لتنبيهك في الجولة القادمة.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_rolelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📖 **الأدوار الـ 17 المعتمدة حصرياً في البوت:**\n"]
    for r in ROLES.values():
        lines.append(f"{r.emoji} **{r.name_ar}**:\n{r.description}\n")
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
        f"📊 **إحصائيات اللاعب: {row['full_name']}**\n"
        f"🎮 المباريات الملعوبة: **{played}**\n"
        f"🏆 انتصارات الفرق: **{won}** ({rate}%)\n"
        f"🛡️ مرات النجاة: **{row['games_survived']}**"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = DB.get_leaderboard(10)
    if not rows:
        await update.message.reply_text("📊 لا توجد بيانات مسجلة بعد.")
        return
    lines = ["🏆 **لوحة صدارة أبطال القرية:**\n"]
    for idx, r in enumerate(rows, start=1):
        lines.append(
            f"{idx}. **{r['full_name']}** — 🏆 {r['games_won']} فوز | 🎮 {r['games_played']} مباراة"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# =====================================================================
# 12. نقطة تشغيل البوت (Main Entrypoint)
# =====================================================================
def main() -> None:
    DB.init_db()
    logger.info("تم تهيئة قاعدة البيانات المحلية SQLite بنجاح: %s", DB_PATH)

    if BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.warning("يرجى وضع التوكن الخاص ببوتك في BOT_TOKEN لتشغيل البوت.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

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

    logger.info("🚀 بوت لعبة الذئب يعمل الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
