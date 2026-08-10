"""
Notification Manager Module — COMPREHENSIVE BUILD.
Handles sending critical alerts and system updates to the admin via rich HTML Email.
Sends email confirmation for EVERY event: errors, posts, module status, strategy changes,
connection status, and speed/volume adjustments.
"""
import logging
import os
import smtplib
import asyncio
import time
import traceback
from collections import deque
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("OmniBot.NotificationManager")

# Pakistan Standard Time offset (UTC+5)
PKT_OFFSET = timedelta(hours=5)


def _get_pkt_now_str() -> str:
    """Returns current PKT time as a formatted string."""
    pkt = datetime.now(timezone.utc) + PKT_OFFSET
    return pkt.strftime('%I:%M %p PKT — %b %d, %Y')


class NotificationManager:
    """
    Sends rich HTML email notifications using Gmail SMTP.
    Every action NOVI takes is reported via email to keep the admin fully informed.
    Requires a Gmail App Password (not your regular password).
    """
    MAX_SEND_ATTEMPTS = 3

    def __init__(self, sender_email: str, app_password: str, receiver_email: str,
                 resend_api_key: str = "", db=None, from_address: str = ""):
        self.sender_email = sender_email
        self.app_password = app_password
        self.resend_api_key = resend_api_key
        # If receiver is empty, send to self
        self.receiver_email = receiver_email if receiver_email else sender_email
        self.db = db

        # Resend's shared sender only delivers to the account owner's address.
        # Override with a verified domain sender once you have one.
        self.from_address = from_address or os.environ.get(
            "RESEND_FROM", "NOVI Bot <onboarding@resend.dev>")

        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 587

        # Notifications that could not be delivered, exposed via /api/health
        # so a silent email outage is visible instead of invisible.
        self._failed_sends: deque = deque(maxlen=50)
        self.sent_count = 0

        if not self.receiver_email:
            logger.error("No receiver email configured — you will NOT receive any notifications.")
        elif not self.resend_api_key and not (self.sender_email and self.app_password):
            logger.warning("Email credentials missing. Notifications will only be logged locally.")
        else:
            transports = []
            if self.resend_api_key:
                transports.append("Resend API")
            if self.sender_email and self.app_password:
                transports.append("Gmail SMTP")
            logger.info(f"NotificationManager ready. Receiver: {self.receiver_email} "
                        f"(transports: {' -> '.join(transports)})")

    @property
    def health(self) -> dict:
        """Delivery health, surfaced on the dashboard."""
        return {
            "receiver": self.receiver_email,
            "resend_configured": bool(self.resend_api_key),
            "smtp_configured": bool(self.sender_email and self.app_password),
            "sent_ok": self.sent_count,
            "recent_failures": list(self._failed_sends)[-5:],
            "failure_count": len(self._failed_sends),
        }

    # ═══════════════════════════════════════════════════════════
    #  CORE EMAIL SENDER
    # ═══════════════════════════════════════════════════════════

    def _build_html_email(self, title: str, body_html: str, accent_color: str = "#4A90D9") -> str:
        """Builds a beautiful HTML email template."""
        return f"""
        <html>
        <body style="margin:0; padding:0; font-family: 'Segoe UI', Arial, sans-serif; background-color: #0d1117; color: #c9d1d9;">
            <div style="max-width: 600px; margin: 20px auto; background: #161b22; border-radius: 12px; border: 1px solid #30363d; overflow: hidden;">
                <!-- Header -->
                <div style="background: linear-gradient(135deg, {accent_color}, #1a1a2e); padding: 24px 28px; border-bottom: 1px solid #30363d;">
                    <h1 style="margin:0; color: #ffffff; font-size: 18px; font-weight: 600; letter-spacing: 1px;">
                        🤖 NOVI — System Notification
                    </h1>
                    <p style="margin: 6px 0 0; color: rgba(255,255,255,0.6); font-size: 12px;">{_get_pkt_now_str()}</p>
                </div>
                <!-- Body -->
                <div style="padding: 28px;">
                    <h2 style="margin: 0 0 16px; color: #58a6ff; font-size: 16px; font-weight: 600;">{title}</h2>
                    {body_html}
                </div>
                <!-- Footer -->
                <div style="padding: 16px 28px; background: #0d1117; border-top: 1px solid #30363d; text-align: center;">
                    <p style="margin:0; color: #484f58; font-size: 11px;">NOVI — Omni-Channel Bot System • Automated Notification</p>
                </div>
            </div>
        </body>
        </html>
        """

    def _send_email_sync(self, full_subject: str, html_body: str) -> bool:
        """
        Synchronous email send with retry and automatic fallback.

        Delivery order:
          1. Resend API over HTTPS (works on Render free tier, which blocks SMTP ports)
          2. Gmail SMTP (works locally)

        Each transport is retried on transient failures, and if Resend fails
        entirely we fall through to SMTP rather than dropping the notification.
        Every send is also recorded in _failed_sends so nothing goes missing
        without a trace.
        """
        attempts = []

        if self.resend_api_key:
            if self._send_via_resend(full_subject, html_body, attempts):
                return True
            if self.sender_email and self.app_password:
                logger.warning("Resend failed — falling back to SMTP.")

        if self.sender_email and self.app_password:
            if self._send_via_smtp(full_subject, html_body, attempts):
                return True

        self._failed_sends.append({
            "subject": full_subject,
            "at": _get_pkt_now_str(),
            "errors": attempts,
        })
        logger.error(f"EMAIL NOT DELIVERED: '{full_subject}'. Attempts: {attempts}")
        return False

    def _send_via_resend(self, subject: str, html_body: str, attempts: list) -> bool:
        """Sends via the Resend HTTPS API, retrying transient failures."""
        import requests

        headers = {
            "Authorization": f"Bearer {self.resend_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "from": self.from_address,
            "to": self.receiver_email,
            "subject": subject,
            "html": html_body,
        }

        for attempt in range(self.MAX_SEND_ATTEMPTS):
            try:
                resp = requests.post("https://api.resend.com/emails",
                                     json=payload, headers=headers, timeout=20)
                if resp.status_code in (200, 201):
                    logger.info(f"Email sent via Resend to {self.receiver_email}")
                    return True

                body = (resp.text or "")[:300]
                attempts.append(f"resend:{resp.status_code}")

                # 4xx (bad key, unverified sender) will never succeed on retry
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    logger.error(f"Resend rejected the email ({resp.status_code}): {body}")
                    return False

                logger.warning(f"Resend transient failure {resp.status_code}, retrying...")
            except Exception as e:
                attempts.append(f"resend:{type(e).__name__}")
                logger.warning(f"Resend attempt {attempt + 1} failed: {type(e).__name__}")

            if attempt < self.MAX_SEND_ATTEMPTS - 1:
                time.sleep(2 ** attempt)

        return False

    def _send_via_smtp(self, subject: str, html_body: str, attempts: list) -> bool:
        """Sends via Gmail SMTP, retrying transient failures."""
        for attempt in range(self.MAX_SEND_ATTEMPTS):
            server = None
            try:
                msg = MIMEMultipart("alternative")
                msg['From'] = f"NOVI Bot <{self.sender_email}>"
                msg['To'] = self.receiver_email
                msg['Subject'] = subject
                msg.attach(MIMEText(html_body, 'html'))

                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=20)
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.sender_email, self.app_password)
                server.send_message(msg)
                logger.info(f"Email sent via SMTP to {self.receiver_email}")
                return True

            except smtplib.SMTPAuthenticationError as e:
                # Wrong app password — retrying cannot help
                attempts.append("smtp:auth")
                logger.error(f"SMTP authentication failed — check your Gmail App Password: {e}")
                return False
            except Exception as e:
                attempts.append(f"smtp:{type(e).__name__}")
                logger.warning(f"SMTP attempt {attempt + 1} failed: {type(e).__name__}")
                if attempt < self.MAX_SEND_ATTEMPTS - 1:
                    time.sleep(2 ** attempt)
            finally:
                if server is not None:
                    try:
                        server.quit()
                    except Exception:
                        pass

        return False

    async def _dispatch(self, full_subject: str, html: str) -> bool:
        """
        Single place every notification goes through.

        Runs the blocking send in a worker thread so the event loop keeps
        serving Telegram and the API while email is in flight, and never lets
        an email failure propagate into the caller — a broken inbox must not
        take down posting.
        """
        if not self.receiver_email:
            logger.warning(f"[NO RECEIVER] Would have emailed: {full_subject}")
            return False

        if not self.resend_api_key and not (self.sender_email and self.app_password):
            logger.warning(f"[NO EMAIL CONFIGURED] Would have emailed: {full_subject}")
            return False

        try:
            ok = await asyncio.to_thread(self._send_email_sync, full_subject, html)
            if ok:
                self.sent_count += 1
            return ok
        except Exception as e:
            logger.error(f"Notification dispatch crashed: {type(e).__name__}: {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    #  GENERIC NOTIFICATION (Backward Compatible)
    # ═══════════════════════════════════════════════════════════

    async def send_notification(self, subject: str, message: str, is_critical: bool = False):
        """
        Sends an email notification asynchronously (non-blocking).
        If is_critical is True, formats the email as an urgent alert.
        Returns True if email delivered successfully.
        """
        prefix = "🚨 [URGENT] " if is_critical else "ℹ️ [INFO] "
        full_subject = f"{prefix}Novi News — {subject}"
        accent = "#e74c3c" if is_critical else "#4A90D9"

        body_html = f'<p style="color: #c9d1d9; line-height: 1.7; font-size: 14px;">{message.replace(chr(10), "<br>")}</p>'
        html = self._build_html_email(subject, body_html, accent_color=accent)

        # Log to terminal
        log_msg = f"NOTIFICATION: {full_subject} | {message}"
        if is_critical:
            logger.error(log_msg)
        else:
            logger.info(log_msg)

        # Log to database
        if self.db:
            try:
                await self.db.log_alert(
                    level="CRITICAL" if is_critical else "INFO",
                    module="NotificationManager",
                    message=f"{subject}: {message}"
                )
            except Exception as e:
                logger.warning(f"Could not log alert to DB: {e}")

        # Send Email (non-blocking via thread, with retry + SMTP fallback)
        return await self._dispatch(full_subject, html)

    # ═══════════════════════════════════════════════════════════
    #  SPECIFIC EVENT NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════

    async def notify_post_success(self, title: str, category: str, channel: str, posts_today: int, total_max: int):
        """Sends email when a post is successfully published."""
        body = f"""
        <div style="background: #0d2818; border: 1px solid #238636; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
            <p style="color: #3fb950; font-weight: 600; margin: 0 0 8px;">✅ Post Published Successfully</p>
            <table style="width: 100%; color: #c9d1d9; font-size: 13px;">
                <tr><td style="padding: 4px 0; color: #8b949e;">Topic:</td><td style="padding: 4px 0;">{title}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">Category:</td><td style="padding: 4px 0;">{category}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">Channel:</td><td style="padding: 4px 0;">{channel}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">Posts Today:</td><td style="padding: 4px 0;">{posts_today} / {total_max}</td></tr>
            </table>
        </div>
        """
        subject = f"✅ Post Published — {title[:50]}"
        html = self._build_html_email("Post Published to Telegram", body, accent_color="#238636")
        full_subject = f"✅ [POST] Novi News — {subject}"

        await self._dispatch(full_subject, html)

    async def notify_error(self, module: str, error: Exception, auto_fixed: bool = False, fix_action: str = ""):
        """Sends email when any error occurs in any module."""
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        tb_str = "".join(tb[-3:])  # Last 3 lines of traceback

        status_color = "#f0ad4e" if auto_fixed else "#e74c3c"
        status_text = "Auto-Fixed" if auto_fixed else "REQUIRES ATTENTION"

        body = f"""
        <div style="background: #2d1b1b; border: 1px solid {status_color}; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
            <p style="color: {status_color}; font-weight: 600; margin: 0 0 8px;">⚠️ Error in {module} — {status_text}</p>
            <table style="width: 100%; color: #c9d1d9; font-size: 13px;">
                <tr><td style="padding: 4px 0; color: #8b949e;">Module:</td><td style="padding: 4px 0;">{module}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">Error:</td><td style="padding: 4px 0;">{type(error).__name__}: {str(error)[:200]}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">Status:</td><td style="padding: 4px 0;">{status_text}</td></tr>
                {"<tr><td style='padding: 4px 0; color: #8b949e;'>Fix Applied:</td><td style='padding: 4px 0;'>" + fix_action + "</td></tr>" if auto_fixed else ""}
            </table>
            <pre style="background: #161b22; color: #f85149; padding: 12px; border-radius: 6px; font-size: 11px; overflow-x: auto; margin-top: 12px;">{tb_str}</pre>
        </div>
        """
        prefix = "⚠️" if auto_fixed else "🚨"
        full_subject = f"{prefix} [ERROR] Novi News — {module}: {type(error).__name__}"
        html = self._build_html_email(f"Error in {module}", body, accent_color=status_color)

        await self._dispatch(full_subject, html)

    async def notify_module_status(self, module_name: str, status: str, details: str = ""):
        """Sends email when a module is turned ON/OFF or connected/disconnected."""
        is_active = status.lower() in ("active", "connected", "on", "activated")
        color = "#238636" if is_active else "#e74c3c"
        icon = "🟢" if is_active else "🔴"

        body = f"""
        <div style="background: {'#0d2818' if is_active else '#2d1b1b'}; border: 1px solid {color}; border-radius: 8px; padding: 16px;">
            <p style="color: {color}; font-weight: 600; margin: 0 0 8px;">{icon} {module_name}: {status.upper()}</p>
            <p style="color: #c9d1d9; font-size: 13px; margin: 0;">{details}</p>
        </div>
        """
        full_subject = f"{icon} [MODULE] Novi News — {module_name}: {status.upper()}"
        html = self._build_html_email(f"{module_name} Status Change", body, accent_color=color)

        await self._dispatch(full_subject, html)

    async def notify_strategy_change(self, change_type: str, old_value: str, new_value: str, reason: str = ""):
        """Sends email when NOVI changes its posting strategy or limits."""
        body = f"""
        <div style="background: #1a1a2e; border: 1px solid #6366f1; border-radius: 8px; padding: 16px;">
            <p style="color: #818cf8; font-weight: 600; margin: 0 0 8px;">📊 Strategy Updated — {change_type}</p>
            <table style="width: 100%; color: #c9d1d9; font-size: 13px;">
                <tr><td style="padding: 4px 0; color: #8b949e;">Change:</td><td style="padding: 4px 0;">{change_type}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">Previous:</td><td style="padding: 4px 0;">{old_value}</td></tr>
                <tr><td style="padding: 4px 0; color: #8b949e;">New:</td><td style="padding: 4px 0;">{new_value}</td></tr>
                {"<tr><td style='padding: 4px 0; color: #8b949e;'>Reason:</td><td style='padding: 4px 0;'>" + reason + "</td></tr>" if reason else ""}
            </table>
        </div>
        """
        full_subject = f"📊 [STRATEGY] Novi News — {change_type}: {old_value} → {new_value}"
        html = self._build_html_email("Strategy Update", body, accent_color="#6366f1")

        await self._dispatch(full_subject, html)

    async def notify_connection_status(self, service: str, connected: bool, details: str = ""):
        """Sends email when a connection (Telegram, StealthMarketer) changes status."""
        icon = "🔗" if connected else "🔌"
        status = "Connected" if connected else "DISCONNECTED"
        color = "#238636" if connected else "#e74c3c"

        body = f"""
        <div style="background: {'#0d2818' if connected else '#2d1b1b'}; border: 1px solid {color}; border-radius: 8px; padding: 16px;">
            <p style="color: {color}; font-weight: 600; margin: 0 0 8px;">{icon} {service}: {status}</p>
            <p style="color: #c9d1d9; font-size: 13px; margin: 0;">{details}</p>
        </div>
        """
        full_subject = f"{icon} [CONNECTION] Novi News — {service}: {status}"
        html = self._build_html_email(f"{service} Connection Status", body, accent_color=color)

        await self._dispatch(full_subject, html)

    async def notify_stealth_step(self, action: str, details: str = "", success: bool = True):
        """Sends email after every step the StealthMarketer takes."""
        icon = "✅" if success else "❌"
        color = "#238636" if success else "#e74c3c"

        body = f"""
        <div style="background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px;">
            <p style="color: {color}; font-weight: 600; margin: 0 0 8px;">{icon} Stealth Action: {action}</p>
            <p style="color: #c9d1d9; font-size: 13px; margin: 0;">{details}</p>
        </div>
        """
        full_subject = f"{icon} [STEALTH] Novi News — {action}"
        html = self._build_html_email(f"Stealth Marketer: {action}", body, accent_color=color)

        await self._dispatch(full_subject, html)
