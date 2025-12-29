"""Application settings."""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL")

# Database
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/kiku-bot")

# Products
RELIAPI_URL = os.getenv("RELIAPI_URL", "https://reliapi.kikuai.dev")

# Web App
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://kikuai.dev/webapp")

# Paddle
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY")
PADDLE_VENDOR_ID = os.getenv("PADDLE_VENDOR_ID")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET")
PADDLE_ENVIRONMENT = os.getenv("PADDLE_ENVIRONMENT", "sandbox")  # sandbox or production

# Security
SERVER_SECRET = os.getenv("SERVER_SECRET", "kiku-dev-secret-change-me-in-prod")

# Email (Brevo)
BREVO_API_KEY = os.getenv("BREVO_API_KEY")

# Credits System
CREDITS_PER_USD = 1000  # 1000 credits = $1 USD
CREDITS_DISPLAY_NAME = "credits"

# Free Tier Settings
FREE_TIER_REQUIRES_EMAIL_VERIFICATION = True
FREE_TIER_PROGRESSIVE_DAYS = 7  # First week = 50% limits
