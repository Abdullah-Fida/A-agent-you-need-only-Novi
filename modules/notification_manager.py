"""
Notification Manager Module — COMPREHENSIVE BUILD.
Handles sending critical alerts and system updates to the admin via rich HTML Email.
Sends email confirmation for EVERY event: errors, posts, module status, strategy changes,
connection status, and speed/volume adjustments.
"""
import logging
import smtplib
import asyncio
import traceback
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
    def __init__(self, sender_email: str, app_password: str, receiver_email: str,
                 resend_api_key: str = "", whatsapp_phone: str = "", whatsapp_api_key: str = "", db=None):
        self.sender_email = sender_email
        self.app_password = app_password
        self.resend_api_key = resend_api_key
        # If receiver is empty, send to self
        self.receiver_email = receiver_email if receiver_email else sender_email
        self.whatsapp_phone = whatsapp_phone
        self.whatsapp_api_key = whatsapp_api_key
        self.db = db

        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 587

        if not self.sender_email and not self.resend_api_key:
            logger.warning("Email credentials missing. Notifications will only be logged locally.")
        else:
            logger.info(f"NotificationManager ready. Receiver: {self.receiver_email} (Using {'Resend API' if self.resend_api_key else 'SMTP'})")

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
        Synchronous email send. Prioritizes Resend API (HTTP port 443, allowed on Render Free).
        Falls back to SMTP if Resend is not configured.
        """
        try:
            if self.resend_api_key:
                import requests
                headers = {
                    "Authorization": f"Bearer {self.resend_api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "from": "NOVI Bot <onboarding@resend.dev>",
                    "to": self.receiver_email,
                    "subject": full_subject,
                    "html": html_body
                }
                resp = requests.post("https://api.resend.com/emails", json=payload, headers=headers, timeout=15)
                if resp.status_code in (200, 201):
                    logger.info(f"Email sent successfully via Resend to {self.receiver_email}")
                    return True
                else:
                    logger.error(f"Resend API Failed: {resp.status_code} {resp.text}")
                    return False
            else:
                msg = MIMEMultipart("alternative")
                msg['From'] = f"NOVI Bot <{self.sender_email}>"
                msg['To'] = self.receiver_email
                msg['Subject'] = full_subject

                msg.attach(MIMEText(html_body, 'html'))

                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15)
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.sender_email, self.app_password)
                server.send_message(msg)
                server.quit()

                logger.info(f"Email sent successfully via SMTP to {self.receiver_email}")
                return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP Authentication Failed — check your App Password: {e}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP Error: {e}")
            return False
        except Exception as e:
            logger.error(f"Email send failed: {type(e).__name__}: {e}")
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

        email_success = False

        # Send Email (non-blocking via thread)
        if self.resend_api_key or (self.sender_email and self.app_password):
            email_success = await asyncio.to_thread(self._send_email_sync, full_subject, html)

        return email_success

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

        if self.resend_api_key or (self.sender_email and self.app_password):
            await asyncio.to_thread(self._send_email_sync, full_subject, html)

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

        if self.resend_api_key or (self.sender_email and self.app_password):
            await asyncio.to_thread(self._send_email_sync, full_subject, html)

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

        if self.resend_api_key or (self.sender_email and self.app_password):
            await asyncio.to_thread(self._send_email_sync, full_subject, html)

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

        if self.resend_api_key or (self.sender_email and self.app_password):
            await asyncio.to_thread(self._send_email_sync, full_subject, html)

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

        if self.resend_api_key or (self.sender_email and self.app_password):
            await asyncio.to_thread(self._send_email_sync, full_subject, html)

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

        if self.resend_api_key or (self.sender_email and self.app_password):
            await asyncio.to_thread(self._send_email_sync, full_subject, html)
