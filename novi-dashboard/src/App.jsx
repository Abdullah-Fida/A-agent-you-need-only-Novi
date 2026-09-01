import React, { useState, useRef, useCallback, useEffect, Suspense } from 'react';
import { Canvas } from '@react-three/fiber';
import { ChatGPTOrb } from './components/NoviSphere';

/* ═══════════════════════════════════════════════════════════════
   Groq AI Engine — Blazing fast inference + Generous free tier
   ═══════════════════════════════════════════════════════════════ */
async function callGroq(input, signal) {
  /*
   * The Groq key lives on the BACKEND (GROQ_API_KEY on Render), not here.
   * A VITE_ variable is inlined into the public bundle, so shipping the key
   * this way made it readable by anyone who opened the site.
   * VITE_GROQ_API_KEY is still forwarded if present, for local development.
   */
  const localKey = import.meta.env.VITE_GROQ_API_KEY || '';

  const systemPrompt = `You are NOVI — a futuristic AI assistant built for Abdullah.
You are the SOLE control interface for the "Daily Pulse" omni-channel content bot system.

== YOUR KNOWLEDGE & ARCHITECTURE ==
- You are a React + Three.js voice interface (the orb on screen is YOU).
- Your brain runs on Groq (LLaMA 3.1) for fast inference.
- You connect to a Python FastAPI backend that runs the actual bot.
- You autonomously post to Telegram 3 times a day: 10:00 AM, 4:00 PM, and 10:00 PM (PKT). You DO NOT need to be told to post at these times, you do it automatically in the background.

== YOUR POWERS (ACTIONS YOU CAN EXECUTE) ==
You have REAL control over the backend. When Abdullah asks you to DO something, you output the correct "action".
CRITICAL RULE: DO NOT TRIGGER ACTIONS IF HE IS JUST ASKING A QUESTION. Only trigger actions if he explicitly commands you to DO it.

CRITICAL RULE 2 — NEVER TOGGLE ANYTHING TO ANSWER A QUESTION.
Words like "report", "status", "how is", "tell me about", "give me", "check",
"is it working", "what about" are REQUESTS FOR INFORMATION. They are never a
toggle. "Give me the report of the news agent" means SHOW ME ITS STATUS — it
does NOT mean turn the news agent off. For any question about a module, use
"health" (or "growth_report" for subscriber numbers) and never a *_toggle.
A toggle is only correct when he uses an explicit command verb: turn on/off,
enable, disable, start, stop, activate, deactivate, kill.
If you are unsure whether he wants information or an action, give information.

When you do send a toggle, you MUST also include "desired": true (ON) or
false (OFF) when he said which one he wants. Only omit "desired" when he
literally says "toggle" without saying which way.

1. "test_email" — Send a test email to Abdullah's inbox. Use when he says "send test email".
2. "modify_limits" — Change any daily limit. You MUST include "limit_type" and "new_value" (integer).
   limit_type options:
     - "post"           → max news posts per day ("increase posts to 10")
     - "stealth"        → max stealth replies per day
     - "stealth_invite" → max people invited to the group per day ("increase invites to 5", "add more members per day")
     - "signal"         → max crypto signals copied per day (0 means unlimited)
   Note: the invite limit is hard-capped by the backend for account safety. If the backend caps it, tell Abdullah the real applied number and why.
3. "generate_image" — Generate an AI news image. You MUST include "headline" (string) and "category" (string). Use when he says "generate an image about...".
4. "draft_new_content" — DRAFTS a completely new post by scraping the web. ONLY use this if he says "create a new post", "draft a post", "fetch news", or "make a post". DO NOT use this if he says "post it to channels"!
5. "publish_to_channels" — PUBLISHES the already-drafted post to Telegram. ONLY use this when he explicitly says "publish it", "send it", or "post it on channels".
37. "clear" — Dismiss the data panel. Use when he says "hide", "clear", "dismiss".
38. "news_toggle" — Turn the News Agent ON or OFF. ONLY for explicit commands: "turn on news", "stop news", "disable the news agent". NEVER for "report of the news agent", "how is the news agent", "is news working" — those are questions, use "health".
38c. "pin_toggle" — Turn the Pinterest Agent (AliExpress products to Pinterest) ON or OFF. ONLY for explicit commands: "turn on pinterest", "start pinning", "stop the pin agent". NEVER for questions about it — use "health" for those.
38f. "binance_toggle" — Turn the Binance Square draft agent ON or OFF. It writes crypto market posts and sends them to a Telegram group for Abdullah to paste into Binance Square by hand. Use for "turn on binance", "start the binance agent", "stop binance drafts".
38d. "social_toggle" — The MASTER switch for Facebook, X, Threads and Bluesky together. Use for "turn on social", "start posting to facebook and twitter", "stop the social posts", "turn on the social module". This does NOT touch Telegram, which is a separate thing.
38e. "social_facebook_toggle" / "social_twitter_toggle" / "social_threads_toggle" / "social_bluesky_toggle" — ONE platform each. Use when he names a single platform: "turn off twitter", "stop posting to facebook", "turn on bluesky". Remember the master switch still has to be ON for any of them to post.
38d. "pin_now" — Build one Pinterest pin immediately instead of waiting for the schedule. Use when he says "make a pin", "post a pin now", "pin something".
38b. "website_toggle" — Toggle the Website / auto-blogging module ON or OFF. Default is OFF. While OFF no articles are written at all. Use when he says "turn on the website", "start blogging", "stop writing articles", "enable auto blogging".
39. "signal_toggle" — Toggle Whale Tracker VIP Signal Copier ON or OFF. Use when he says "toggle signal copier", "start whale tracker", "stop copying signals".
40. "stealth_reply_toggle" — Toggle Stealth Marketer Reply Mode ON or OFF.
41. "stealth_invite_toggle" — Toggle Stealth Marketer Member Adding ON or OFF.
42. "master_kill" — Engage or release the Master Kill Switch. Stops ALL modules instantly. Use when he says "stop everything", "kill switch", "emergency stop".
43. "check_telegram" — Check if Telegram is connected. Use when he says "is telegram connected".
44. "check_stealth_connection" — Check if the StealthMarketer account is connected. Use when he says "is stealth connected".
45. "health" — READ-ONLY. Full system status: every module, what is connected, what is on or off, whether the bot is awake or sleeping. Changes nothing.
    Use for "how is everything", "system health", "is everything working", "are you awake", "is it sleeping",
    AND for any report or status question about a SINGLE module: "report of the news agent", "how is the website
    module", "is the stealth marketer running", "status of the signal copier", "tell me about the news agent".
    When he asked about one module, read that module's numbers out of the result and answer about that module only.
46. "set_sleep_window" — Change the hours the bot sleeps. Include "start_hour" and "end_hour" (0-23, PKT). Set both to the SAME number for 24/7 always-on. Use when he says "sleep from 1am to 6am", "never sleep", "stay awake all day", "work 24/7".
47. "post_now" — Create AND publish a post to Telegram immediately in one step. Optionally include "category". Use when he says "post now", "publish something now", "send a post right now". (Different from draft_new_content, which only drafts for review.)
48. "invite_now" — Immediately run a subscriber-invite cycle instead of waiting for the hourly schedule. Use when he says "add subscribers", "add members now", "grow the channel", "invite people now".
49. "growth_report" — Show measured subscriber growth and what NOVI recommends changing. Use when he says "how is growth", "are we gaining subscribers", "growth report", "what should we change".

== HOW TO RESPOND ==
RESPOND WITH ONLY A RAW JSON OBJECT. No markdown, no code fences.

Keys:
- "reply": What you say aloud. Be natural, warm, and confident. Address Abdullah by name sometimes. 1-3 sentences max.
- "layout": Where the UI panel appears: "left", "right", "top", "bottom", or "center". Vary it.
- "display": Array of display elements. Each: {"type":"...", "value":"..."}
  Types:
    - "large" — Very big prominent text
    - "heading" — Section heading
    - "stat" — Metric card as "label: value" (ONLY for real bot stats/metrics, never for general knowledge)
    - "text" — Informational text (for general answers, explanations)
    - "highlight" — Important glowing text
  Set to [] for voice-only replies (casual chat, confirmations).
- "action": One of the action strings above, OR omit entirely if it's just a conversation/question.
- "desired": true or false. REQUIRED alongside any *_toggle action when Abdullah said which way he wants it
  ("turn ON the website" -> true, "stop the news agent" -> false). Without it the backend simply flips the
  current state, which can do the opposite of what he asked. Omit only for a bare "toggle X".
  If action is "modify_limits", also include "limit_type" and "new_value".
  If action is "generate_image", also include "headline" and "category".
  If action is "create_post", also include "category".

== CRITICAL RULES ==
1. NEVER use markdown fences. Raw JSON only.
2. When you execute an action, your "reply" should confirm WHAT you are doing, not describe HOW the system works internally.
3. YOU CANNOT GENERATE IMAGES YOURSELF. If asked to generate an image, YOU MUST output "action": "generate_image" and the system will do it for you. DO NOT put an image in the "display" array yourself.
4. DO NOT output fake stats. If he asks a general question like "tell me the news", answer using "text" type, not "stat" type.
5. Be self-aware. If he asks "who are you" or "what can you do", describe yourself as his AI assistant that controls the Daily Pulse PK bot system.
6. If he asks about content the bot posts, explain that you manage a Pakistani news channel that scrapes headlines, generates AI summaries, creates thumbnail images, and posts to Telegram/Reddit.
7. Every action you take is real. Don't say you did something if you're not outputting the action. Don't say email was sent unless action is "test_email".
8. CRITICAL INSTRUCTION: If the user EVER asks to generate, create, or fetch a post, news, or update, YOU MUST OUTPUT 'action': 'create_post'. DO NOT just talk about it. YOU MUST output the action to trigger the process. If they specify a topic like tech, business, world, crypto, pakistan, put it in 'category', otherwise use 'trending'.
9. If asked about Telegram or Stealth connection status, output the appropriate check action.
10. You cover International News, Crypto, Tech, Business, and Pakistani news — NOT just Pakistani news.`;

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: 'POST',
      signal,
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        api_key: localKey,   // empty in production; backend uses its own key
        systemPrompt: systemPrompt,
        input: input
      }),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => null);
      const errorMsg = errData?.error?.message || `Server error: ${res.status}`;
      return {
        display: [
          { type: 'heading', value: 'API ERROR' },
          { type: 'text', value: errorMsg }
        ],
        layout: 'center',
        reply: 'I encountered a server error. Let me try again.',
      };
    }

    const data = await res.json();
    
    if (data.error) {
      console.error("Backend Proxy Error:", data.error.message);
      return {
        display: [{ type: 'text', value: 'API Proxy Error: ' + data.error.message }],
        layout: 'center',
        reply: 'The backend proxy encountered an error: ' + data.error.message,
      };
    }

    if (!data.choices?.[0]) {
      console.error("Empty response data:", data);
      return { display: [], layout: 'right', reply: 'I received an empty response from the AI.' };
    }

    let content = data.choices[0].message.content.trim();
    content = content.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/i, '').trim();

    try {
      const parsed = JSON.parse(content);
      if (!Array.isArray(parsed.display)) {
        if (parsed.title && parsed.text) {
          const lines = parsed.text.split('\n').filter(l => l.trim());
          parsed.display = [
            { type: 'heading', value: parsed.title },
            ...lines.map(l => ({ type: 'text', value: l })),
          ];
        } else {
          parsed.display = [];
        }
      }
      if (!parsed.layout) parsed.layout = 'right';
      return parsed;
    } catch (e) {
      return { display: [], layout: 'right', reply: content.slice(0, 200) };
    }
  } catch (error) {
    if (error.name === 'AbortError') return null;
    console.error("Groq fetch error:", error);
    return {
      display: [{ type: 'text', value: 'Network error: ' + error.message }],
      layout: 'center',
      reply: 'I lost connection. The network error is: ' + error.message,
    };
  }
}

