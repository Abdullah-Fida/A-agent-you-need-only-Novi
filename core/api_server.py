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
    return {
        "status": "paused" if brain.is_paused else ("sleeping" if brain.is_sleeping else "running"),
        "subscribers": brain.current_subscribers,
        "posts_today": brain.posts_today,
        "replies_today": brain.replies_today,
        "max_posts": brain.max_posts_today,
        "max_replies": brain.max_replies_today
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
    """Modifies Brain limits and sends a notification email."""
    if not hasattr(request.app.state, 'brain'):
        raise HTTPException(status_code=500, detail="Brain not wired.")
        
    brain = request.app.state.brain
    nm = request.app.state.notification_manager
    msg = ""
    
    if req.limit_type == "stealth":
        brain.max_replies_today = req.new_value
        msg = f"Dashboard manually updated daily stealth replies to {req.new_value}."
    elif req.limit_type == "post":
        brain.max_posts_today = req.new_value
        msg = f"Dashboard manually updated daily posts to {req.new_value}."
    else:
        raise HTTPException(status_code=400, detail="Invalid limit type.")
        
    # Brain notifies the decision it implemented
    if nm:
        await nm.send_notification("Dashboard Action: Limits Modified", msg)
        
    return {"success": True, "message": msg}

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
    
    # Run synchronously in executor since image_gen.generate is blocking (Pillow + urllib)
    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(None, lambda: image_gen.generate(
            headline=req.headline, 
            category=req.category, 
            source_credit=req.source_credit
        ))
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
    api_key: str
    systemPrompt: str
    input: str

@app.post("/api/chat")
async def chat_proxy(req: ChatProxyRequest):
    import aiohttp
    headers = {
        "Authorization": f"Bearer {req.api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
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

