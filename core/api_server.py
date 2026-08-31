"""
API Server Module (FastAPI)
Provides endpoints for the React Dashboard to interact with the OmniBot.
This handles "proper UI handling" for image generation and bot monitoring.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.image_generator import ImageGenerator

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    if hasattr(app.state, 'notification_manager'):
        nm = app.state.notification_manager
        try:
            # We must use the sync method directly in a thread to ensure it completes during shutdown
            import threading
            subject = "🚨 [URGENT] Novi News — Bot Status: Inactive / Sleeping"
            body = "<p>Render has sent a shutdown signal. The bot is going to sleep or shutting down.</p>"
            html = nm._build_html_email("Bot Offline", body, accent_color="#e74c3c")
            threading.Thread(target=nm._send_email_sync, args=(subject, html)).start()
        except Exception:
            pass

app = FastAPI(title="OmniBot Control API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles
import os

# Mount the generated images folder so React can display them
assets_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "generated_images")
os.makedirs(assets_dir, exist_ok=True)
app.mount("/generated_images", StaticFiles(directory=assets_dir), name="generated_images")

class ImageGenRequest(BaseModel):
    headline: str
    category: str = "default"
    source_credit: str = "Novi News"

class LimitUpdateRequest(BaseModel):
    limit_type: str
    new_value: int

@app.get("/api/status")
async def get_status():
    return {"status": "online", "message": "API Bridge is running"}

@app.get("/api/report")
async def get_report(request: Request):
    """Fetches real-time stats from the Brain."""
    if not hasattr(request.app.state, 'brain'):
        return {"status": "API standalone mode", "subscribers": 0, "posts_today": 0, "replies_today": 0, "max_posts": 0, "max_replies": 0}
        
    brain = request.app.state.brain
    status = ("killed" if brain.master_kill else
              "paused" if brain.is_paused else
              "sleeping" if brain.is_sleeping else "running")
    return {
        "status": status,
        "subscribers": brain.current_subscribers,
        "posts_today": brain.posts_today,
        "replies_today": brain.replies_today,
        "max_posts": brain.max_posts_today,
        "max_replies": brain.max_replies_today,
        "errors_today": brain.errors_today,
        "current_time_pkt": brain._get_pkt_now().strftime("%I:%M %p, %b %d"),
        "sleep_window": f"{brain.SCHEDULE['sleep_start']}:00 - {brain.SCHEDULE['sleep_end']}:00 PKT",
    }


@app.get("/api/health")
async def health(request: Request):
    """
    Full system health in one call: what is running, what is connected,
    and whether the bot is currently awake. Use this to answer
    'is it working right now?' without digging through logs.
    """
    st = request.app.state
    brain = getattr(st, 'brain', None)
    sm = getattr(st, 'stealth_marketer', None)
    sc = getattr(st, 'signal_copier', None)
    bc = getattr(st, 'telegram_broadcaster', None)

    def connected(mod):
        try:
            return bool(mod and mod.client and mod.client.is_connected())
        except Exception:
            return False

    awake = bool(brain) and not brain.is_sleeping and not brain.master_kill and not brain.is_paused

    return {
        "ok": True,
        "awake": awake,
        "current_time_pkt": brain._get_pkt_now().strftime("%I:%M %p, %b %d") if brain else None,
        "sleeping": brain.is_sleeping if brain else None,
        "sleep_window": (f"{brain.SCHEDULE['sleep_start']}:00 - {brain.SCHEDULE['sleep_end']}:00 PKT"
                         if brain else None),
        "always_on": (brain.SCHEDULE['sleep_start'] == brain.SCHEDULE['sleep_end']) if brain else None,
        "master_kill": brain.master_kill if brain else None,
        "modules": {
            "news_agent": {
                "active": brain.news_module_active if brain else False,
                "telegram_connected": connected(bc),
                "posts_today": brain.posts_today if brain else 0,
                "max_posts": brain.max_posts_today if brain else 0,
            },
            "signal_copier": sc.status if sc else {"active": False, "wired": False},
            "stealth_marketer": sm.status if sm else {"active": False, "wired": False},
            "pin_agent": (pa.status | {"active": bool(brain and brain.pin_module_active)}
                          if (pa := getattr(st, 'pin_agent', None))
                          else {"active": False, "wired": False}),
            "website": {
                "active": brain.website_module_active if brain else False,
                "articles_written": getattr(
                    getattr(getattr(st, 'content_engine', None), 'article_agent', None),
                    'articles_written', 0),
                "site_url": getattr(getattr(st, 'config', None), 'site_url', ''),
            },
        },
        "buffer": bb.status if (bb := getattr(st, 'buffer_broadcaster', None)) else {"configured": False},
        "social": sy.status if (sy := getattr(st, 'syndicator', None)) else {"ready": False},
        "database_connected": bool(brain and brain.db and getattr(brain.db, "_initialized", False)),
        "email": nm.health if (nm := getattr(st, 'notification_manager', None)) else {"configured": False},
        "growth": ge.summary() if (ge := getattr(st, 'growth_engine', None)) else None,
    }

@app.post("/api/test_email")
async def test_email(request: Request):
    """Fires a test email using NotificationManager."""
    if not hasattr(request.app.state, 'notification_manager'):
        raise HTTPException(status_code=500, detail="Notification Manager not wired.")
        
    nm = request.app.state.notification_manager
    success = await nm.send_notification("Test Email", "This is a test email triggered from the React Dashboard.", is_critical=False)
    if success:
        return {"success": True, "message": "Email sent successfully."}
    raise HTTPException(status_code=500, detail="Failed to send email. Check credentials.")

@app.post("/api/modify_limits")
async def modify_limits(req: LimitUpdateRequest, request: Request):
    """
    Modifies any runtime limit and sends a notification email.

    Supported limit_type values:
      post            — max news posts per day
      stealth         — max stealth replies per day
      stealth_invite  — max people invited per day (hard-capped for safety)
      signal          — max signals copied per day (0 = unlimited)
    """
    brain = getattr(request.app.state, 'brain', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    nm = getattr(request.app.state, 'notification_manager', None)
    sm = getattr(request.app.state, 'stealth_marketer', None)
    sc = getattr(request.app.state, 'signal_copier', None)

    limit_type = (req.limit_type or "").strip().lower()
    new_value = int(req.new_value)
    if new_value < 0:
        raise HTTPException(status_code=400, detail="Limit cannot be negative.")

    detail = {}

    if limit_type == "stealth":
        old = brain.max_replies_today
        brain.max_replies_today = new_value
        msg = f"Daily stealth reply limit updated: {old} → {new_value}."

    elif limit_type == "post":
        old = brain.max_posts_today
        brain.max_posts_today = new_value
        msg = f"Daily news post limit updated: {old} → {new_value}."

    elif limit_type in ("stealth_invite", "invite"):
        if not sm:
            raise HTTPException(status_code=500, detail="Stealth Marketer not wired.")
        detail = sm.set_daily_invite_limit(new_value)
        msg = f"Daily invite limit updated: {detail['old']} → {detail['applied']}."
        if detail["capped"]:
            msg += (f" Requested {detail['requested']} but capped at "
                    f"{detail['hard_cap']} to protect the account from a Telegram ban.")

    elif limit_type == "signal":
        if not sc:
            raise HTTPException(status_code=500, detail="Signal Copier not wired.")
        old = sc.max_signals_per_day
        sc.set_daily_limit(new_value)
        msg = (f"Daily signal limit updated: {old or 'unlimited'} → "
               f"{sc.max_signals_per_day or 'unlimited'}.")

    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid limit type. Use: post, stealth, stealth_invite, or signal."
        )

    if nm:
        await nm.notify_strategy_change(
            change_type=f"{limit_type} limit changed",
            old_value=str(detail.get("old", "")) or "previous",
            new_value=str(detail.get("applied", new_value)),
            reason="Manually changed from the NOVI dashboard."
        )

    if brain.db:
        await brain.db.log_metric(f"limit_{limit_type}", new_value,
                                  {"action": "manual_update", "detail": detail})
    await brain.save_state()

    return {"success": True, "message": msg, "detail": detail}


class SleepWindowRequest(BaseModel):
    start_hour: int
    end_hour: int


@app.get("/api/schedule")
async def get_schedule(request: Request):
    """Returns the current sleep window and posting schedule (PKT)."""
    brain = getattr(request.app.state, 'brain', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    now = brain._get_pkt_now()
    return {
        "current_time_pkt": now.strftime("%I:%M %p, %b %d"),
        "sleep_start_hour": brain.SCHEDULE["sleep_start"],
        "sleep_end_hour": brain.SCHEDULE["sleep_end"],
        "currently_sleeping": brain.is_sleeping,
        "always_on": brain.SCHEDULE["sleep_start"] == brain.SCHEDULE["sleep_end"],
        "post_slots": brain.SCHEDULE["post_slots"],
        "morning_brief": brain.SCHEDULE["morning_brief"],
    }


@app.post("/api/schedule/sleep_window")
async def set_sleep_window(req: SleepWindowRequest, request: Request):
    """
    Changes when the bot sleeps. Set start_hour == end_hour for 24/7 operation.
    This answers 'I don't know when it is sleeping and working'.
    """
    brain = getattr(request.app.state, 'brain', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    nm = getattr(request.app.state, 'notification_manager', None)
    old = (brain.SCHEDULE["sleep_start"], brain.SCHEDULE["sleep_end"])
    brain.set_sleep_window(req.start_hour, req.end_hour)
    new = (brain.SCHEDULE["sleep_start"], brain.SCHEDULE["sleep_end"])

    always_on = new[0] == new[1]
    msg = ("Bot is now ALWAYS ON (24/7, never sleeps)." if always_on
           else f"Sleep window updated: {new[0]}:00 → {new[1]}:00 PKT.")

    if nm:
        await nm.notify_strategy_change(
            change_type="Sleep Window Changed",
            old_value=f"{old[0]}:00 → {old[1]}:00 PKT",
            new_value="24/7 — never sleeps" if always_on else f"{new[0]}:00 → {new[1]}:00 PKT",
            reason="Changed from the NOVI dashboard."
        )

    await brain.save_state()
    return {"success": True, "message": msg, "currently_sleeping": brain.is_sleeping,
            "sleep_start_hour": new[0], "sleep_end_hour": new[1], "always_on": always_on}

@app.get("/api/logs")
async def get_logs():
    """Fetches the last 200 lines of the bot log to debug remote issues."""
    try:
        # Assuming omni_bot.log is in the working directory
        log_path = os.path.join(os.path.dirname(__file__), "..", "omni_bot.log")
        if not os.path.exists(log_path):
            log_path = "omni_bot.log" # fallback to current dir
            
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return {"logs": lines[-200:]}
    except Exception as e:
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════
#  STEALTH MARKETER KILL SWITCH API
# ═══════════════════════════════════════════════════════════

@app.get("/api/stealth/status")
async def stealth_status(request: Request):
    """Returns the current status of the Stealth Marketer."""
    if not hasattr(request.app.state, 'stealth_marketer'):
        return {"active": False, "message": "Stealth Marketer not wired."}
    
    sm = request.app.state.stealth_marketer
    return sm.status

@app.post("/api/stealth/toggle")
async def stealth_toggle(request: Request):
    """Toggles the Stealth Marketer ON or OFF."""
    if not hasattr(request.app.state, 'stealth_marketer'):
        raise HTTPException(status_code=500, detail="Stealth Marketer not wired.")
    
    sm = request.app.state.stealth_marketer
    
    if sm.is_active:
        await sm.deactivate(reason="Toggled OFF from dashboard")
        return {"success": True, "active": False, "message": "Stealth Marketer has been DEACTIVATED."}
    else:
        success = await sm.activate()
        if success:
            return {"success": True, "active": True, "message": "Stealth Marketer has been ACTIVATED."}
        else:
            return {"success": False, "active": False, "message": "Cannot activate: emergency stop is engaged. Reset required."}

@app.post("/api/stealth/emergency_reset")
async def stealth_emergency_reset(request: Request):
    """Resets the emergency stop flag after a cooldown period."""
    if not hasattr(request.app.state, 'stealth_marketer'):
        raise HTTPException(status_code=500, detail="Stealth Marketer not wired.")
    
    sm = request.app.state.stealth_marketer
    sm.reset_emergency()
    return {"success": True, "message": "Emergency stop flag has been reset. You can now reactivate the Stealth Marketer."}


# ═══════════════════════════════════════════════════════════
#  MODULE TOGGLE ENDPOINTS (Dashboard Control Panel)
# ═══════════════════════════════════════════════════════════

@app.get("/api/modules/status")
async def modules_status(request: Request):
    """Returns the ON/OFF status of all modules for the dashboard."""
    brain = getattr(request.app.state, 'brain', None)
    sm = getattr(request.app.state, 'stealth_marketer', None)
    sc = getattr(request.app.state, 'signal_copier', None)
    
    return {
        "news_agent": brain.news_module_active if brain else False,
        "website_module": brain.website_module_active if brain else False,
        "signal_copier": sc.is_active if sc else False,
        "stealth_reply_mode": sm._reply_active if sm else False,
        "stealth_invite_mode": sm._scraping_active if sm else False,
        "master_kill": brain.master_kill if brain else False,
        "stealth_status": sm.status if sm else {},
        "signal_copier_status": sc.status if sc else {},
    }

async def _desired_state(request: Request, current: bool) -> bool:
    """
    What the caller wants a module set to.

    A body of {"active": true} sets it ON, {"active": false} sets it OFF, and
    an absent/blank body falls back to inverting the current value.

    This exists because a blind invert is genuinely unsafe here: NOVI decides
    which action to run from natural language, and it once read "give me the
    report of the news agent" as a toggle and switched the News Agent off. A
    misread intent should at worst do nothing, never the opposite of what was
    asked — so "turn it on" can no longer turn anything off.
    """
    try:
        body = await request.json()
    except Exception:
        return not current
    if isinstance(body, dict) and body.get("active") is not None:
        value = body["active"]
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "active")
        return bool(value)
    return not current


@app.post("/api/news/toggle")
async def toggle_news(request: Request):
    """Sets the News Agent ON or OFF (see `_desired_state`)."""
    brain = getattr(request.app.state, 'brain', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    brain.news_module_active = await _desired_state(request, brain.news_module_active)
    status = "ACTIVE" if brain.news_module_active else "DEACTIVATED"
    
    if nm:
        await nm.notify_module_status("News Agent (@Novi_Network)", status,
            f"News posting has been turned {'ON' if brain.news_module_active else 'OFF'} from the dashboard.")
    
    if brain.db:
        await brain.db.log_metric("news_module_status", 1 if brain.news_module_active else 0,
                                   {"action": "toggled", "new_status": status})
    
    await brain.save_state()
    return {"success": True, "active": brain.news_module_active, "message": f"News Agent is now {status}."}

@app.post("/api/website/toggle")
async def toggle_website(request: Request):
    """
    Toggles the Website / auto-blogging module ON or OFF.

    Default is OFF. While off, the Article Agent does not run at all — no
    article is generated and nothing is published to the site, so its
    (separate) API key is never charged.
    """
    brain = getattr(request.app.state, 'brain', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    brain.website_module_active = await _desired_state(request, brain.website_module_active)
    status = "ACTIVE" if brain.website_module_active else "DEACTIVATED"

    if nm:
        await nm.notify_module_status(
            "Website / Auto-Blogging", status,
            f"Article generation has been turned "
            f"{'ON' if brain.website_module_active else 'OFF'} from the dashboard."
        )

    if brain.db:
        await brain.db.log_metric("website_module_status",
                                  1 if brain.website_module_active else 0,
                                  {"action": "toggled", "new_status": status})

    await brain.save_state()
    return {"success": True, "active": brain.website_module_active,
            "message": f"Website / Auto-Blogging is now {status}."}


@app.post("/api/social/toggle")
async def toggle_social(request: Request):
    """
    Turns Facebook AND X on or off together — the master switch.

    Separate from the website switch on purpose. The article still publishes
    while this is off; it just is not announced. A page under review or an
    account being rebuilt should never be a reason to stop writing.
    """
    brain = getattr(request.app.state, 'brain', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    brain.social_module_active = await _desired_state(request, brain.social_module_active)
    status = "ACTIVE" if brain.social_module_active else "DEACTIVATED"

    if nm:
        await nm.notify_module_status(
            "Facebook + X", status,
            f"Social announcing has been turned "
            f"{'ON' if brain.social_module_active else 'OFF'} from the dashboard.")

    if brain.db:
        await brain.db.log_metric("social_module_status",
                                  1 if brain.social_module_active else 0,
                                  {"action": "toggled", "new_status": status})

    await brain.save_state()
    return {"success": True, "active": brain.social_module_active,
            "message": f"Facebook + X is now {status}."}


@app.post("/api/social/{platform}/toggle")
async def toggle_social_platform(platform: str, request: Request):
    """
    Turns ONE platform on or off: /api/social/facebook/toggle or
    /api/social/twitter/toggle.

    Two switches rather than one because they fail independently — an X
    account gets restricted, a Facebook page is mid-rebuild — and losing one
    is no reason to lose the other.
    """
    brain = getattr(request.app.state, 'brain', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    field = {"facebook": "facebook_active",
             "twitter": "twitter_active",
             "x": "twitter_active",
             "threads": "threads_active",
             "bluesky": "bluesky_active"}.get(platform.strip().lower())
    if not field:
        raise HTTPException(
            status_code=400,
            detail="Platform must be 'facebook', 'twitter', 'threads' "
                   "or 'bluesky'.")

    setattr(brain, field, await _desired_state(request, getattr(brain, field)))
    on = getattr(brain, field)
    label = {"facebook_active": "Facebook",
             "twitter_active": "X / Twitter",
             "threads_active": "Threads",
             "bluesky_active": "Bluesky"}[field]
    status = "ACTIVE" if on else "DEACTIVATED"

    if brain.db:
        await brain.db.log_metric(f"{field}_status", 1 if on else 0,
                                  {"action": "toggled", "new_status": status})

    await brain.save_state()
    note = ("" if brain.social_module_active else
            " (the social master switch is still OFF, so nothing posts yet)")
    return {"success": True, "active": on,
            "message": f"{label} is now {status}.{note}"}


@app.post("/api/pins/toggle")
async def toggle_pins(request: Request):
    """Turns the AliExpress to Pinterest agent ON or OFF."""
    brain = getattr(request.app.state, 'brain', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")

    brain.pin_module_active = await _desired_state(request, brain.pin_module_active)
    status = "ACTIVE" if brain.pin_module_active else "DEACTIVATED"

    if nm:
        await nm.notify_module_status(
            "Pinterest Agent", status,
            f"AliExpress product pinning has been turned "
            f"{'ON' if brain.pin_module_active else 'OFF'} from the dashboard.")

    if brain.db:
        await brain.db.log_metric("pin_module_status",
                                  1 if brain.pin_module_active else 0,
                                  {"action": "toggled", "new_status": status})

    await brain.save_state()
    return {"success": True, "active": brain.pin_module_active,
            "message": f"Pinterest Agent is now {status}."}


@app.get("/api/pins/status")
async def pins_status(request: Request):
    """Read-only view of the pin agent, including anything awaiting review."""
    brain = getattr(request.app.state, 'brain', None)
    agent = getattr(request.app.state, 'pin_agent', None)
    if not agent:
        return {"wired": False, "active": False,
                "message": "Pinterest agent is not configured on this deploy."}

    return {
        "wired": True,
        "active": bool(brain and brain.pin_module_active),
        **agent.status,
        "pending": [
            {"product_id": p["product_id"], "title": p["title"],
             "image_url": p.get("image_url", ""), "link": p["link"],
             "description": p["description"]}
            for p in agent.pending_review[:10]
        ],
    }


@app.post("/api/pins/review")
async def pins_review(request: Request):
    """
    Approves or rejects a pin awaiting review.

    Body: {"product_id": "...", "decision": "approve" | "reject"}
    A pin only reaches Pinterest once a person has said yes, while review is
    on — this recommends purchases rather than opinions, so an invented
    feature is worth catching before it publishes.
    """
    agent = getattr(request.app.state, 'pin_agent', None)
    if not agent:
        raise HTTPException(status_code=500, detail="Pinterest agent not wired.")

    try:
        body = await request.json()
    except Exception:
        body = {}
    product_id = str(body.get("product_id") or "")
    decision = str(body.get("decision") or "").lower()

    if decision == "approve":
        pin = await agent.approve_pending(product_id)
        if pin:
            return {"success": True, "message": f"Pin published: {pin['title'][:60]}"}
        raise HTTPException(status_code=400,
                            detail="Pin could not be published. Check the logs.")

    if decision == "reject":
        if agent.reject_pending(product_id):
            return {"success": True, "message": "Pin rejected and product suppressed."}
        raise HTTPException(status_code=404, detail="No such pin awaiting review.")

    raise HTTPException(status_code=400, detail="decision must be approve or reject.")


@app.post("/api/pins/run_now")
async def pins_run_now(request: Request):
    """Builds one pin immediately instead of waiting for the schedule."""
    agent = getattr(request.app.state, 'pin_agent', None)
    brain = getattr(request.app.state, 'brain', None)
    if not agent:
        raise HTTPException(status_code=500, detail="Pinterest agent not wired.")
    if brain and not brain.pin_module_active:
        raise HTTPException(status_code=400,
                            detail="Pinterest agent is switched off.")

    pin = await agent.run_once()
    if not pin:
        raise HTTPException(status_code=400,
                            detail=agent.last_error or "No pin could be produced.")
    return {"success": True, "status": pin.get("status"),
            "title": pin["title"], "image_url": pin.get("image_url", ""),
            "message": ("Pin is awaiting your review."
                        if pin.get("status") == "awaiting_review"
                        else "Pin published.")}


@app.post("/api/signal_copier/toggle")
async def toggle_signal_copier(request: Request):
    """Toggles the Signal Copier (Whale Tracker VIP) ON or OFF."""
    sc = getattr(request.app.state, 'signal_copier', None)
    if not sc:
        raise HTTPException(status_code=500, detail="Signal Copier not wired.")

    if await _desired_state(request, sc.is_active):
        await sc.activate()
        return {"success": True, "active": True, "message": "Signal Copier (Whale Tracker VIP) has been ACTIVATED."}
    await sc.deactivate()
    return {"success": True, "active": False, "message": "Signal Copier (Whale Tracker VIP) has been DEACTIVATED."}

@app.post("/api/stealth/toggle_reply")
async def toggle_stealth_reply(request: Request):
    """Toggles the Stealth Marketer Reply Mode ON or OFF."""
    sm = getattr(request.app.state, 'stealth_marketer', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    if not sm:
        raise HTTPException(status_code=500, detail="Stealth Marketer not wired.")
    
    sm._reply_active = await _desired_state(request, sm._reply_active)
    status = "ACTIVE" if sm._reply_active else "DEACTIVATED"
    
    if nm:
        await nm.notify_module_status("Stealth Reply Mode", status,
            f"Reply mode has been turned {'ON' if sm._reply_active else 'OFF'} from the dashboard.")
    
    if sm.db:
        await sm.db.log_metric("stealth_reply_status", 1 if sm._reply_active else 0,
                                {"action": "toggled", "new_status": status})
    
    return {"success": True, "active": sm._reply_active, "message": f"Stealth Reply Mode is now {status}."}

@app.post("/api/stealth/toggle_invite")
async def toggle_stealth_invite(request: Request):
    """Toggles the Stealth Marketer Member Adding ON or OFF."""
    sm = getattr(request.app.state, 'stealth_marketer', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    if not sm:
        raise HTTPException(status_code=500, detail="Stealth Marketer not wired.")
    
    sm._scraping_active = await _desired_state(request, sm._scraping_active)
    status = "ACTIVE" if sm._scraping_active else "DEACTIVATED"
    
    if nm:
        await nm.notify_module_status("Stealth Member Adding", status,
            f"Member adding has been turned {'ON' if sm._scraping_active else 'OFF'} from the dashboard.")
    
    if sm.db:
        await sm.db.log_metric("stealth_invite_status", 1 if sm._scraping_active else 0,
                                {"action": "toggled", "new_status": status})
    
    return {"success": True, "active": sm._scraping_active, "message": f"Stealth Member Adding is now {status}."}

@app.post("/api/master_kill")
async def master_kill(request: Request):
    """Master kill switch — stops ALL modules."""
    brain = getattr(request.app.state, 'brain', None)
    sm = getattr(request.app.state, 'stealth_marketer', None)
    sc = getattr(request.app.state, 'signal_copier', None)
    nm = getattr(request.app.state, 'notification_manager', None)
    
    if not brain:
        raise HTTPException(status_code=500, detail="Brain not wired.")
    
    brain.master_kill = not brain.master_kill
    
    if brain.master_kill:
        # Kill everything
        brain.news_module_active = False
        if sm:
            await sm.deactivate(reason="Master Kill Switch engaged from dashboard")
        if sc:
            await sc.deactivate()
        
        if nm:
            await nm.send_notification(
                subject="MASTER KILL SWITCH — ALL MODULES STOPPED",
                message="The master kill switch has been engaged from the dashboard. ALL modules are now OFF.",
                is_critical=True
            )
        return {"success": True, "master_kill": True, "message": "MASTER KILL ENGAGED. All modules stopped."}
    else:
        if nm:
            await nm.send_notification(
                subject="Master Kill Switch Released",
                message="The master kill switch has been released. Modules can now be individually activated.",
                is_critical=False
            )
        return {"success": True, "master_kill": False, "message": "Master kill released. You can now activate individual modules."}

# ═══════════════════════════════════════════════════════════
#  ON-DEMAND ACTIONS (NOVI voice / dashboard commands)
# ═══════════════════════════════════════════════════════════

class GrowNowRequest(BaseModel):
    count: int = 1


@app.post("/api/growth/invite_now")
async def invite_now(req: GrowNowRequest, request: Request):
    """
    Runs an invite cycle IMMEDIATELY on command, instead of waiting for the
    hourly loop. This is what makes "Novi, add some subscribers" actually do
    something right now.

    All normal safety rules still apply — daily cap, spacing, sleep hours and
    the emergency stop are enforced inside the Stealth Marketer.
    """
    sm = getattr(request.app.state, 'stealth_marketer', None)
    brain = getattr(request.app.state, 'brain', None)
    if not sm:
        raise HTTPException(status_code=500, detail="Stealth Marketer not wired.")

    if brain and brain.master_kill:
        raise HTTPException(status_code=409, detail="Master kill switch is engaged. Release it first.")
    if sm._emergency_stop:
        raise HTTPException(status_code=409,
                            detail="Emergency stop is engaged. Reset it before inviting.")
    if not sm.client:
        raise HTTPException(status_code=409, detail="Stealth account is not connected.")

    if not sm.is_active:
        await sm.activate()

    remaining = sm._invites_max_per_day - sm._invites_today
    if remaining <= 0:
        return {"success": False,
                "message": f"Daily invite limit already reached "
                           f"({sm._invites_today}/{sm._invites_max_per_day}). "
                           f"Raise the limit or wait until tomorrow."}

    # Run in the background — a full cycle includes long human-like delays
    # and must never block the HTTP response.
    asyncio.create_task(sm.scrape_and_invite_cycle())

    return {
        "success": True,
        "message": f"Invite cycle started. Up to {min(req.count, remaining)} "
                   f"invitation(s) will go out with human-like delays. "
                   f"You will get an email for each one.",
        "invites_today": sm._invites_today,
        "daily_limit": sm._invites_max_per_day,
    }


@app.get("/api/growth/status")
async def growth_status(request: Request):
    """Subscriber growth, measured — plus a recommendation on what to change."""
    ge = getattr(request.app.state, 'growth_engine', None)
    if not ge:
        raise HTTPException(status_code=500, detail="Growth Engine not wired.")
    return ge.summary()


class PublishRequest(BaseModel):
    category: str = ""
    publish: bool = True


@app.post("/api/post_now")
async def post_now(req: PublishRequest, request: Request):
    """
    Full one-shot pipeline on command: scrape -> synthesize -> image -> publish.

    This is the "Novi, post something right now" path. /api/create_post only
    drafts; this drafts AND publishes in a single call.
    """
    st = request.app.state
    ce = getattr(st, 'content_engine', None)
    bc = getattr(st, 'telegram_broadcaster', None)
    brain = getattr(st, 'brain', None)

    if not ce:
        raise HTTPException(status_code=500, detail="Content Engine not wired.")
    if brain and brain.master_kill:
        raise HTTPException(status_code=409, detail="Master kill switch is engaged.")

    category = (req.category or "").strip() or "all"
    if category.lower() == "trending":
        category = "all"

    try:
        package = await ce.produce_content_package(category=category, force=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Content generation failed: {e}")

    if not package:
        raise HTTPException(status_code=500,
                            detail="Could not produce a post. News sources or the AI may be unreachable.")

    if package.get("image_path"):
        package["image_url"] = f"/generated_images/{os.path.basename(package['image_path'])}"
    st.last_generated_package = package

    if not req.publish:
        return {"success": True, "published": False,
                "message": "Draft ready for review.", "package": package}

    if not bc or not bc.is_ready:
        raise HTTPException(status_code=409,
                            detail="Telegram Broadcaster is not connected, so the post could not be published.")

    ok = await bc.post(package)
    if not ok:
        raise HTTPException(status_code=500, detail="Telegram rejected the post. Check the logs.")

    if brain:
        brain.record_post(category=package.get("category", "general"),
                          topic=package.get("original_title", ""))
    ge = getattr(st, 'growth_engine', None)
    if ge:
        ge.record_action(ge.ACTION_POST, {"category": package.get("category")})

    # Same fan-out the scheduler uses: website article, Reddit, X, Facebook
    fanout_results = {}
    fo = getattr(st, 'fanout', None)
    if fo:
        fanout_results = await fo.distribute(package, story=package.get("story"))

    delivered = [k for k, v in fanout_results.items() if v]
    return {"success": True, "published": True,
            "message": f"Published to {bc.channel_username}"
                       + (f" and {', '.join(delivered)}." if delivered else "."),
            "fanout": fanout_results,
            "package": {k: v for k, v in package.items() if k != "story"}}


@app.post("/api/generate_image")
async def generate_image_endpoint(req: ImageGenRequest):
    """
    Generates a high-quality AI background with text overlay and returns the path.
    The React UI can call this when the user clicks 'Generate Image'.
    """
    output_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "generated_images")
    os.makedirs(output_dir, exist_ok=True)
    
    bing_cookie = os.environ.get("BING_COOKIE", "")
    image_gen = ImageGenerator(output_dir=output_dir, channel_name="NoviNews", bing_cookie=bing_cookie)
    
    try:
        path = await image_gen.generate(
            headline=req.headline,
            category=req.category,
            source_credit=req.source_credit,
        )
        if path:
            # Return relative path for web serving
            filename = os.path.basename(path)
            return {"success": True, "image_url": f"/generated_images/{filename}"}
        
        raise HTTPException(status_code=500, detail="Image generation failed internally.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CreatePostRequest(BaseModel):
    category: str = ""

@app.post("/api/create_post")
async def create_post_endpoint(req: CreatePostRequest, request: Request):
    """
    Triggers the Content Engine to scrape news, synthesize it, and generate an image.
    Returns the full content package ready for review on the dashboard.
    """
    if not hasattr(request.app.state, 'content_engine'):
        raise HTTPException(status_code=500, detail="Content Engine not wired.")
        
    ce = request.app.state.content_engine
    category = req.category if req.category else "all"
    if category.lower() == "trending" or category.strip() == "":
        category = "all"
    
    try:
        package = await ce.produce_content_package(category=category)
        if not package:
            raise HTTPException(status_code=500, detail="Failed to produce content package. Check logs for scraping or AI errors.")
            
        # Convert absolute path to relative URL for web
        if package.get("image_path"):
            filename = os.path.basename(package["image_path"])
            package["image_url"] = f"/generated_images/{filename}"
        
        return {"success": True, "package": package}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from fastapi.responses import StreamingResponse
import json

@app.get("/api/create_post_stream")
async def create_post_stream(request: Request, category: str = "all"):
    """
    Streaming version of create_post using Server-Sent Events (SSE).
    The sub-agent reports progress in real-time.
    """
    if not hasattr(request.app.state, 'content_engine'):
        raise HTTPException(status_code=500, detail="Content Engine not wired.")
        
    ce = request.app.state.content_engine
    if category.lower() == "trending" or category.strip() == "":
        category = "all"
        
    queue = asyncio.Queue()
    
    async def progress_callback(event: dict):
        await queue.put(event)
    
    async def run_pipeline():
        """Runs the content engine and pushes errors to the queue if it crashes."""
        try:
            package = await ce.produce_content_package(category=category, progress_callback=progress_callback, force=True)
            if package:
                if package.get("image_path"):
                    filename = os.path.basename(package["image_path"])
                    package["image_url"] = f"/generated_images/{filename}"
                request.app.state.last_generated_package = package
                await queue.put({"step": "result", "package": package})
            else:
                await queue.put({"step": "error", "message": "Sub-agent failed to produce a content package. Check if news sources are reachable."})
        except Exception as e:
            await queue.put({"step": "error", "message": f"Sub-agent crashed: {str(e)}"})
        finally:
            # Always send a sentinel so the generator loop exits
            await queue.put({"step": "_done"})
        
    async def event_generator():
        task = asyncio.create_task(run_pipeline())
        
        while True:
            event = await queue.get()
            
            if event.get("step") == "_done":
                break
            
            yield f"data: {json.dumps(event)}\n\n"
        
        # Make sure task is finished
        await task
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")


class ChatProxyRequest(BaseModel):
    systemPrompt: str
    input: str
    api_key: str = ""   # optional legacy override, for local development only

@app.post("/api/chat")
async def chat_proxy(req: ChatProxyRequest):
    """
    Proxies NOVI's voice brain to Groq.

    The key comes from the SERVER environment (GROQ_API_KEY). It used to be
    sent by the browser via VITE_GROQ_API_KEY, which baked the key into the
    public JS bundle where anyone could read it.
    """
    import aiohttp

    api_key = os.environ.get("GROQ_API_KEY", "").strip() or req.api_key
    if not api_key:
        return {"error": {"message": "No Groq API key configured on the backend. "
                                     "Set GROQ_API_KEY in the Render environment."}}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        # Groq retired every Llama model on this account; llama-3.1-8b-instant
        # now 404s, which is what stopped NOVI from answering at all.
        "model": os.environ.get("NOVI_CHAT_MODEL", "openai/gpt-oss-20b").strip(),
        "messages": [
            {"role": "system", "content": req.systemPrompt},
            {"role": "user", "content": req.input}
        ],
        "temperature": 0.7
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=15.0) as resp:
                data = await resp.json()
                if resp.status != 200:
                    return {"error": {"message": data.get("error", {}).get("message", f"HTTP {resp.status}")}}
                return data
    except Exception as e:
        return {"error": {"message": f"Backend Proxy Error: {str(e)}"}}


# ═══════════════════════════════════════════════════════════
#  KEEP-ALIVE PING (Render Free Tier)
# ═══════════════════════════════════════════════════════════

@app.get("/ping")
async def ping():
    """Keep-alive endpoint for Render free tier. Pinged every 10 minutes."""
    return {"status": "alive", "message": "NOVI is running."}


# ═══════════════════════════════════════════════════════════
#  TELEGRAM OTP AUTH (Main Channel)
# ═══════════════════════════════════════════════════════════

class TelegramAuthRequest(BaseModel):
    phone: str

class TelegramCodeSubmit(BaseModel):
    code: str
    password: str = ""  # 2FA password if required

# Store pending auth state
_telegram_auth_state = {"client": None, "phone_hash": None, "phone": None}
_stealth_auth_state = {"client": None, "phone_hash": None, "phone": None}

@app.post("/api/telegram/request_code")
async def telegram_request_code(req: TelegramAuthRequest, request: Request):
    """Step 1: Sends OTP code to the phone number for Telegram main account login."""
    try:
        from telethon import TelegramClient
        
        config = getattr(request.app.state, 'config', None)
        if not config:
            raise HTTPException(status_code=500, detail="Config not loaded.")
        
        api_id = config.telegram_api_id
        api_hash = config.telegram_api_hash
        
        session_path = os.path.join(os.path.dirname(__file__), "..", "sessions", "main_channel")
        os.makedirs(os.path.dirname(session_path), exist_ok=True)
        
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        
        result = await client.send_code_request(req.phone)
        _telegram_auth_state["client"] = client
        _telegram_auth_state["phone_hash"] = result.phone_code_hash
        _telegram_auth_state["phone"] = req.phone
        
        nm = getattr(request.app.state, 'notification_manager', None)
        if nm:
            await nm.notify_module_status("Telegram Auth", "OTP Sent", f"OTP code sent to {req.phone}. Waiting for code input on dashboard.")
        
        return {"success": True, "message": f"OTP code sent to {req.phone}. Enter it below."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send code: {str(e)}")

@app.post("/api/telegram/submit_code")
async def telegram_submit_code(req: TelegramCodeSubmit, request: Request):
    """Step 2: Submits the OTP code to complete Telegram login."""
    client = _telegram_auth_state.get("client")
    if not client:
        raise HTTPException(status_code=400, detail="No pending auth. Request a code first.")
    
    try:
        from telethon.errors import SessionPasswordNeededError
        
        try:
            await client.sign_in(
                phone=_telegram_auth_state["phone"],
                code=req.code,
                phone_code_hash=_telegram_auth_state["phone_hash"]
            )
        except SessionPasswordNeededError:
            if not req.password:
                return {"success": False, "needs_2fa": True, "message": "2FA password required. Please enter your Telegram password."}
            await client.sign_in(password=req.password)
        
        me = await client.get_me()
        
        # Store the connected client on app state
        request.app.state.telegram_user_client = client
        
        nm = getattr(request.app.state, 'notification_manager', None)
        if nm:
            await nm.notify_connection_status("Telegram Main Account", True, f"Logged in as {me.first_name} ({_telegram_auth_state['phone']})")
        
        _telegram_auth_state["client"] = None
        _telegram_auth_state["phone_hash"] = None
        
        return {"success": True, "message": f"Telegram authorized as {me.first_name}!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth failed: {str(e)}")


# ═══════════════════════════════════════════════════════════
#  STEALTH MARKETER OTP AUTH (Separate Burner Number)
# ═══════════════════════════════════════════════════════════

@app.post("/api/stealth/request_code")
async def stealth_request_code(req: TelegramAuthRequest, request: Request):
    """Step 1: Sends OTP code to the burner phone for StealthMarketer login."""
    try:
        from telethon import TelegramClient
        import random
        
        config = getattr(request.app.state, 'config', None)
        if not config:
            raise HTTPException(status_code=500, detail="Config not loaded.")
        
        api_id = config.telegram_api_id
        api_hash = config.telegram_api_hash
        
        # Randomize device fingerprint for anti-detection
        devices = [
            {"model": "Samsung SM-S928B", "system": "Android 14", "app": "10.14.5"},
            {"model": "Google Pixel 8 Pro", "system": "Android 14", "app": "10.14.5"},
            {"model": "OnePlus 12", "system": "Android 14", "app": "10.14.5"},
        ]
        device = random.choice(devices)
        
        session_path = os.path.join(os.path.dirname(__file__), "..", "sessions", "stealth")
        os.makedirs(os.path.dirname(session_path), exist_ok=True)
        
        client = TelegramClient(
            session_path, api_id, api_hash,
            device_model=device["model"],
            system_version=device["system"],
            app_version=device["app"],
        )
        await client.connect()
        
        result = await client.send_code_request(req.phone)
        _stealth_auth_state["client"] = client
        _stealth_auth_state["phone_hash"] = result.phone_code_hash
        _stealth_auth_state["phone"] = req.phone
        
        nm = getattr(request.app.state, 'notification_manager', None)
        if nm:
            await nm.notify_module_status("Stealth Auth", "OTP Sent", f"OTP sent to burner {req.phone}. Device: {device['model']}")
        
        return {"success": True, "message": f"OTP sent to {req.phone} (Device: {device['model']}). Enter the code below."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send code: {str(e)}")

@app.post("/api/stealth/submit_code")
async def stealth_submit_code(req: TelegramCodeSubmit, request: Request):
    """Step 2: Submits the OTP code to complete StealthMarketer login."""
    client = _stealth_auth_state.get("client")
    if not client:
        raise HTTPException(status_code=400, detail="No pending stealth auth. Request a code first.")
    
    try:
        from telethon.errors import SessionPasswordNeededError
        
        try:
            await client.sign_in(
                phone=_stealth_auth_state["phone"],
                code=req.code,
                phone_code_hash=_stealth_auth_state["phone_hash"]
            )
        except SessionPasswordNeededError:
            if not req.password:
                return {"success": False, "needs_2fa": True, "message": "2FA password required."}
            await client.sign_in(password=req.password)
        
        me = await client.get_me()
        
        # Wire the new client into the stealth marketer
        sm = getattr(request.app.state, 'stealth_marketer', None)
        if sm:
            sm.client = client
            sm._session_start = __import__('time').time()
        
        nm = getattr(request.app.state, 'notification_manager', None)
        if nm:
            await nm.notify_connection_status("Stealth Marketer", True, f"Logged in as {me.first_name} ({_stealth_auth_state['phone']})")
        
        _stealth_auth_state["client"] = None
        _stealth_auth_state["phone_hash"] = None
        
        return {"success": True, "message": f"Stealth Marketer authorized as {me.first_name}!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stealth auth failed: {str(e)}")


# ═══════════════════════════════════════════════════════════
#  CONNECTION STATUS ENDPOINTS
# ═══════════════════════════════════════════════════════════

@app.get("/api/telegram/connection_status")
async def telegram_connection_status(request: Request):
    """Returns whether the main Telegram account is connected."""
    # Check the Broadcaster Telethon client first
    broadcaster = getattr(request.app.state, 'telegram_broadcaster', None)
    if broadcaster and broadcaster.is_ready and broadcaster.client:
        try:
            me = await broadcaster.client.get_me()
            return {"connected": True, "name": me.first_name, "phone": getattr(me, 'phone', 'Personal Account')}
        except Exception:
            pass

    # Fallback to user client if using dashboard login
    client = getattr(request.app.state, 'telegram_user_client', None)
    if client and client.is_connected():
        try:
            me = await client.get_me()
            return {"connected": True, "name": me.first_name, "phone": getattr(me, 'phone', None)}
        except Exception:
            pass
            
    return {"connected": False, "name": None, "phone": None}

@app.get("/api/stealth/connection_status")
async def stealth_connection_status(request: Request):
    """Returns whether the StealthMarketer Telegram account is connected."""
    sm = getattr(request.app.state, 'stealth_marketer', None)
    if sm and sm.client and sm.client.is_connected():
        try:
            me = await sm.client.get_me()
            return {"connected": True, "name": me.first_name, "status": sm.status}
        except Exception:
            return {"connected": False, "name": None, "status": sm.status if sm else {}}
    return {"connected": False, "name": None, "status": sm.status if sm else {}}

@app.post("/api/publish_post")
async def publish_post(request: Request):
    """
    Manually publishes the last generated post from the dashboard.
    """
    package = getattr(request.app.state, 'last_generated_package', None)
    if not package:
        raise HTTPException(status_code=400, detail="No post has been generated yet. Please create a post first.")
        
    broadcaster = getattr(request.app.state, 'telegram_broadcaster', None)
    if not broadcaster:
        raise HTTPException(status_code=500, detail="Telegram Broadcaster is not wired.")
        
    try:
        # Publish to Telegram
        success = await broadcaster.post(package)
        
        # We can also attempt to push to Twitter if available in the app state
        # But this is just a quick push for Telegram
        if hasattr(request.app.state, 'brain') and success:
            request.app.state.brain.record_post()
            
        if success:
            return {"success": True, "message": "Post published to Telegram successfully."}
        else:
            raise HTTPException(status_code=500, detail="Failed to publish. Telegram Broadcaster returned False.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # Run with: uvicorn core.api_server:app --reload
    uvicorn.run(app, host="127.0.0.1", port=8000)

