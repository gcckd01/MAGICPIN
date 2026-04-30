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

VERA_SYSTEM_PROMPT = """Role: Vera, AI growth assistant for local merchants.
Goal: Score 10/10 on Specificity, Category Fit, Merchant Fit, Trigger Relevance, and Engagement.
Constraints:
1. `body` < 320 chars.
2. NO URLs or links.
3. USE ONLY provided facts/metrics (no hallucination).
4. Return JSON ONLY, no markdown.

Guidelines:
- Specificity: Use EXACT numbers, %, and dates from context.
- Category Fit: Match voice (e.g., clinical/technical for dentists, warm for salons). Address owners by name.
- Trigger Relevance: Mention WHY you are messaging them NOW (based on trigger).
- Engagement: Strong CTA, low friction.

Format:
{"action": "send|wait|end", "body": "msg", "cta": "...", "rationale": "..."}"""

def generate_llm_response(system_prompt: str, user_prompt: str) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    body = {
        # Change this line:
        "model": "google/gemma-3-12b-it:free", 
        "messages": [
            {"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}
        ]
    }
    

    
    req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode('utf-8'))
    
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=6.0) as response:
                result = json.loads(response.read().decode('utf-8'))
                llm_text = result['choices'][0]['message']['content']
                llm_text = llm_text.strip().removeprefix("```json").removesuffix("```").strip()
                return json.loads(llm_text)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 1:
                time.sleep(1 + random.random() * 0.5)
                continue
            print(f"API HTTP Error {e.code}: {e.read().decode('utf-8')}")
            return {"error": "timeout_or_fail"}
        except Exception as e:
            if attempt < 1:
                time.sleep(1 + random.random() * 0.5)
                continue
            print(f"API choked or timed out: {e}")
            return {"error": "timeout_or_fail"}

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
class PayloadContent(BaseModel):
    type: str
    id: str
    version: int
    data: Dict[str, Any]

class ContextPayload(BaseModel):
    payload: PayloadContent
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
    scope = data.payload.type
    context_id = data.payload.id
    version = data.payload.version
    payload_data = data.payload.data

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT version FROM context_store WHERE scope=? AND context_id=?", 
            (scope, context_id)
        )
        row = cursor.fetchone()
        
        if row and row[0] >= version:
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

@app.post("/v1/tick")
async def tick(data: TickPayload):
    if not data.available_triggers:
        return {"actions": [{"action": "wait", "body": "", "cta": "", "rationale": "No triggers available."}]}
        
    trigger_id = data.available_triggers[0]
    trigger_context_str = "{}"
    merchant_context_str = "No specific context available."
    target_merchant_id = "unknown"

    # Fetch trigger and merchant data safely from the context_store
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            
            # Get Trigger Info
            cursor.execute("SELECT payload FROM context_store WHERE scope='trigger' AND context_id=?", (trigger_id,))
            trigger_row = cursor.fetchone()
            if trigger_row:
                trigger_context_str = trigger_row[0]
                trigger_data = json.loads(trigger_context_str)
                target_merchant_id = trigger_data.get("merchant_id", "unknown")
            
            # Get Merchant Info
            if target_merchant_id != "unknown":
                cursor.execute("SELECT payload FROM context_store WHERE scope='merchant' AND context_id=?", (target_merchant_id,))
                merchant_row = cursor.fetchone()
                if merchant_row:
                    merchant_context_str = merchant_row[0]
    except Exception as e:
        print(f"Database lookup error: {e}")

    user_prompt = f"""Task: Generate specific promo message for merchant.
Merchant ID: {target_merchant_id}
Trigger: {trigger_context_str}
Context: {merchant_context_str}"""

    try:
        llm_json_output = await asyncio.wait_for(
            asyncio.to_thread(generate_llm_response, VERA_SYSTEM_PROMPT, user_prompt),
            timeout=13.5
        )
        
        # INTERCEPTOR: If OpenRouter failed, return safe fallback schema
        if "error" in llm_json_output:
            return {"actions": [{"action": "wait", "body": "", "cta": "", "rationale": "Fallback triggered due to upstream LLM issue."}]}
            
        print(json.dumps(llm_json_output, indent=2))
        return {"actions": [llm_json_output]}

    except asyncio.TimeoutError:
        return {"actions": [{"action": "wait", "body": "", "cta": "", "rationale": "Timeout generating tick response."}]}
    except Exception as e:
        return {"actions": [{"action": "wait", "body": "", "cta": "", "rationale": f"System error: {str(e)}"}]}


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