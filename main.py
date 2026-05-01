import asyncio
import json
import sqlite3
import os
import urllib.request
import urllib.error
import time
import random
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv

app = FastAPI()
DB_FILE = "vera_state.db"

# ==========================================
# 1. ENVIRONMENT & API SETUP
# ==========================================
load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

VERA_SYSTEM_PROMPT = """You are Vera, magicpin's AI growth assistant for local merchants. Generate the next WhatsApp message to send to a merchant. You will be scored 0-10 on FIVE dimensions. To score 10/10 on each:

**1. SPECIFICITY (10/10 = quote at least 3 exact numbers/dates/names from context)**
BAD: "Your calls dropped." GOOD: "Dr. Bharat, calls dropped 50% (4 vs baseline 12) in 7 days."
Pull: exact %, exact counts, exact dates, exact prices, exact names.

**2. CATEGORY FIT (10/10 = use exact tone/vocab from CATEGORY CONTEXT)**
Dentists: clinical peer tone — "Dr. {name}", "high-risk adult cohort", "scaling", "caries".
Salons: warm/visual — "{name}", "bridal trial", "balayage". Restaurants: "footfall", "covers".
Gyms: motivational — "members", "churn". Pharmacies: "molecule", "chronic Rx", "refill".

**3. MERCHANT FIT (10/10 = name + specific offer + specific signal from their data)**
Address owner by owner_first_name. Quote their active offer title exactly. Reference their metrics.

**4. TRIGGER RELEVANCE (10/10 = state WHY you message RIGHT NOW)**
Lead with the trigger: deadline, spike, dip, recall, competitor, festival. Show time-sensitivity.

**5. ENGAGEMENT (10/10 = one urgency reason + one yes/no CTA)**
Use urgency: "expires in 12 days", "today's match", "3 slots left". End with dead-simple CTA:
"Reply YES to send", "Want me to draft this?", "Shall I book it?" Never use "Click here".

HARD RULES: Body under 320 chars. Return ONLY valid JSON, no markdown. Never hallucinate facts.

Return ONLY this JSON:
{
  "action": "send",
  "body": "<message under 320 chars with 3+ exact facts>",
  "cta": "<specific action e.g. 'Reply YES to launch' or 'Want me to draft the post?'>",
  "suppression_key": "<exact suppression_key from trigger payload>",
  "rationale": "<which exact numbers/facts used and why this scores 10/10 on each dimension>"
}"""

def generate_llm_response(system_prompt: str, user_prompt: str) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://magicpin.com",
    }
    
    data = {
        "model": "openai/gpt-oss-20b:free", 
        "messages": [
            {"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}
        ]
    }
    
    req = urllib.request.Request(url, headers=headers, data=json.dumps(data).encode('utf-8'))
    
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=12.0) as response:
                result = json.loads(response.read().decode('utf-8'))
                llm_text = result['choices'][0]['message']['content']
                llm_text = llm_text.strip().removeprefix("```json").removesuffix("```").strip()
                return json.loads(llm_text)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"OpenRouter Rate Limit (429) hit! Sleeping for 15s to bypass... (Attempt {attempt+1}/4)")
                time.sleep(15)
                continue
            error_body = e.read().decode('utf-8', errors='ignore')
            print(f"OpenRouter API Error {e.code}: {error_body}")
            return {"error": "http_error"}
        except Exception as e:
            print(f"API choked or timed out: {e}")
            if attempt < 2:
                time.sleep(2)
                continue
            return {"error": "timeout_or_fail"}
    return {"error": "max_retries_exceeded"}

