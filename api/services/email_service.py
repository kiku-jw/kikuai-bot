"""Brevo Email Service for sending transactional emails."""

import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Brevo API configuration (will be set from settings)
BREVO_API_KEY: Optional[str] = None
FROM_EMAIL = "noreply@kikuai.dev"
FROM_NAME = "KikuAI"


def configure_brevo(api_key: str):
    """Configure Brevo with API key."""
    global BREVO_API_KEY
    BREVO_API_KEY = api_key


async def send_magic_link_email(to_email: str, magic_link: str) -> bool:
    """Send magic link email using Brevo API.
    
    Args:
        to_email: Recipient email address
        magic_link: The magic link URL
        
    Returns:
        True if email was sent successfully, False otherwise
    """
    if not BREVO_API_KEY:
        logger.error("Brevo API key not configured, cannot send email")
        return False
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "api-key": BREVO_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
                    "to": [{"email": to_email}],
                    "subject": "Your KikuAI Login Link",
                    "htmlContent": f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #ffffff; padding: 40px; }}
        .container {{ max-width: 480px; margin: 0 auto; background: #111; border-radius: 8px; padding: 32px; border: 1px solid #222; }}
        .logo {{ font-size: 24px; font-weight: bold; margin-bottom: 24px; }}
        .logo span {{ color: #10b981; }}
        h1 {{ font-size: 20px; margin-bottom: 16px; }}
        p {{ color: #888; line-height: 1.6; }}
        .button {{ display: inline-block; background: #10b981; color: #000; padding: 12px 24px; border-radius: 4px; text-decoration: none; font-weight: 500; margin: 24px 0; }}
        .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #222; color: #666; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">Kiku<span>AI</span></div>
        <h1>Login to Your Account</h1>
        <p>Click the button below to sign in to your KikuAI dashboard. This link will expire in 15 minutes.</p>
        <a href="{magic_link}" class="button">Sign In to KikuAI</a>
        <p>If you didn't request this login link, you can safely ignore this email.</p>
        <div class="footer">
            <p>This link can only be used once and expires in 15 minutes.</p>
            <p>© 2025 KikuAI OÜ. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
""",
                    "textContent": f"""
KikuAI Login

Click the link below to sign in to your KikuAI dashboard:
{magic_link}

This link will expire in 15 minutes.

If you didn't request this login link, you can safely ignore this email.

© 2025 KikuAI OÜ
""",
                },
                timeout=10.0,
            )
            
            if response.status_code in (200, 201, 202):
                logger.info(f"Magic link email sent to {to_email}")
                return True
            else:
                logger.error(f"Brevo API error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        logger.error(f"Failed to send magic link email: {e}")
        return False