/* ═══════════════════════════════════════════════════════════════ */
const THINKING_MESSAGES = [
  'Analyzing…',
  'Processing…',
  'Querying neural core…',
  'Synthesizing…',
  'Computing…',
];

/*
 * Backend base URL.
 *
 * This was hardcoded to http://localhost:8000, which meant the deployed
 * dashboard could never reach the backend — every action failed in
 * production with "backend not reachable".
 *
 * Resolution order:
 *   1. VITE_API_BASE            (explicit override at build time)
 *   2. same origin              (when the dashboard is served BY the backend)
 *   3. http://localhost:8000    (local dev with `npm run dev`)
 */
/*
 * The production backend. Used whenever the dashboard is served from a host
 * that has no backend of its own (Vercel, Netlify, GitHub Pages).
 *
 * This is a real fallback, not a convenience: Vite inlines env vars at BUILD
 * time, so a "Redeploy" that reuses the build cache silently ships a bundle
 * without VITE_API_BASE — which looks exactly like the backend being down.
 */
const PRODUCTION_API = 'https://a-agent-you-need-only-novi.onrender.com';

const API_BASE = (() => {
  // 1. Explicit build-time override always wins
  const explicit = import.meta.env.VITE_API_BASE;
  if (explicit) return explicit.replace(/\/$/, '');

  if (typeof window !== 'undefined' && window.location) {
    const { origin, hostname, port } = window.location;

    // 2. Runtime override, for testing without a rebuild:
    //    localStorage.setItem('novi_api', 'https://...')
    try {
      const stored = window.localStorage?.getItem('novi_api');
      if (stored) return stored.replace(/\/$/, '');
    } catch (_) { /* storage can be blocked */ }

    const isLocalhost = hostname === 'localhost' || hostname === '127.0.0.1';

    // 3. Static hosts serve the UI but have no /api — use the real backend
    const isStaticHost = /\.(vercel\.app|netlify\.app|github\.io|pages\.dev)$/.test(hostname);
    if (isStaticHost) return PRODUCTION_API;

    // 4. Local dev server (vite on :5173) talks to the local backend
    if (isLocalhost && port && port !== '8000') return 'http://localhost:8000';

    // 5. Otherwise the backend is serving this page — same origin
    if (!isLocalhost) return origin;
  }
  return 'http://localhost:8000';
})();

