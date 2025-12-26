"""FastAPI application."""
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from api.routes import api_keys_v2 as api_keys, proxy, balance_v2 as balance, payment, webhooks, webapp, auth, chart2csv, masker
from api.dependencies import get_payment_engine
from api.db.base import AsyncSessionLocal, DebugLog
from api.context import request_id_var, ip_address_var, user_agent_var, account_id_var, opt_in_debug_var

# Configure Structured Logging
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "request_id"):
            log_obj["request_id"] = record.request_id
        return json.dumps(log_obj)

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)

# Webapp path for serving static files
webapp_path = "/app/webapp"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    if os.path.exists(webapp_path):
        logger.info(f"Webapp available at: {webapp_path}")
        files = os.listdir(webapp_path)
        logger.info(f"Webapp files: {files}")
    else:
        logger.warning(f"Webapp directory not found at: {webapp_path}")
    
    # Initialize Brevo email service
    from config.settings import BREVO_API_KEY
    if BREVO_API_KEY:
        from api.services.email_service import configure_brevo
        configure_brevo(BREVO_API_KEY)
        logger.info("Brevo email service configured")
    else:
        logger.warning("BREVO_API_KEY not set, email sending disabled")

    yield

    # Shutdown (if needed)
    logger.info("Application shutting down")


app = FastAPI(
    title="KikuAI Bot API",
    description="Backend API for KikuAI Telegram bot",
    version="0.1.0",
    lifespan=lifespan,
)

# Standardized Error Handler
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    # Extract error_code if provided as second argument
    code = "VALIDATION_ERROR"
    message = str(exc)
    if len(exc.args) > 1:
        message, code = exc.args[0], exc.args[1]
    
    return Response(
        content=json.dumps({
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id_var.get()
            }
        }),
        status_code=400 if code != "BALANCE_EXHAUSTED" else 402,
        media_type="application/json"
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global error: {exc}", extra={"request_id": request_id_var.get()})
    return Response(
        content=json.dumps({
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred",
                "request_id": request_id_var.get()
            }
        }),
        status_code=500,
        media_type="application/json"
    )

# Request Trace Middleware
class RequestTraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        
        # Tokenize and set context
        token_rid = request_id_var.set(request_id)
        token_ip = ip_address_var.set(request.client.host if request.client else None)
        token_ua = user_agent_var.set(request.headers.get("user-agent"))
        
        logger.info(f"Incoming {request.method} {request.url.path}", extra={"request_id": request_id})
        
        # Capture request body for debug if needed
        request_body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            body_bytes = await request.body()
            request_body = body_bytes.decode(errors="ignore")
            # Must re-set body for downstream consumers
            request._body = body_bytes

        try:
            response: Response = await call_next(request)
            
            # If opt-in debug is enabled, capture response and log
            if opt_in_debug_var.get() and account_id_var.get():
                response_body = b""
                async for chunk in response.body_iterator:
                    response_body += chunk
                
                # Re-wrap response
                response = Response(
                    content=response_body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type
                )
                
                # Write debug log in background (simple async call here for brevity, or use BackgroundTasks if preferred)
                await self._log_debug(
                    account_id=account_id_var.get(),
                    request_id=request_id,
                    path=request.url.path,
                    method=request.method,
                    request_body=request_body,
                    response_body=response_body.decode(errors="ignore"),
                    status_code=response.status_code
                )

            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # Reset context
            request_id_var.reset(token_rid)
            ip_address_var.reset(token_ip)
            user_agent_var.reset(token_ua)

    async def _log_debug(self, **kwargs):
        """Async write to debug logs."""
        try:
            async with AsyncSessionLocal() as session:
                log = DebugLog(**kwargs)
                session.add(log)
                await session.commit()
        except Exception as e:
            logger.error(f"Failed to write DebugLog: {e}")

app.add_middleware(RequestTraceMiddleware)

# CORS middleware - configure allowed origins from environment
ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "https://kikuai.dev,https://bot.kikuai.dev").split(",")
if os.getenv("ENVIRONMENT", "development") == "development":
    ALLOWED_ORIGINS = ["*"]  # Allow all in development

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"]
)

# Initialize shared Payment Engine (singleton from dependencies)
payment_engine = get_payment_engine()

# Set payment engine in routes
payment.set_payment_engine(payment_engine)
webhooks.set_payment_engine(payment_engine)

# Direct routes for webapp files (BEFORE routers to ensure they work)
@app.get("/webapp/dashboard.html")
async def dashboard_html():
    """Serve dashboard.html"""
    file_path = os.path.join(webapp_path, "dashboard.html")
    logger.info(f"Dashboard request: checking {file_path}, exists={os.path.exists(file_path)}")
    if os.path.exists(file_path):
        logger.info(f"Serving dashboard.html from {file_path}")
        return FileResponse(file_path, media_type="text/html")
    logger.error(f"dashboard.html not found at: {file_path}")
    raise HTTPException(status_code=404, detail=f"File not found at {file_path}")

@app.get("/webapp/manage_keys.html")
async def manage_keys_html():
    """Serve manage_keys.html"""
    file_path = os.path.join(webapp_path, "manage_keys.html")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/webapp/payment.html")
async def payment_html():
    """Serve payment.html"""
    file_path = os.path.join(webapp_path, "payment.html")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="File not found")

# Note: We use direct routes instead of mount to have more control
# Mount would intercept all /webapp/* requests before our specific routes

# Register routers (AFTER webapp routes)
app.include_router(api_keys.router)
app.include_router(proxy.router)
app.include_router(balance.router)
app.include_router(payment.router)
app.include_router(webhooks.router)
app.include_router(webapp.router)
app.include_router(auth.router)
app.include_router(chart2csv.router)
app.include_router(masker.router)

# Additional webhook mount to support /api/webhooks/paddle (without /v1)
app.add_api_route(
    "/api/webhooks/paddle",
    webhooks.handle_paddle_webhook,
    methods=["POST"],
)
app.add_api_route(
    "/api/webhooks/telegram_stars",
    webhooks.handle_telegram_stars_webhook,
    methods=["POST"],
)

@app.get("/healthz")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


