Family Trash Bot (Postgres) - README

This package contains a Telegram bot that uses PostgreSQL for storage.
Files:
- family_trash_bot_postgres.py  - main bot code (async, uses asyncpg)
- requirements.txt
- Procfile

How to deploy on Railway:
1. Create a Postgres plugin in your Railway project (Provision > PostgreSQL), copy the DATABASE_URL variable to project variables.
2. Add BOT_TOKEN variable with your bot token.
3. Push code to GitHub and connect to Railway, or upload directly.
4. Railway will read requirements.txt and install packages; Procfile starts the bot.
5. Check logs; on startup the bot will run migrations to create tables.

Notes:
- Creator of a family becomes admin and can manage members from the Admin panel (accessible via buttons).
- DATABASE_URL must be set in Railway Variables (it is provided by Railway Postgres plugin).
