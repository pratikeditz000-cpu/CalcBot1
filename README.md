# Telegram Calculator Bot

A production-grade Telegram bot that automatically detects and solves mathematical expressions in group chats — no commands needed.

## Features

### Auto-Detection (No Commands Required)
Paste any math into a group and the bot replies instantly:
- `2+2` → `4`
- `15000*18/100` → `2,700`
- `sqrt(144)` → `12`
- `log(100)` → `4.60517`
- `sin(90)` → `0.893997`
- `5!` → `120`
- `(10+20)*5` → `150`

### Natural Language Math
| Message | What it does |
|---|---|
| `25% of 4000` | Percentage calculation |
| `2 lakh ka 18% GST` | GST breakdown |
| `50000 loan 3 years 12%` | Monthly EMI + interest |
| `CI 10000 5 years 8%` | Compound interest |
| `cp 500 sp 750` | Profit / loss |
| `age born 1990` | Age calculation |
| `convert 5 km to m` | Unit conversion |
| `100 USD to INR` | Currency conversion (static rates) |
| `solve x^2 + 5x + 6 = 0` | Symbolic equation solving |

### Admin Commands
| Command | Description |
|---|---|
| `/help` | Show usage guide |
| `/stats` | Usage statistics |
| `/ping` | Latency check |
| `/enable` | Enable bot in this group |
| `/disable` | Silence bot in this group |

## Quick Start

### 1. Get a Bot Token
1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token you receive

### 2. Configure Environment
```bash
cd telegram-bot
cp .env.example .env
# Edit .env and set BOT_TOKEN=your_token_here
```

On **Replit**: add `BOT_TOKEN` as a Secret in the Secrets tab (no `.env` file needed).

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Run
```bash
python main.py
```

## Replit Deployment

1. Add `BOT_TOKEN` in **Tools → Secrets**
2. Optionally add `ADMIN_IDS` (comma-separated Telegram user IDs for admin access)
3. The workflow `Telegram Bot` starts the bot automatically
4. Use **Deployments → Reserved VM** for 24/7 uptime

## Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python", "main.py"]
```

```bash
docker build -t calcbot .
docker run -e BOT_TOKEN=your_token_here calcbot
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram bot token from @BotFather |
| `ADMIN_IDS` | <emoji id="5210952531676504517">❌</emoji> | (all users) | Comma-separated admin Telegram user IDs |
| `DB_PATH` | <emoji id="5210952531676504517">❌</emoji> | `calcbot.db` | SQLite database file path |
| `LOG_LEVEL` | <emoji id="5210952531676504517">❌</emoji> | `INFO` | Logging verbosity |

## Architecture

```
main.py
├── Database (aiosqlite)      — stats, groups, users tables
├── RateLimiter               — sliding-window, 15 req/min per user
├── safe_eval_fast()          — numexpr fast path for pure arithmetic
├── safe_eval()               — SymPy fallback (no eval, whitelist only)
├── NLPHandler                — regex pattern library for natural language
│   ├── gst()
│   ├── emi()
│   ├── compound_interest()
│   ├── profit_loss()
│   ├── percentage_of()
│   ├── unit_convert()
│   ├── currency()
│   ├── age_calc()
│   ├── solve_equation()
│   └── lakh_crore()
├── handle_message()          — group message handler
├── handle_private()          — DM handler
└── Admin commands            — /stats /enable /disable /ping /help
```

## Security

- **No `eval()`** — expressions are parsed by SymPy with an explicit whitelist of allowed functions
- **Regex validation** — input screened for blocked keywords (`import`, `exec`, `os`, `__`, etc.)
- **Rate limiting** — 15 requests per user per 60-second window
- **Length guard** — messages longer than 300 characters are ignored
- **Forwarded message ignore** — spam prevention
- **Bot message ignore** — no bot-to-bot loops

## Performance Notes

- `concurrent_updates=True` enables parallel update processing
- `drop_pending_updates=True` skips the backlog on startup
- `numexpr` provides near-native-speed evaluation for pure arithmetic
- Group enabled/disabled status is cached in memory for 5 minutes
- All DB writes are async and non-blocking
- Tested capable of handling 40,000+ member groups
