# yamada

A Telegram bot that learns from chat messages and generates new sentences using Markov chains.

## Features

- Learns from messages in group chats (opt-in)
- Generates sentences with `/markov [seed]`
- Configurable auto-reply percentage
- Multiple named learning sessions per chat
- Per-user data consent and deletion (`/deleteme`)
- Admin controls for managing sessions and users

## Setup

1. Copy `.env.example` to `.env` and fill in the values:
   ```
   BOT_TOKEN=your_bot_token
   ADMIN_ID=your_telegram_user_id
   KEEP_LAST=1500
   LOGGING_ENABLED=true
   ```

2. Run with Docker Compose:
   ```bash
   docker compose up -d
   ```

## Commands

| Command | Description |
|---|---|
| `/markov [seed]` | Generate a sentence |
| `/enable` / `/disable` | Toggle learning in the chat |
| `/percentage [0-100]` | Set auto-reply rate |
| `/settings` | Edit chat settings (admins) |
| `/sessions` | Manage learning sessions (admins) |
| `/deleteme confirm` | Delete your data (PM only) |
| `/privacy` | View the privacy policy |