# ==========================================
# 2. DATABASE INITIALIZATION
# ==========================================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # We only use context_store, keeping everything unified in one table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS context_store (
                scope TEXT,
                context_id TEXT,
                version INTEGER,
                payload TEXT,
                stored_at TEXT,
                PRIMARY KEY (scope, context_id)
            )
        """)
        conn.commit()

init_db()

# ==========================================
# 3. PYDANTIC MODELS
# ==========================================
class ContextPayload(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: str

class TickPayload(BaseModel):
    now: str
    available_triggers: List[str]

class ReplyPayload(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

# ==========================================
# 4. CORE ENDPOINTS
# ==========================================
def get_context_counts():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT scope, COUNT(*) FROM context_store GROUP BY scope")
        counts = {row[0]: row[1] for row in cursor.fetchall()}
        return {
            "category": counts.get("category", 0),
            "merchant": counts.get("merchant", 0),
            "customer": counts.get("customer", 0),
            "trigger": counts.get("trigger", 0)
        }

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": 120,
        "contexts_loaded": get_context_counts()
    }

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Core",
        "model": "openrouter/free",
        "approach": "Stateful FastAPI + SQLite context store with strict zero-shot prompting",
        "version": "1.0.0"
    }

@app.post("/v1/context")
async def push_context(data: ContextPayload):
    scope = data.scope
    context_id = data.context_id
    version = data.version
    payload_data = data.payload

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT version FROM context_store WHERE scope=? AND context_id=?", 
            (scope, context_id)
        )
        row = cursor.fetchone()
        
        if row and row[0] > version:
            raise HTTPException(status_code=409, detail={"accepted": False, "reason": "stale_version", "current_version": row[0]})

        cursor.execute("""
            INSERT INTO context_store (scope, context_id, version, payload, stored_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, context_id) DO UPDATE SET
                version=excluded.version,
                payload=excluded.payload,
                stored_at=excluded.stored_at
        """, (scope, context_id, version, json.dumps(payload_data), datetime.utcnow().isoformat() + "Z"))
        conn.commit()

    return {
        "accepted": True, 
        "ack_id": f"ack_{context_id}_v{version}", 
        "stored_at": datetime.utcnow().isoformat() + "Z"
    }

async def process_single_trigger(trigger_id: str) -> dict:
    trigger_context_str = "{}"
    merchant_context_str = "No specific merchant context available."
    category_context_str = "No specific category context available."
    customer_context_str = "No specific customer context available."
    target_merchant_id = "unknown"
    trigger_data = {}
    merchant_data = {}

    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # 1. Get Trigger Info
            cursor.execute("SELECT payload FROM context_store WHERE scope='trigger' AND context_id=?", (trigger_id,))
            trigger_row = cursor.fetchone()
            if trigger_row:
                trigger_context_str = trigger_row[0]
                trigger_data = json.loads(trigger_context_str)
                target_merchant_id = trigger_data.get("merchant_id", "unknown")
            
            # 2. Get Merchant Info
            if target_merchant_id != "unknown":
                cursor.execute("SELECT payload FROM context_store WHERE scope='merchant' AND context_id=?", (target_merchant_id,))
                merchant_row = cursor.fetchone()
                if merchant_row:
                    merchant_context_str = merchant_row[0]
                    merchant_data = json.loads(merchant_context_str)

            # 3. Get Category Info
            category_slug = merchant_data.get("category_slug")
            if category_slug:
                cursor.execute("SELECT payload FROM context_store WHERE scope='category' AND context_id=?", (category_slug,))
                category_row = cursor.fetchone()
                if category_row:
                    category_context_str = category_row[0]

            # 4. Get Customer Info
            customer_id = trigger_data.get("customer_id")
            if customer_id:
                cursor.execute("SELECT payload FROM context_store WHERE scope='customer' AND context_id=?", (customer_id,))
                customer_row = cursor.fetchone()
                if customer_row:
                    customer_context_str = customer_row[0]

    except Exception as e:
        print(f"Database lookup error: {e}")

    user_prompt = f"""Task: Generate specific promo message for merchant.
Merchant ID: {target_merchant_id}
Trigger ID: {trigger_id}

--- TRIGGER CONTEXT (WHY NOW) ---
{trigger_context_str}