/* ═══════════════════════════════════════════════════════════════
   MAIN APP
   ═══════════════════════════════════════════════════════════════ */
function App() {
  const [initialized, setInitialized] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [status, setStatus] = useState('OFFLINE');
  const [subtitle, setSubtitle] = useState('');
  const [micEnabled, setMicEnabled] = useState(true);

  /* Live system health, polled from the backend. Without this the toggle
     buttons were fire-and-forget — there was no way to see what was
     actually running without asking NOVI out loud. */
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(false);

  const [hasData, setHasData] = useState(false);
  const [displayItems, setDisplayItems] = useState([]);
  const [panelLayout, setPanelLayout] = useState('right');

  const [sphereX, setSphereX] = useState(0);
  const [sphereY, setSphereY] = useState(0);
  const [sphereScale, setSphereScale] = useState(1);

  const recRef = useRef(null);
  const speakingRef = useRef(false);
  const busyRef = useRef(false);
  const initRef = useRef(false);
  const abortRef = useRef(null);
  const thinkingTimerRef = useRef(null);
  const voicePausedRef = useRef(false);

  /* ── Stop recognition completely ───────────────────────── */
  const stopRecognition = useCallback(() => {
    try { recRef.current?.abort(); } catch (_) { }
    try { recRef.current?.stop(); } catch (_) { }
    setIsListening(false);
  }, []);

  /* ── Start recognition with long cooldown ──────────────── */
  const startRecognition = useCallback(() => {
    if (busyRef.current || speakingRef.current || voicePausedRef.current) return;
    // 1.5s cooldown to let speaker audio fade completely
    setTimeout(() => {
      if (busyRef.current || speakingRef.current || voicePausedRef.current) return;
      try { recRef.current?.start(); } catch (_) { }
    }, 1500);
  }, []);

  /* ── Sphere layout ─────────────────────────────────────── */
  const moveSphere = useCallback((dataVisible, layoutPos = 'right') => {
    if (dataVisible) {
      if (layoutPos === 'left') {
        setSphereX(2.0); setSphereY(0); setSphereScale(0.5);
      } else if (layoutPos === 'right') {
        setSphereX(-2.0); setSphereY(0); setSphereScale(0.5);
      } else if (layoutPos === 'top') {
        setSphereX(0); setSphereY(-1.8); setSphereScale(0.5);
      } else if (layoutPos === 'bottom') {
        setSphereX(0); setSphereY(1.5); setSphereScale(0.45);
      } else if (layoutPos === 'center') {
        setSphereX(-6.0); setSphereY(3.5); setSphereScale(0.15);
      } else {
        setSphereX(-2.0); setSphereY(0); setSphereScale(0.5);
      }
    } else {
      setSphereX(0);
      setSphereY(0);
      setSphereScale(1);
    }
  }, []);

  /* ── Speech Synthesis ──────────────────────────────────── */
  const speak = useCallback((text) => {
    if (!text) {
      busyRef.current = false;
      startRecognition();
      return;
    }
    try {
      if (!('speechSynthesis' in window)) {
        busyRef.current = false;
        startRecognition();
        return;
      }
      window.speechSynthesis.cancel();

      const utter = new SpeechSynthesisUtterance(text);
      utter.lang = 'en-US';
      utter.pitch = 0.95;
      utter.rate = 0.95;

      try {
        const voices = window.speechSynthesis.getVoices();
        if (voices.length > 0) {
          const preferred =
            voices.find(v => v.name.includes('Google') && v.lang.startsWith('en')) ||
            voices.find(v => v.lang.startsWith('en'));
          if (preferred) utter.voice = preferred;
        }
      } catch (e) {
        console.warn("Could not set voice:", e);
      }

      utter.onstart = () => {
        speakingRef.current = true;
        setIsSpeaking(true);
        setStatus('SPEAKING');
        stopRecognition();
      };

      const finish = () => {
        speakingRef.current = false;
        setIsSpeaking(false);
        busyRef.current = false;
        setStatus('LISTENING');
        setSubtitle('Listening…');
        startRecognition();
      };
      utter.onend = finish;
      utter.onerror = finish;

      window.speechSynthesis.speak(utter);
    } catch (err) {
      console.error('Speech error:', err);
      speakingRef.current = false;
      setIsSpeaking(false);
      busyRef.current = false;
      startRecognition();
    }
  }, [stopRecognition, startRecognition]);

  /* ── Speech Recognition ────────────────────────────────── */
  const initRecognition = useCallback(() => {
    try {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) {
        setSubtitle('Speech Recognition not supported — use Chrome.');
        return;
      }

      const rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = 'en-US';
      rec.maxAlternatives = 1;

      rec.onstart = () => {
        if (busyRef.current || speakingRef.current) {
          try { rec.stop(); } catch (_) { }
          return;
        }
        setIsListening(true);
        setStatus('LISTENING');
        setSubtitle('Listening…');
      };

      rec.onresult = (e) => {
        if (busyRef.current || speakingRef.current) return;

        try {
          let interim = '', final = '';
          for (let i = e.resultIndex; i < e.results.length; i++) {
            const transcript = e.results[i][0].transcript;
            const confidence = e.results[i][0].confidence || 0;

            if (e.results[i].isFinal) {
              if (confidence >= 0.65) final += transcript;
            } else {
              interim += transcript;
            }
          }

          if (interim && !busyRef.current) setSubtitle(interim);

          if (final && final.trim()) {
            const trimmed = final.trim();
            const lowerTrimmed = trimmed.toLowerCase();
            
            // Wake word detection: "NOVI" or "NOVI"
            const hasWakeWord = lowerTrimmed.includes('novi') || lowerTrimmed.includes('novi') || lowerTrimmed.includes('no v');
            
            // If voice is paused and wake word is spoken, re-enable
            if (voicePausedRef.current && hasWakeWord) {
              voicePausedRef.current = false;
              setMicEnabled(true);
              setStatus('LISTENING');
              setSubtitle('Wake word detected! Listening...');
              // Strip the wake word and process the rest as a command
              let command = trimmed.replace(/\b(novi|novi|no v)\b/gi, '').trim();
              if (command.length >= 5) {
                handleCommand(command);
              }
              return;
            }
            
            // If voice is paused, ignore all input except wake word
            if (voicePausedRef.current) return;
            
            const words = trimmed.split(/\s+/).length;
            if (words >= 2 || trimmed.length >= 5) {
              // Strip wake word prefix from command if present
              let command = trimmed.replace(/\b(novi|novi|no v)\b/gi, '').trim();
              if (!command) command = trimmed;
              setSubtitle(command);
              handleCommand(command);
            }
          }
        } catch (err) {
          console.error('Recognition error:', err);
        }
      };

      rec.onerror = (e) => {
        console.error('Recognition error:', e.error);
        setIsListening(false);
        setSubtitle('Mic Error: ' + e.error + ' (Check site settings)');
      };

      rec.onend = () => {
        setIsListening(false);
        if (!busyRef.current && !speakingRef.current) {
          setTimeout(() => {
            if (!busyRef.current && !speakingRef.current) {
              try { rec.start(); } catch (_) { }
            }
          }, 800);
        }
      };

      recRef.current = rec;
    } catch (err) {
      console.error('Failed to init recognition:', err);
    }
  }, []);

  /* ═══════════════════════════════════════════════════════════
     COMMAND HANDLER
     ═══════════════════════════════════════════════════════════ */
  const handleCommand = useCallback((input) => {
    busyRef.current = true;
    stopRecognition();

    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    try { window.speechSynthesis?.cancel(); } catch (_) { }
    if (thinkingTimerRef.current) { clearInterval(thinkingTimerRef.current); }

    setStatus('THINKING');
    let msgIdx = Math.floor(Math.random() * THINKING_MESSAGES.length);
    setSubtitle(THINKING_MESSAGES[msgIdx]);
    thinkingTimerRef.current = setInterval(() => {
      msgIdx = (msgIdx + 1) % THINKING_MESSAGES.length;
      setSubtitle(THINKING_MESSAGES[msgIdx]);
    }, 2000);

    const controller = new AbortController();
    abortRef.current = controller;

    (async () => {
      // Use Groq for intelligent command routing instead of hardcoding
      const response = await callGroq(input, controller.signal);

      if (thinkingTimerRef.current) {
        clearInterval(thinkingTimerRef.current);
        thinkingTimerRef.current = null;
      }

      if (!response) {
        busyRef.current = false;
        setStatus('LISTENING');
        setSubtitle('Listening…');
        startRecognition();
        return;
      }

      if (response.action === 'clear') {
        setHasData(false);
        setDisplayItems([]);
        moveSphere(false);
        setSubtitle('');
        speak(response.reply || 'Cleared.');
        return;
      }

      let textToSpeak = response.reply;
      let actionItems = response.display || [];

      // Handle Bot Control Actions — call the real API and use the REAL result
      try {
        if (response.action === 'test_email') {
          const apiRes = await fetch(`${API_BASE}/api/test_email`, { method: 'POST' });
          if (apiRes.ok) {
            textToSpeak = response.reply || "Done, Abdullah. Test email sent to your inbox.";
          } else {
            const err = await apiRes.json().catch(() => ({}));
            textToSpeak = `The email failed, Abdullah. Error: ${err.detail || 'Check your Gmail App Password in the .env file.'}`;
          }
        } else if (response.action === 'modify_limits') {
          const apiRes = await fetch(`${API_BASE}/api/modify_limits`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ limit_type: response.limit_type, new_value: response.new_value })
          });
          if (apiRes.ok) {
            const data = await apiRes.json();
            // Always speak the REAL applied result, so a capped invite limit
            // is never reported back as the number he asked for.
            textToSpeak = data.message;
            actionItems = [
              { type: 'heading', value: 'Limit Updated' },
              { type: 'highlight', value: data.message },
            ];
          } else {
            const err = await apiRes.json().catch(() => ({}));
            textToSpeak = `I couldn't update the limits. ${err.detail || 'The backend might not be running.'}`;
          }
        } else if (response.action === 'pin_now') {
          const apiRes = await fetch(`${API_BASE}/api/pins/run_now`, { method: 'POST' });
          if (apiRes.ok) {
            const data = await apiRes.json();
            textToSpeak = response.reply || data.message;
            actionItems = [
              { type: 'heading', value: data.status === 'awaiting_review'
                  ? 'Pin awaiting your review' : 'Pin published' },
              { type: 'highlight', value: data.title },
            ];
          } else {
            const err = await apiRes.json().catch(() => ({}));
            textToSpeak = `I could not build a pin. ${err.detail || ''}`;
          }
        } else if (response.action === 'post_now') {
          textToSpeak = "Working on it, Abdullah. Scraping the news and publishing now.";
          setSubtitle('Creating and publishing post...');
          const apiRes = await fetch(`${API_BASE}/api/post_now`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ category: response.category || '', publish: true })
          });
          if (apiRes.ok) {
            const data = await apiRes.json();
            const pkg = data.package || {};
            textToSpeak = data.message || "Published, Abdullah.";
            actionItems = [
              {
                type: 'post_preview',
                package: pkg,
                image_url: pkg.image_url ? (API_BASE + pkg.image_url) : null
              }
            ];
          } else {
            const err = await apiRes.json().catch(() => ({}));
            textToSpeak = `I couldn't publish that. ${err.detail || 'Check the logs.'}`;
          }
        } else if (response.action === 'invite_now') {
          const apiRes = await fetch(`${API_BASE}/api/growth/invite_now`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: response.count || 1 })
          });
          if (apiRes.ok) {
            const data = await apiRes.json();
            textToSpeak = data.message;
            actionItems = [
              { type: 'heading', value: 'Subscriber Growth' },
              { type: 'highlight', value: data.message },
              { type: 'stat', value: `Invites today: ${data.invites_today ?? 0} / ${data.daily_limit ?? 0}` },
            ];
          } else {
            const err = await apiRes.json().catch(() => ({}));
            textToSpeak = `I couldn't start the invite cycle. ${err.detail || ''}`;
          }
        } else if (response.action === 'growth_report') {
          const apiRes = await fetch(`${API_BASE}/api/growth/status`);
          if (apiRes.ok) {
            const g = await apiRes.json();
            const rec = g.recommendation || {};
            const wk = g.weekly || {};
            textToSpeak = `${rec.headline || 'Here is the growth report.'} ${
              (rec.suggested_changes || []).map(c => c.why).join(' ')}`;
            actionItems = [
              { type: 'heading', value: 'Growth Report' },
              { type: 'large', value: `${g.subscribers ?? '—'} subscribers` },
              { type: 'stat', value: `Last 24h: ${g.growth_24h == null ? 'measuring…' : (g.growth_24h > 0 ? '+' : '') + g.growth_24h}` },
              { type: 'stat', value: `This week: ${wk.gained ?? 0} / ${wk.goal ?? 0} (${wk.percent ?? 0}%)` },
              { type: 'highlight', value: rec.headline || '' },
              ...(rec.suggested_changes || []).map(c => ({ type: 'text', value: `• ${c.what}: ${c.why}` })),
            ];
          } else {
            textToSpeak = "I can't reach the growth data right now.";
          }
        } else if (response.action === 'health') {
          const apiRes = await fetch(`${API_BASE}/api/health`);
          if (apiRes.ok) {
            const h = await apiRes.json();
            const m = h.modules || {};
            textToSpeak = h.awake
              ? `Everything is up, Abdullah. It's ${h.current_time_pkt} and the bot is awake.`
              : `The bot is currently ${h.master_kill ? 'stopped by the kill switch' : 'sleeping'}, Abdullah. It's ${h.current_time_pkt}.`;
            actionItems = [
              { type: 'heading', value: 'System Health' },
              { type: 'stat', value: `Now: ${h.current_time_pkt}` },
              { type: 'stat', value: `State: ${h.awake ? '🟢 Awake' : '😴 Sleeping'}` },
              { type: 'stat', value: `Sleep window: ${h.always_on ? '24/7 — never sleeps' : h.sleep_window}` },
              { type: 'stat', value: `News Agent: ${m.news_agent?.active ? '🟢 ON' : '🔴 OFF'} (TG ${m.news_agent?.telegram_connected ? '✓' : '✗'})` },
              { type: 'stat', value: `Signals: ${m.signal_copier?.active ? '🟢 ON' : '🔴 OFF'} — ${m.signal_copier?.signals_copied_today ?? 0} today` },
              { type: 'stat', value: `Stealth: ${m.stealth_marketer?.active ? '🟢 ON' : '🔴 OFF'} — ${m.stealth_marketer?.invites_today ?? 0}/${m.stealth_marketer?.max_invites_per_day ?? 0} invites` },
              { type: 'stat', value: `Database: ${h.database_connected ? '🟢 Connected' : '🔴 Offline'}` },
            ];
          } else {
            textToSpeak = "I can't reach the backend to check health.";
          }
        } else if (response.action === 'set_sleep_window') {
          const apiRes = await fetch(`${API_BASE}/api/schedule/sleep_window`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_hour: response.start_hour, end_hour: response.end_hour })
          });
          if (apiRes.ok) {
            const data = await apiRes.json();
            textToSpeak = data.message;
            actionItems = [
              { type: 'heading', value: 'Schedule Updated' },
              { type: 'highlight', value: data.message },
            ];
          } else {
            textToSpeak = "I couldn't change the sleep schedule.";
          }
        } else if (response.action === 'status' || response.action === 'stealth_status') {
          const apiRes = await fetch(`${API_BASE}/api/modules/status`);
          if (apiRes.ok) {
            const s = await apiRes.json();
            textToSpeak = response.reply || `Here is the system status, Abdullah.`;
            actionItems = [
              { type: 'heading', value: 'Live System Status' },
              { type: 'stat', value: `News Agent: ${s.news_agent ? '🟢 ON' : '🔴 OFF'}` },
              { type: 'stat', value: `Signal Copier: ${s.signal_copier ? '🟢 ON' : '🔴 OFF'}` },
              { type: 'stat', value: `Stealth Reply: ${s.stealth_reply_mode ? '🟢 ON' : '🔴 OFF'}` },
              { type: 'stat', value: `Stealth Invite: ${s.stealth_invite_mode ? '🟢 ON' : '🔴 OFF'}` },
              { type: 'stat', value: `Master Kill: ${s.master_kill ? '⚠️ ENGAGED' : '✅ Clear'}` },
            ];
          } else {
            textToSpeak = "I can't reach the backend server. Make sure main.py is running, Abdullah.";
          }
        } else if (response.action === 'generate_image') {
          const apiRes = await fetch(`${API_BASE}/api/generate_image`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ headline: response.headline, category: response.category || 'default' })
          });
          if (apiRes.ok) {
            const data = await apiRes.json();
            textToSpeak = response.reply || "Image generated and saved, Abdullah.";
            if (data.image_url) {
              actionItems = [
                { type: 'heading', value: 'Generated Image' },
                { type: 'image', value: `${API_BASE}${data.image_url}` }
              ];
            }
          } else {
            textToSpeak = "Image generation failed. The backend might be offline or Pollinations AI timed out.";
          }
        } else if (response.action === 'draft_new_content') {
          textToSpeak = response.reply || "I am dispatching the sub-agent now, Abdullah. I will notify you when the post is ready.";
          actionItems = [
            { type: 'heading', value: 'Sub-Agent Deployed' },
            { type: 'text', value: 'Fetching latest news across all sources...' }
          ];
          
          const cat = encodeURIComponent(response.category || '');
          
          // Fire and forget the stream (Sub-Agent works in background)
          (async () => {
            try {
              const streamRes = await fetch(`${API_BASE}/api/create_post_stream?category=${cat}`);
              if (streamRes.ok && streamRes.body) {
                const reader = streamRes.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let buffer = '';
                
                while (true) {
                  const { done, value } = await reader.read();
                  if (done) break;
                  
                  buffer += decoder.decode(value, { stream: true });
                  const parts = buffer.split('\n\n');
                  buffer = parts.pop();
                  
                  for (const part of parts) {
                    const trimmed = part.trim();
                    if (trimmed.startsWith('data: ')) {
                      try {
                        const eventData = JSON.parse(trimmed.substring(6));
                        
                        if (eventData.step === 'error') {
                          setSubtitle('Sub-agent error: ' + eventData.message);
                          speak('The sub-agent encountered an error.');
                        } else if (eventData.step === 'result') {
                          const pkg = eventData.package;
                          speak("The sub-agent has finished. Here is the generated post, Abdullah.");
                          setSubtitle("Post ready!");
                          
                          const finalItems = [
                            { 
                              type: 'post_preview', 
                              package: pkg,
                              image_url: pkg.image_url ? (API_BASE + pkg.image_url) : null
                            }
                          ];
                          
                          setHasData(true);
                          setDisplayItems(finalItems);
                          setPanelLayout('center');
                          moveSphere(true, 'center');
                        } else {
                          // Sub-agent progress report
                          setSubtitle(`Sub-agent: ${eventData.message}`);
                        }
                      } catch (parseErr) {}
                    }
                  }
                }
              }
            } catch (streamErr) {
              console.error("Sub-agent stream error:", streamErr);
              speak('The sub-agent failed to complete the task.');
            }
          })();
          
          // Do not return! Let the main flow render the "Sub-Agent Deployed" message.
        } else if (response.action === 'publish_to_channels') {
          const apiRes = await fetch(`${API_BASE}/api/publish_post`, { method: 'POST' });
          if (apiRes.ok) {
            textToSpeak = response.reply || "The post has been published to your channels, Abdullah!";
            actionItems = [
              { type: 'heading', value: 'Post Published' },
              { type: 'text', value: 'Successfully broadcasted to Telegram and X.' }
            ];
          } else {
            const err = await apiRes.json().catch(() => ({}));
            textToSpeak = `I could not publish the post. ${err.detail || 'Make sure you generated a post first.'}`;
          }
        } else if (['news_toggle', 'website_toggle', 'pin_toggle', 'signal_toggle', 'stealth_reply_toggle', 'stealth_invite_toggle', 'master_kill', 'social_toggle', 'social_facebook_toggle', 'social_twitter_toggle', 'social_threads_toggle', 'social_bluesky_toggle', 'binance_toggle'].includes(response.action)) {
          let endpoint = '';
          if (response.action === 'news_toggle') endpoint = '/api/news/toggle';
          if (response.action === 'website_toggle') endpoint = '/api/website/toggle';
          if (response.action === 'pin_toggle') endpoint = '/api/pins/toggle';
          if (response.action === 'binance_toggle') endpoint = '/api/binance/toggle';
          if (response.action === 'social_toggle') endpoint = '/api/social/toggle';
          if (response.action === 'social_facebook_toggle') endpoint = '/api/social/facebook/toggle';
          if (response.action === 'social_twitter_toggle') endpoint = '/api/social/twitter/toggle';
          if (response.action === 'social_threads_toggle') endpoint = '/api/social/threads/toggle';
          if (response.action === 'social_bluesky_toggle') endpoint = '/api/social/bluesky/toggle';
          if (response.action === 'signal_toggle') endpoint = '/api/signal_copier/toggle';
          if (response.action === 'stealth_reply_toggle') endpoint = '/api/stealth/toggle_reply';
          if (response.action === 'stealth_invite_toggle') endpoint = '/api/stealth/toggle_invite';
          if (response.action === 'master_kill') endpoint = '/api/master_kill';

          // Send the state NOVI actually intended. Without this the backend
          // just inverts whatever it finds, so a misread question could switch
          // a module off — which is how "report of the news agent" once
          // disabled the news agent.
          const body = typeof response.desired === 'boolean'
            ? JSON.stringify({ active: response.desired })
            : null;

          const apiRes = await fetch(`${API_BASE}${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
          });
          if (apiRes.ok) {
            const data = await apiRes.json();
            textToSpeak = response.reply || data.message;
            actionItems = [
              { type: 'heading', value: 'Module Status Updated' },
              { type: 'highlight', value: data.message }
            ];
          } else {
            textToSpeak = "I couldn't toggle the module. The backend might not be running.";
          }
        } else if (response.action === 'check_telegram') {
          const apiRes = await fetch(`${API_BASE}/api/telegram/connection_status`);
          if (apiRes.ok) {
            const s = await apiRes.json();
            textToSpeak = s.connected
              ? `Telegram is connected, Abdullah. Logged in as ${s.name}.`
              : "Telegram is NOT connected. You need to authorize it from the dashboard.";
            actionItems = [
              { type: 'heading', value: 'Telegram Connection' },
              { type: 'stat', value: `Status: ${s.connected ? '🟢 Connected' : '🔴 Disconnected'}` },
              { type: 'stat', value: `Name: ${s.name || 'N/A'}` },
              { type: 'stat', value: `Phone: ${s.phone || 'N/A'}` },
            ];
          } else {
            textToSpeak = "I can't check the Telegram connection. Backend might be offline.";
          }
        } else if (response.action === 'check_stealth_connection') {
          const apiRes = await fetch(`${API_BASE}/api/stealth/connection_status`);
          if (apiRes.ok) {
            const s = await apiRes.json();
            textToSpeak = s.connected
              ? `The Stealth Marketer account is connected, Abdullah. Logged in as ${s.name}.`
              : "The Stealth Marketer account is NOT connected. You need to authorize the burner number from the dashboard.";
            actionItems = [
              { type: 'heading', value: 'Stealth Connection' },
              { type: 'stat', value: `Status: ${s.connected ? '🟢 Connected' : '🔴 Disconnected'}` },
              { type: 'stat', value: `Name: ${s.name || 'N/A'}` },
            ];
          } else {
            textToSpeak = "I can't check the Stealth connection. Backend might be offline.";
          }
        }
      } catch (err) {
        console.error("API Action Error:", err);
        textToSpeak = "I tried to execute your command, but the backend server is not reachable right now.";
      }

      const items = actionItems;
      const layoutChoice = response.layout || 'right';

      // Fallback: if AI forgot "reply" but sent display text, read the text
      if (!textToSpeak && items.length > 0) {
        textToSpeak = items.map(i => i.value).join('. ');
      }

      console.log("AI Response:", response);
      console.log("Speaking text:", textToSpeak);

      if (items.length > 0) {
        setHasData(true);
        setDisplayItems(items);
        setPanelLayout(layoutChoice);
        moveSphere(true, layoutChoice);
        setSubtitle('');
      } else {
        setHasData(false);
        setDisplayItems([]);
        moveSphere(false);
        setSubtitle(textToSpeak || '');
      }

      speak(textToSpeak || 'Done.');
    })();
  }, [stopRecognition, startRecognition, moveSphere, speak]);

  /* ── Boot ───────────────────────────────────────────────── */
  const handleInit = useCallback(() => {
    if (initRef.current) return;
    initRef.current = true;

    try {
      window.speechSynthesis?.getVoices();
      if (window.speechSynthesis?.onvoiceschanged !== undefined) {
        window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
      }
    } catch (_) { }

    setInitialized(true);
    initRecognition();

    setTimeout(() => {
      try { recRef.current?.start(); } catch (_) { }
      speak('Novi is online.');
    }, 600);
  }, [initRecognition, speak]);

  /* ── Live health polling ───────────────────────────────── */
  const refreshHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/health`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setHealth(await res.json());
      setHealthError(false);
    } catch {
      setHealthError(true);
    }
  }, []);

  useEffect(() => {
    if (!initialized) return;
    refreshHealth();
    const id = setInterval(refreshHealth, 15000);
    return () => clearInterval(id);
  }, [initialized, refreshHealth]);

  /* ── Cleanup ───────────────────────────────────────────── */
  useEffect(() => {
    return () => {
      if (abortRef.current) abortRef.current.abort();
      if (thinkingTimerRef.current) clearInterval(thinkingTimerRef.current);
      try { window.speechSynthesis?.cancel(); } catch (_) { }
      try { recRef.current?.abort(); } catch (_) { }
    };
  }, []);

  const aiState = isSpeaking ? 'speaking'
    : status === 'THINKING' ? 'thinking'
      : isListening ? 'listening'
        : 'idle';

  /* ═══════════════════════════════════════════════════════════
     RENDER
     ═══════════════════════════════════════════════════════════ */
  return (
    <div className="app-container">
      <div className="canvas-container">
        <Canvas
          camera={{ position: [0, 0, 6], fov: 50 }}
          dpr={[1, 2]}
          onCreated={({ gl }) => gl.setClearColor('#000000')}
        >
          <ambientLight intensity={0.08} />
          {initialized && (
            <Suspense fallback={null}>
              <ChatGPTOrb
                aiState={aiState}
                targetX={sphereX}
                targetY={sphereY}
                targetScale={sphereScale}
              />
            </Suspense>
          )}
        </Canvas>
      </div>

      <div className="ui-layer">
        <header className="header">
          <h1 className="logo">
            NOVI
            {initialized && (
              <span className={`status-badge status-${status.toLowerCase()}`}>
                <span className="pulse-dot" />
                {status}
              </span>
            )}
          </h1>

          {/* Control Buttons */}
          {initialized && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginTop: '12px', justifyContent: 'center' }}>
              {/* Mic Toggle Button */}
              <button
                id="mic-toggle-btn"
                className="control-btn"
                onClick={() => {
                  if (micEnabled) {
                    voicePausedRef.current = true;
                    setMicEnabled(false);
                    stopRecognition();
                    setStatus('PAUSED');
                    setSubtitle('Voice paused. Say "NOVI" to wake me up.');
                  } else {
                    voicePausedRef.current = false;
                    setMicEnabled(true);
                    setStatus('LISTENING');
                    setSubtitle('Listening…');
                    startRecognition();
                  }
                }}
                style={{
                  background: micEnabled ? 'rgba(56, 189, 95, 0.15)' : 'rgba(239, 68, 68, 0.15)',
                  border: `1px solid ${micEnabled ? 'rgba(56, 189, 95, 0.4)' : 'rgba(239, 68, 68, 0.4)'}`,
                  color: micEnabled ? '#38bd5f' : '#ef4444',
                  padding: '6px 12px', borderRadius: '12px', fontSize: '0.6rem', fontWeight: 600,
                  cursor: 'pointer', textTransform: 'uppercase', backdropFilter: 'blur(12px)'
                }}
              >
                {micEnabled ? '🎤 MIC ON' : '🔇 MIC OFF'}
              </button>

              {/* Module Toggles — each reflects its REAL live state */}
              {[
                { label: '📰 NEWS AGENT', action: 'news_toggle', color: '14, 165, 233',
                  on: health?.modules?.news_agent?.active },
                { label: '🌐 WEBSITE', action: 'website_toggle', color: '99, 102, 241',
                  on: health?.modules?.website?.active },
                { label: '📌 PINTEREST', action: 'pin_toggle', color: '198, 106, 58',
                  on: health?.modules?.pin_agent?.active },
                { label: '🟡 BINANCE', action: 'binance_toggle', color: '240, 185, 11',
                  on: health?.binance?.active },
                { label: '📣 SOCIAL', action: 'social_toggle', color: '236, 72, 153',
                  on: health?.social?.active },
                { label: '📘 FACEBOOK', action: 'social_facebook_toggle', color: '24, 119, 242',
                  on: health?.social?.platforms_on?.facebook },
                { label: '𝕏 TWITTER', action: 'social_twitter_toggle', color: '120, 120, 130',
                  on: health?.social?.platforms_on?.twitter },
                { label: '🧵 THREADS', action: 'social_threads_toggle', color: '90, 90, 100',
                  on: health?.social?.platforms_on?.threads },
                { label: '🦋 BLUESKY', action: 'social_bluesky_toggle', color: '0, 133, 255',
                  on: health?.social?.platforms_on?.bluesky },
                { label: '🐳 SIGNAL COPIER', action: 'signal_toggle', color: '245, 158, 11',
                  on: health?.modules?.signal_copier?.active },
                { label: '💬 STEALTH REPLY', action: 'stealth_reply_toggle', color: '139, 92, 246',
                  on: health?.modules?.stealth_marketer?.reply_mode },
                { label: '📥 STEALTH INVITE', action: 'stealth_invite_toggle', color: '16, 185, 129',
                  on: health?.modules?.stealth_marketer?.scrape_mode },
                { label: '🛑 MASTER KILL', action: 'master_kill', color: '239, 68, 68',
                  on: health?.master_kill }
              ].map(btn => (
                <button
                  key={btn.action}
                  className="control-btn"
                  onClick={async () => {
                    setSubtitle(`Triggering ${btn.label}...`);
                    try {
                      let endpoint = '';
                      if (btn.action === 'news_toggle') endpoint = '/api/news/toggle';
                      if (btn.action === 'pin_toggle') endpoint = '/api/pins/toggle';
                      if (btn.action === 'binance_toggle') endpoint = '/api/binance/toggle';
                      if (btn.action === 'social_toggle') endpoint = '/api/social/toggle';
                      if (btn.action === 'social_facebook_toggle') endpoint = '/api/social/facebook/toggle';
                      if (btn.action === 'social_twitter_toggle') endpoint = '/api/social/twitter/toggle';
                      if (btn.action === 'social_threads_toggle') endpoint = '/api/social/threads/toggle';
                      if (btn.action === 'social_bluesky_toggle') endpoint = '/api/social/bluesky/toggle';
                      if (btn.action === 'website_toggle') endpoint = '/api/website/toggle';
                      if (btn.action === 'signal_toggle') endpoint = '/api/signal_copier/toggle';
                      if (btn.action === 'stealth_reply_toggle') endpoint = '/api/stealth/toggle_reply';
                      if (btn.action === 'stealth_invite_toggle') endpoint = '/api/stealth/toggle_invite';
                      if (btn.action === 'master_kill') endpoint = '/api/master_kill';
                      
                      const res = await fetch(`${API_BASE}${endpoint}`, { method: 'POST' });
                      if (res.ok) {
                        const data = await res.json();
                        setSubtitle(data.message);
                        speak(data.message);
                        setHasData(true);
                        setDisplayItems([
                          { type: 'heading', value: 'System Updated' },
                          { type: 'highlight', value: data.message }
                        ]);
                        setPanelLayout('center');
                        moveSphere(true, 'center');
                        refreshHealth();   // reflect the new state immediately
                      }
                    } catch (err) {
                      setSubtitle('Error: Cannot reach backend server.');
                      speak('Cannot reach the backend server.');
                    }
                  }}
                  title={btn.on ? 'Currently ON — click to turn off'
                                : 'Currently OFF — click to turn on'}
                  style={{
                    /* Solid when live, ghosted when off, so state is readable
                       at a glance instead of every button looking identical. */
                    background: btn.on ? `rgba(${btn.color}, 0.28)` : 'rgba(255,255,255,0.04)',
                    border: `1px solid rgba(${btn.color}, ${btn.on ? 0.85 : 0.25})`,
                    color: btn.on ? `rgb(${btn.color})` : 'rgba(255,255,255,0.45)',
                    boxShadow: btn.on ? `0 0 14px rgba(${btn.color}, 0.35)` : 'none',
                    padding: '6px 12px', borderRadius: '12px', fontSize: '0.6rem', fontWeight: 600,
                    cursor: 'pointer', textTransform: 'uppercase', backdropFilter: 'blur(12px)',
                    display: 'inline-flex', alignItems: 'center', gap: '6px',
                    transition: 'all .2s ease', opacity: health ? 1 : 0.55
                  }}
                >
                  <span style={{
                    width: 6, height: 6, borderRadius: '50%', flex: 'none',
                    background: btn.on ? `rgb(${btn.color})` : 'rgba(255,255,255,0.3)'
                  }} />
                  {btn.label}
                </button>
              ))}

              {/* Live status strip — answers "is it working right now?"
                  without having to ask out loud. */}
              <div style={{
                width: '100%', display: 'flex', justifyContent: 'center',
                gap: '14px', flexWrap: 'wrap', marginTop: '10px',
                fontSize: '0.58rem', letterSpacing: '.06em', textTransform: 'uppercase',
                fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
                color: 'rgba(255,255,255,0.5)'
              }}>
                {healthError && (
                  <span style={{ color: '#ef4444' }}>
                    ⚠ backend unreachable — tried {API_BASE}
                  </span>
                )}
                {health && !healthError && (
                  <>
                    <span style={{ color: health.awake ? '#38bd5f' : '#f59e0b' }}>
                      {health.awake ? '● awake' : '◌ sleeping'}
                    </span>
                    <span>{health.current_time_pkt} PKT</span>
                    <span>sleep {health.always_on ? 'never' : health.sleep_window}</span>
                    <span>
                      posts {health.modules?.news_agent?.posts_today ?? 0}/
                      {health.modules?.news_agent?.max_posts ?? 0}
                    </span>
                    <span>
                      invites {health.modules?.stealth_marketer?.invites_today ?? 0}/
                      {health.modules?.stealth_marketer?.max_invites_per_day ?? 0}
                    </span>
                    <span>signals {health.modules?.signal_copier?.signals_copied_today ?? 0}</span>
                    <span style={{ color: health.database_connected ? '#38bd5f' : '#ef4444' }}>
                      db {health.database_connected ? 'ok' : 'down'}
                    </span>
                    {health.growth?.subscribers != null && (
                      <span>subs {health.growth.subscribers}
                        {health.growth.growth_24h != null &&
                          ` (${health.growth.growth_24h >= 0 ? '+' : ''}${health.growth.growth_24h})`}
                      </span>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </header>

        {/* Loading UI overlay */}
        <div className={`data-panel-wrapper layout-center ${status === 'THINKING' && !hasData ? 'visible' : ''}`} style={{ zIndex: 10, pointerEvents: 'none' }}>
          {status === 'THINKING' && !hasData && (
            <div className="glass-panel" style={{ alignItems: 'center', justifyContent: 'center', minHeight: '200px', width: '300px', margin: '0 auto', textAlign: 'center' }}>
              <div style={{ width: '40px', height: '40px', border: '3px solid rgba(255,255,255,0.1)', borderTopColor: '#fff', borderRadius: '50%', animation: 'spin 1s linear infinite', margin: '0 auto 16px' }} />
              <h3 style={{ color: '#fff', letterSpacing: '2px', fontSize: '0.9rem', marginBottom: '8px' }}>PROCESSING</h3>
              <p style={{ color: 'rgba(255,255,255,0.5)', fontSize: '0.75rem', margin: 0 }}>Please wait a moment...</p>
            </div>
          )}
        </div>

        {/* Rich Data Panel */}
        <div className={`data-panel-wrapper ${hasData ? 'visible' : ''} layout-${panelLayout}`}>
          {hasData && (
            <div className="glass-panel">
              {displayItems.map((item, i) => {
                const delay = { animationDelay: `${i * 0.12}s` };

                if (item.type === 'large') {
                  return <div key={i} className="display-large card-anim" style={delay}>{item.value}</div>;
                }
                if (item.type === 'heading') {
                  return <h2 key={i} className="display-heading card-anim" style={delay}>{item.value}</h2>;
                }
                if (item.type === 'stat') {
                  const parts = item.value.split(':');
                  const label = parts[0]?.trim();
                  const val = parts.slice(1).join(':')?.trim();
                  return (
                    <div key={i} className="display-stat card-anim" style={delay}>
                      <span className="stat-dot" />
                      <span className="stat-label">{label}</span>
                      {val && <span className="stat-value">{val}</span>}
                    </div>
                  );
                }
                if (item.type === 'image') {
                  return (
                    <div key={i} className="display-image card-anim" style={delay}>
                      <img src={item.value} alt="Generated UI Output" style={{ maxWidth: '100%', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.1)' }} />
                    </div>
                  );
                }
                if (item.type === 'post_preview') {
                  const p = item.package;
                  return (
                    <div key={i} className="post-preview card-anim" style={delay}>
                      {p.real_image_url && (
                        <div className="post-preview-real-img" style={{ padding: '12px 20px 0 20px' }}>
                          <h4 style={{ fontSize: '0.6rem', color: 'rgba(255,255,255,0.35)', marginBottom: '6px', letterSpacing: '1px' }}>REAL NEWS PHOTO</h4>
                          <img src={p.real_image_url} alt="Real news" style={{ width: '100%', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.08)' }} />
                        </div>
                      )}
                      
                      <div className="post-preview-content">
                        {/* ── Telegram Post ── */}
                        <div className="post-preview-platform">
                          <div className="platform-header">
                            <span className="platform-icon" style={{ background: '#0088cc' }}>T</span>
                            <h4>TELEGRAM POST</h4>
                          </div>
                          {item.image_url && (
                            <div style={{ marginBottom: '24px' }}>
                              <img src={item.image_url} alt="Telegram thumbnail" style={{ width: '100%', borderRadius: '16px', boxShadow: '0 10px 30px rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.05)' }} />
                              {p.headline && (
                                <h3 style={{ fontSize: '1.3rem', fontWeight: 700, color: '#fff', marginTop: '20px', marginBottom: '8px', lineHeight: 1.4, letterSpacing: '0.5px' }}>
                                  {p.headline}
                                </h3>
                              )}
                            </div>
                          )}
                          <p>{p.telegram_text}</p>
                        </div>
                        
                        {/* ── Twitter/X Post ── */}
                        <div className="post-preview-platform">
                          <div className="platform-header">
                            <span className="platform-icon" style={{ background: '#1d9bf0' }}>𝕏</span>
                            <h4>TWEET / X POST</h4>
                          </div>
                          {item.image_url && (
                            <div style={{ marginBottom: '24px' }}>
                              <img src={item.image_url} alt="Twitter thumbnail" style={{ width: '100%', borderRadius: '16px', boxShadow: '0 10px 30px rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.05)' }} />
                              {p.headline && (
                                <h3 style={{ fontSize: '1.3rem', fontWeight: 700, color: '#fff', marginTop: '20px', marginBottom: '8px', lineHeight: 1.4, letterSpacing: '0.5px' }}>
                                  {p.headline}
                                </h3>
                              )}
                            </div>
                          )}
                          <p>{p.tweet_text}</p>
                        </div>
                        
                        {/* ── Reddit Post ── */}
                        {p.reddit_title && (
                          <div className="post-preview-platform">
                            <div className="platform-header">
                              <span className="platform-icon" style={{ background: '#ff4500' }}>R</span>
                              <h4>REDDIT POST</h4>
                            </div>
                            {item.image_url && (
                              <div style={{ marginBottom: '24px' }}>
                                <img src={item.image_url} alt="Reddit thumbnail" style={{ width: '100%', borderRadius: '16px', boxShadow: '0 10px 30px rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.05)' }} />
                              </div>
                            )}
                            <p style={{ fontWeight: 600, marginBottom: '6px', color: 'rgba(255,255,255,0.95)' }}>{p.reddit_title}</p>
                            <p style={{ fontSize: '0.82rem', color: 'rgba(255,255,255,0.7)' }}>{p.reddit_body}</p>
                          </div>
                        )}
                        
                        <div className="post-preview-meta">
                          <span className="source-credits">Sources: {p.source_credits}</span>
                        </div>
                      </div>
                    </div>
                  );
                }
                if (item.type === 'highlight') {
                  return <div key={i} className="display-highlight card-anim" style={delay}>{item.value}</div>;
                }
                return <div key={i} className="display-text card-anim" style={delay}>{item.value}</div>;
              })}
            </div>
          )}
        </div>

        {initialized && (
          <div className="subtitle-bar">
            <span className={`subtitle ${status === 'THINKING' ? 'thinking-pulse' : ''}`}>
              {subtitle}
            </span>
          </div>
        )}
      </div>

      {!initialized && (
        <button className="init-btn" onClick={handleInit}>
          INITIALIZE
        </button>
      )}
    </div>
  );
}

export default App;
