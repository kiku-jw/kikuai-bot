# KikuAI Bot

Telegram bot for managing and paying for KikuAI API products. 
Built with AI-first principles, high security, and seamless user experience.

## Features

- 🔑 **API Key Management**: Create and manage your KikuAI access keys.
- 💰 **Unified Balance**: Pay-as-you-go pricing across all products.
- 💳 **Paddle Integration**: Support for Cards, PayPal, and Apple Pay/Google Pay via Paddle.
- 🌟 **Telegram Stars**: Native Telegram crypto-payments for the best UX.
- 📊 **Usage Tracking**: Monitor your API consumption and balance in real-time.
- 🛡️ **Rate Limiting**: Protected by robust rate limiting and resilience logic.
- 🔔 **Notifications**: Real-time updates for successful top-ups and balance alerts.

## Quick Start

### Local Development

1. **Setup Environment:**
   ```bash
   cp .env.example .env
   # Edit .env and add your TELEGRAM_BOT_TOKEN and PADDLE keys
   ```

2. **Run Services:**
   ```bash
   docker-compose up --build
   ```

3. **Verify:**
   - Bot is polling
   - API is running at `http://localhost:8000`
   - Health check: `http://localhost:8000/healthz`

## Project Structure

- `api/`: FastAPI backend handling webhooks, payments, and proxy.
- `bot/`: aiogram-based Telegram bot implementation.
- `webapp/`: Telegram Web Apps for premium payment UI.
- `tests/`: Comprehensive test suite (42+ unit and resilience tests).
- `docs/`: In-depth documentation for for devs and ops.

## Documentation Index

- 📁 [SETUP.md](SETUP.md) - Initial setup and quick start guide.
- 📁 [DEPLOY.md](DEPLOY.md) - Production deployment (Docker, Hetzner, Nginx).
- 📁 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - System design and data flows.
- 📁 [docs/PADDLE_INTEGRATION.md](docs/PADDLE_INTEGRATION.md) - Paddle setup and webhooks.
- 📁 [docs/TELEGRAM_STARS_INTEGRATION.md](docs/TELEGRAM_STARS_INTEGRATION.md) - Telegram Stars details.
- 📁 [docs/PAYMENT_FLOW.md](docs/PAYMENT_FLOW.md) - Detailed financial logic.
- 📁 [docs/API_SPEC.md](docs/API_SPEC.md) - API endpoints and models.
- 📁 [docs/SECURITY.md](docs/SECURITY.md) - Security measures and validation.
- 📁 [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) - Manual testing checklist.

## Bot Commands

- `/start` - Create account and get started.
- `/balance` - Check your USD balance.
- `/topup` - Open payment menu (Paddle or Stars).
- `/api_key` - Retrieve your active API key.
- `/usage` - View usage stats for the current period.

## Tech Stack

- **Python 3.11** (FastAPI, aiogram)
- **Redis** (Balance management & caching)
- **Prometheus** (Monitoring)
- **Httpx** (Resilient API communications)

---
*Created by Antigravity (Google DeepMind).*

