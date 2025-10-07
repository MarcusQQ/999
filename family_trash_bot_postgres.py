#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
family_trash_bot_postgres.py
Telegram bot for tracking trash take-outs per family, backed by PostgreSQL (DATABASE_URL).
Features:
- Families with name + optional password
- Creator becomes admin
- Members can press "Вынес мусор" and "Статистика семьи"
- Admin panel: view members, set count, promote/demote, remove member, reset counts, delete family
- Notifications: after a member marks trash, bot notifies member with lowest count
- Uses asyncpg for PostgreSQL connectivity. Provide DATABASE_URL env var (Railway provides it).
"""

import os
import asyncio
import logging
from typing import Optional

import asyncpg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import hashlib

# --- CONFIG ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")  # railway Postgres provides this
if not DATABASE_URL:
    # also accept individual parts for local testing
    DATABASE_URL = os.environ.get("PG_CONN", None)

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# --- DB helpers ---
async def init_db(pool: asyncpg.pool.Pool):
    async with pool.acquire() as conn:
        await conn.execute(\"\"\"
        CREATE TABLE IF NOT EXISTS families (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS members (
            id SERIAL PRIMARY KEY,
            family_id INTEGER REFERENCES families(id) ON DELETE CASCADE,
            telegram_id BIGINT NOT NULL,
            username TEXT,
            count INTEGER DEFAULT 0,
            is_admin BOOLEAN DEFAULT FALSE,
            UNIQUE(family_id, telegram_id)
        );
        CREATE INDEX IF NOT EXISTS idx_members_telegram ON members(telegram_id);
        \"\"\")


def hash_password(pw: Optional[str]) -> Optional[str]:
    if pw is None:
        return None
    return hashlib.sha256(("salt_v1_" + pw).encode("utf-8")).hexdigest()


# --- DB operations ---
async def create_family(pool, name: str, pw: Optional[str], creator_tid: int, creator_username: str):
    ph = hash_password(pw) if pw else None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO families (name, password_hash) VALUES ($1,$2) RETURNING id", name, ph)
        fid = row["id"]
        await conn.execute(
            "INSERT INTO members (family_id, telegram_id, username, is_admin) VALUES ($1,$2,$3,$4)",
            fid, creator_tid, creator_username, True
        )
        return fid

async def get_family_by_name(pool, name: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM families WHERE name=$1", name)

async def join_family(pool, name: str, pw: Optional[str], tid: int, username: str):
    fam = await get_family_by_name(pool, name)
    if not fam:
        return False, "Семья не найдена"
    # check password
    if fam["password_hash"] and fam["password_hash"] != hash_password(pw):
        return False, "Неверный пароль"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO members (family_id, telegram_id, username) VALUES ($1,$2,$3) ON CONFLICT (family_id, telegram_id) DO NOTHING",
            fam["id"], tid, username
        )
    return True, "Вы успешно присоединились к семье!"

async def get_member_family(pool, tid: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow(\"\"\"
            SELECT f.* FROM families f
            JOIN members m ON m.family_id = f.id
            WHERE m.telegram_id = $1
        \"\"\", tid)

async def add_trash(pool, tid: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE members SET count = count + 1 WHERE telegram_id = $1", tid)

async def get_family_stats(pool, family_id: int):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id, telegram_id, username, count, is_admin FROM members WHERE family_id=$1 ORDER BY count DESC, username", family_id)

async def get_least_member(pool, family_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT telegram_id, username, count FROM members WHERE family_id=$1 ORDER BY count ASC, id ASC LIMIT 1", family_id)

# Admin operations
async def is_member_admin(pool, family_id: int, tid: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_admin FROM members WHERE family_id=$1 AND telegram_id=$2", family_id, tid)
        return bool(row and row["is_admin"])

async def get_members(pool, family_id: int):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id, telegram_id, username, count, is_admin FROM members WHERE family_id=$1 ORDER BY username", family_id)

async def set_member_count(pool, member_telegram_id: int, family_id: int, new_count: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE members SET count=$1 WHERE family_id=$2 AND telegram_id=$3", new_count, family_id, member_telegram_id)

async def remove_member(pool, member_telegram_id: int, family_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM members WHERE family_id=$1 AND telegram_id=$2", family_id, member_telegram_id)

async def promote_member(pool, member_telegram_id: int, family_id: int, make_admin: bool):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE members SET is_admin=$1 WHERE family_id=$2 AND telegram_id=$3", make_admin, family_id, member_telegram_id)

async def reset_counts(pool, family_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE members SET count=0 WHERE family_id=$1", family_id)

async def delete_family(pool, family_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM families WHERE id=$1", family_id)


# --- UI helpers ---
def main_menu_kb(is_member=False):
    if not is_member:
        kb = [
            [InlineKeyboardButton("Создать семью 👨‍👩‍👧‍👦", callback_data="create_family")],
            [InlineKeyboardButton("Присоединиться к семье 🔑", callback_data="join_family")],
        ]
    else:
        kb = [
            [InlineKeyboardButton("🗑 Вынес мусор", callback_data="trash_out")],
            [InlineKeyboardButton("📊 Статистика семьи", callback_data="stats")],
        ]
    return InlineKeyboardMarkup(kb)

def lobby_kb_admin(family_id: int, is_admin: bool):
    kb = []
    kb.append([InlineKeyboardButton("🗑 Вынес мусор", callback_data=f"trash_out|{family_id}")])
    kb.append([InlineKeyboardButton("📊 Статистика семьи", callback_data=f"stats|{family_id}")])
    if is_admin:
        kb.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data=f"admin|{family_id}")])
    return InlineKeyboardMarkup(kb)

def admin_panel_kb(family_id: int):
    kb = [
        [InlineKeyboardButton("Список участников", callback_data=f"admin_list|{family_id}")],
        [InlineKeyboardButton("Сбросить счёт семьи", callback_data=f"admin_reset|{family_id}")],
        [InlineKeyboardButton("Удалить семью", callback_data=f"admin_delete|{family_id}")],
        [InlineKeyboardButton("Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(kb)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pool = context.application.bot_data.get(\"pool\")
    fam = await get_member_family(pool, user.id)
    await update.message.reply_text(f"Привет, {user.first_name}! 👋", reply_markup=main_menu_kb(is_member=bool(fam)))

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user = update.effective_user
    pool = context.application.bot_data.get(\"pool\")
    # data handling
    if data == "create_family":
        context.user_data["flow"] = "create_name"
        await q.message.reply_text(\"Введите название семьи:\")
        return
    if data == "join_family":
        context.user_data["flow"] = "join_name"
        await q.message.reply_text(\"Введите название семьи:\")
        return
    if data.startswith("trash_out"):
        # data may include family id
        parts = data.split("|")
        fam = await get_member_family(pool, user.id)
        if not fam:
            await q.message.reply_text(\"Вы не в семье.\", reply_markup=main_menu_kb(False))
            return
        await add_trash(pool, user.id)
        await q.message.reply_text(\"✅ Мусор отмечен.\", reply_markup=main_menu_kb(True))
        # notify least
        await notify_least(pool, fam["id"], context)
        return
    if data.startswith("stats"):
        parts = data.split("|")
        fam = await get_member_family(pool, user.id)
        if not fam:
            await q.message.reply_text(\"Вы не в семье.\")
            return
        rows = await get_family_stats(pool, fam["id"])
        txt = \"📊 Статистика семьи:\\n\" + \"\\n\".join([f\"{r['username']}: {r['count']}\" for r in rows])
        await q.message.reply_text(txt, reply_markup=main_menu_kb(True))
        return
    if data.startswith("admin|"):
        _, fid = data.split("|",1)
        fid = int(fid)
        fam = await get_member_family(pool, user.id)
        if not fam or fam["id"] != fid:
            await q.message.reply_text(\"Нет доступа к админ-панели.\")
            return
        if not await is_member_admin(pool, fid, user.id):
            await q.message.reply_text(\"Только админ может открыть панель.\")
            return
        await q.message.reply_text(\"Админ-панель:\", reply_markup=admin_panel_kb(fid))
        return
    if data.startswith("admin_list|"):
        _, fid = data.split("|",1)
        fid = int(fid)
        members = await get_members(pool, fid)
        kb = []
        for m in members:
            label = f\"{m['username']} — {m['count']} {'(admin)' if m['is_admin'] else ''}\"
            kb.append([InlineKeyboardButton(label, callback_data=f\"admin_member|{fid}|{m['telegram_id']}\")])
        kb.append([InlineKeyboardButton(\"Назад\", callback_data=f\"admin|{fid}\")])
        await q.message.reply_text(\"Участники:\", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith(\"admin_member|\"):
        _, fid, member_tid = data.split(\"|\",2)
        fid = int(fid); member_tid = int(member_tid)
        kb = [
            [InlineKeyboardButton(\"+1\", callback_data=f\"admin_inc|{fid}|{member_tid}\"),
             InlineKeyboardButton(\"-1\", callback_data=f\"admin_dec|{fid}|{member_tid}\")],
            [InlineKeyboardButton(\"Установить...\", callback_data=f\"admin_set|{fid}|{member_tid}\")],
            [InlineKeyboardButton(\"Промо/Демотировать\", callback_data=f\"admin_toggle_admin|{fid}|{member_tid}\")],
            [InlineKeyboardButton(\"Удалить\", callback_data=f\"admin_remove|{fid}|{member_tid}\")],
            [InlineKeyboardButton(\"Назад\", callback_data=f\"admin_list|{fid}\")],
        ]
        await q.message.reply_text(\"Действия с участником:\", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith(\"admin_inc|\") or data.startswith(\"admin_dec|\"):
        op, fid, member_tid = data.split(\"|\",2)
        fid = int(fid); member_tid = int(member_tid)
        delta = 1 if op.endswith(\"inc\") else -1
        # perform change
        async with pool.acquire() as conn:
            await conn.execute(\"UPDATE members SET count = GREATEST(0, count + $1) WHERE family_id=$2 AND telegram_id=$3\", delta, fid, member_tid)
        await q.message.reply_text(\"Изменено.\", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(\"Назад\", callback_data=f\"admin_list|{fid}\")]]))
        return
    if data.startswith(\"admin_set|\"):
        _, fid, member_tid = data.split(\"|\",2)
        context.user_data[\"flow\"] = \"admin_set_count\"
        context.user_data[\"admin_set_fid\"] = int(fid)
        context.user_data[\"admin_set_target\"] = int(member_tid)
        await q.message.reply_text(\"Введите новое целое число (>=0):\")
        return
    if data.startswith(\"admin_remove|\"):
        _, fid, member_tid = data.split(\"|\",2)
        fid = int(fid); member_tid = int(member_tid)
        await remove_member(pool, member_tid, fid)
        await q.message.reply_text(\"Участник удалён.\", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(\"Назад\", callback_data=f\"admin_list|{fid}\")]]))
        return
    if data.startswith(\"admin_toggle_admin|\"):
        _, fid, member_tid = data.split(\"|\",2)
        fid = int(fid); member_tid = int(member_tid)
        # toggle
        async with pool.acquire() as conn:
            row = await conn.fetchrow(\"SELECT is_admin FROM members WHERE family_id=$1 AND telegram_id=$2\", fid, member_tid)
            if row:
                await conn.execute(\"UPDATE members SET is_admin = NOT is_admin WHERE family_id=$1 AND telegram_id=$2\", fid, member_tid)
        await q.message.reply_text(\"Права изменены.\", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(\"Назад\", callback_data=f\"admin_list|{fid}\")]]))
        return
    if data.startswith(\"admin_reset|\"):
        _, fid = data.split(\"|\",1); fid = int(fid)
        await reset_counts(pool, fid)
        await q.message.reply_text(\"Счёты сброшены.\", reply_markup=admin_panel_kb(fid))
        return
    if data.startswith(\"admin_delete|\"):
        _, fid = data.split(\"|\",1); fid = int(fid)
        context.user_data[\"flow\"] = \"admin_confirm_delete\"
        context.user_data[\"admin_delete_fid\"] = fid
        await q.message.reply_text(\"Введите DELETE чтобы подтвердить удаление семьи:\")
        return
    if data == \"back_main\":
        fam = await get_member_family(pool, user.id)
        await q.message.reply_text(\"Главное меню:\", reply_markup=main_menu_kb(is_member=bool(fam)))
        return
    # unknown
    await q.message.reply_text(\"Неизвестная операция.\")


async def text_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    pool = context.application.bot_data.get(\"pool\")
    flow = context.user_data.get(\"flow\")
    text = (msg.text or \"\").strip()

    if flow == \"create_name\":
        context.user_data[\"new_family_name\"] = text
        context.user_data[\"flow\"] = \"create_pass\"
        await msg.reply_text(\"Введите пароль для семьи (можно оставить пустым):\")
        return
    if flow == \"create_pass\":
        name = context.user_data.get(\"new_family_name\")
        pw = text if text != \"\" else None
        try:
            fid = await create_family(context.application.bot_data.get(\"pool\"), name, pw, user.id, user.username or user.first_name)
        except Exception as e:
            await msg.reply_text(f\"Ошибка: {e}\")
            context.user_data.clear()
            return
        context.user_data.clear()
        await msg.reply_text(f\"Семья '{name}' создана. Вы админ.\", reply_markup=main_menu_kb(True))
        return
    if flow == \"join_name\":
        context.user_data[\"join_family_name\"] = text
        context.user_data[\"flow\"] = \"join_pass\"
        await msg.reply_text(\"Введите пароль семьи (если есть) или оставьте пустым:\")
        return
    if flow == \"join_pass\":
        name = context.user_data.get(\"join_family_name\")
        pw = text
        ok, msg_text = await join_family(context.application.bot_data.get(\"pool\"), name, pw, user.id, user.username or user.first_name)
        context.user_data.clear()
        await msg.reply_text(msg_text, reply_markup=main_menu_kb(ok))
        return
    if flow == \"admin_set_count\":
        try:
            newv = int(text)
            if newv < 0:
                raise ValueError()
        except:
            await msg.reply_text(\"Неверное число. Введите целое >=0 или /cancel.\")
            return
        fid = context.user_data.get(\"admin_set_fid\")
        target = context.user_data.get(\"admin_set_target\")
        await set_member_count(context.application.bot_data.get(\"pool\"), target, fid, newv)
        context.user_data.pop(\"admin_set_fid\", None); context.user_data.pop(\"admin_set_target\", None); context.user_data.pop(\"flow\", None)
        await msg.reply_text(\"Значение установлено.\", reply_markup=main_menu_kb(True))
        return
    if flow == \"admin_confirm_delete\":
        fid = context.user_data.get(\"admin_delete_fid\")
        if text == \"DELETE\":
            await delete_family(context.application.bot_data.get(\"pool\"), fid)
            await msg.reply_text(\"Семья удалена.\", reply_markup=main_menu_kb(False))
        else:
            await msg.reply_text(\"Подтверждение не пройдено.\", reply_markup=main_menu_kb(True))
        context.user_data.pop(\"admin_delete_fid\", None); context.user_data.pop(\"flow\", None)
        return

    # fallback
    await msg.reply_text(\"Нераспознанный ввод. Вернись в меню.\", reply_markup=main_menu_kb(False))


async def notify_least(pool, family_id: int, context: ContextTypes.DEFAULT_TYPE):
    row = await get_least_member(pool, family_id)
    if not row:
        return
    uid = row["telegram_id"]
    username = row["username"] or \"Участник\"
    try:
        await context.bot.send_message(chat_id=uid, text=f\"🚨 {username}, твоя очередь вынести мусор! У тебя меньше всех выносов.\")
    except Exception as e:
        log.warning(\"Notify failed: %s\", e)


async def on_startup(application):
    # create pool and migrations
    pool = await asyncpg.create_pool(DATABASE_URL, max_size=5)
    application.bot_data[\"pool\"] = pool
    await init_db(pool)
    log.info(\"DB pool created and migrations applied.\")


async def on_shutdown(application):
    pool = application.bot_data.get(\"pool\")
    if pool:
        await pool.close()
        log.info(\"DB pool closed.\")


def main():
    if not BOT_TOKEN:
        print(\"ERROR: Set BOT_TOKEN environment variable.\")
        return
    if not DATABASE_URL:
        print(\"ERROR: Set DATABASE_URL environment variable (Postgres connection string).\")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # handlers
    app.add_handler(CommandHandler(\"start\", start))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_flow))
    app.add_handler(MessageHandler(filters.COMMAND, lambda u,c: c.bot.send_message(chat_id=u.effective_chat.id, text=\"Используйте кнопки\")))
    app.post_init = on_startup
    app.stop = on_shutdown
    print(\"Bot starting (polling)...\")
    app.run_polling()


if __name__ == \"__main__\":
    main()