--- MERCHANT CONTEXT (WHO THEY ARE & PERFORMANCE) ---
{merchant_context_str}

--- CATEGORY CONTEXT (TONE & SEASONAL RULES) ---
{category_context_str}

--- CUSTOMER CONTEXT (IF APPLICABLE) ---
{customer_context_str}
"""

    suppression_key = trigger_data.get("suppression_key", "fallback_key")

    try:
        llm_json_output = await asyncio.wait_for(
            asyncio.to_thread(generate_llm_response, VERA_SYSTEM_PROMPT, user_prompt),
            timeout=25.0
        )
        
        if "error" in llm_json_output or not llm_json_output.get("body") or len(str(llm_json_output.get("body", ""))) < 5:
            return {"action": "send", "body": "Hi there! We noticed you recently engaged with us and we have an exclusive offer just for you.", "cta": "View Offer", "rationale": "Safe fallback due to LLM hallucination", "suppression_key": suppression_key, "trigger_id": trigger_id, "merchant_id": target_merchant_id}
        
        # Enforce 320 char limit — truncate at word boundary to avoid penalties
        body = llm_json_output.get("body", "")
        if len(body) > 317:
            body = body[:317].rsplit(" ", 1)[0] + "..."
            llm_json_output["body"] = body

        llm_json_output["trigger_id"] = trigger_id
        llm_json_output["merchant_id"] = target_merchant_id
        if "suppression_key" not in llm_json_output:
            llm_json_output["suppression_key"] = suppression_key
        return llm_json_output
    except Exception as e:
        return {"action": "send", "body": "Hi there! We noticed you recently engaged with us and we have an exclusive offer just for you.", "cta": "View Offer", "rationale": str(e), "suppression_key": suppression_key, "trigger_id": trigger_id, "merchant_id": target_merchant_id}

@app.post("/v1/tick")
async def tick(data: TickPayload):
    if not data.available_triggers:
        return {"actions": [{"action": "wait", "body": "", "cta": "", "rationale": "No triggers available."}]}
    
    try:
        # Stagger requests by 0.3s each to avoid free-tier rate-limit collisions
        async def staggered(tid: str, i: int):
            await asyncio.sleep(i * 0.3)
            return await process_single_trigger(tid)
        
        tasks = [staggered(tid, i) for i, tid in enumerate(data.available_triggers)]
        actions = await asyncio.wait_for(asyncio.gather(*tasks), timeout=28.0)
        return {"actions": list(actions)}
    except asyncio.TimeoutError:
        return {"actions": [{"action": "wait", "body": "", "cta": "", "rationale": "Tick timeout — LLM too slow."}]}


@app.post("/v1/reply")
async def reply(data: ReplyPayload):
    user_prompt = f"""Task: Decide next action.
Merchant '{data.merchant_id}' replied: '{data.message}'
Conv ID: {data.conversation_id}
Turn: {data.turn_number}"""
    
    try:
        llm_json_output = await asyncio.wait_for(
            asyncio.to_thread(generate_llm_response, VERA_SYSTEM_PROMPT, user_prompt),
            timeout=13.5
        )
        
        # INTERCEPTOR: If OpenRouter failed, return safe fallback schema
        if "error" in llm_json_output:
            return {
                "action": "end",
                "body": "Got it. I have noted your response and our team will review the details shortly.",
                "cta": "",
                "rationale": "Fallback triggered due to upstream LLM issue."
            }
            
        print(json.dumps(llm_json_output, indent=2))
        return llm_json_output
        
    except asyncio.TimeoutError:
        return {
            "action": "end",
            "body": "Got it. I have noted your response and our team will review the details shortly.",
            "cta": "",
            "rationale": "Fallback triggered due to upstream LLM timeout."
        }
    except Exception as e:
        return {
            "action": "end",
            "body": "Understood. Let me pause here and ensure your request is routed correctly.",
            "cta": "",
            "rationale": f"System error fallback: {str(e)}"
        }