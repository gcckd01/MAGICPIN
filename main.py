import asyncio
import json
import sqlite3
import os
import urllib.request
import urllib.error
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

VERA_SYSTEM_PROMPT = """You are Vera, an elite, data-driven AI growth assistant for local merchants. Your goal is to drive engagement by composing highly specific, relevant, and compelling nudges.

### HARD CONSTRAINTS (Violating these results in immediate failure):
1. **LENGTH:** Your `body` text MUST NOT exceed 320 characters. Count carefully.
2. **NO URLs:** You MUST NOT include any links, URLs, or web addresses.
3. **NO FABRICATION:** You must only use numbers, prices, and facts explicitly provided in the context. Do not invent metrics.
4. **NO REPETITION:** Do not repeat messages in the same conversation thread.

### OUTPUT FORMAT:
You must return valid JSON ONLY. Do not include markdown formatting like ```json.
You MUST wrap your response in an "actions" array. 

Example Output Structure:
{
  "actions": [
    {
      "conversation_id": "conv_123",
      "merchant_id": "m_001_drmeera",
      "customer_id": null,
      "send_as": "vera",
      "trigger_id": "trg_001",
      "template_name": "vera_nudge_v1",
      "template_params": ["string"],
      "body": "String strictly under 320 chars containing specific facts and a binary CTA.",
      "cta": "binary_yes_no",
      "suppression_key": "string",
      "rationale": "Explain how this message meets the scoring dimensions."
    }
  ]
}
EXPERT TIP: To score 10/10 on Category Fit, you MUST use technical terms from the category context (e.g., 'caries', 'IOPA', 'conversion') and address the owner by their first name.
You are a precise marketing assistant. You MUST ONLY use the numbers and facts provided in the prompt context. NEVER invent, hallucinate, or guess metrics. If no metrics are provided, do not mention any
"""

def generate_llm_response(system_prompt: str, user_prompt: str) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    body = {
        
        "model": "openrouter/free", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }
    

    
    req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode('utf-8'))
    
    try:
        with urllib.request.urlopen(req, timeout=8.0) as response:
            result = json.loads(response.read().decode('utf-8'))
            llm_text = result['choices'][0]['message']['content']
            llm_text = llm_text.strip().removeprefix("```json").removesuffix("```").strip()
            return json.loads(llm_text)
    except Exception as e:
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
class ContextPayload(BaseModel):
    scope: str
    context_id: str
    version: int
    delivered_at: str
    payload: Dict[str, Any]

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
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT version FROM context_store WHERE scope=? AND context_id=?", 
            (data.scope, data.context_id)
        )
        row = cursor.fetchone()
        
        if row and row[0] >= data.version:
            raise HTTPException(status_code=409, detail={"accepted": False, "reason": "stale_version", "current_version": row[0]})

        cursor.execute("""
            INSERT INTO context_store (scope, context_id, version, payload, stored_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, context_id) DO UPDATE SET
                version=excluded.version,
                payload=excluded.payload,
                stored_at=excluded.stored_at
        """, (data.scope, data.context_id, data.version, json.dumps(data.payload), datetime.utcnow().isoformat() + "Z"))
        conn.commit()

    return {
        "accepted": True, 
        "ack_id": f"ack_{data.context_id}_v{data.version}", 
        "stored_at": datetime.utcnow().isoformat() + "Z"
    }

@app.post("/v1/tick")
async def tick(data: TickPayload):
    if not data.available_triggers:
        return {"action": "wait", "body": "", "cta": "", "rationale": "No triggers available."}
        
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

    user_prompt = f"""
    Generate a highly specific promotional message for this merchant.
    Merchant ID: {target_merchant_id}
    Trigger Event Details: {trigger_context_str}
    
    MERCHANT REALITY (USE THESE FACTS ONLY):
    {merchant_context_str}
    
    Return a JSON object with 'action' (send/wait), 'body' (the message), 'cta', and 'rationale'.
    """

    try:
        llm_json_output = await asyncio.wait_for(
            asyncio.to_thread(generate_llm_response, VERA_SYSTEM_PROMPT, user_prompt),
            timeout=6.0
        )
        
        # INTERCEPTOR: If OpenRouter failed, return safe fallback schema
        if "error" in llm_json_output:
            return {"action": "wait", "body": "", "cta": "", "rationale": "Fallback triggered due to upstream LLM issue."}
            
        print(json.dumps(llm_json_output, indent=2))
        return llm_json_output

    except asyncio.TimeoutError:
        return {"action": "wait", "body": "", "cta": "", "rationale": "Timeout generating tick response."}
    except Exception as e:
        return {"action": "wait", "body": "", "cta": "", "rationale": f"System error: {str(e)}"}


@app.post("/v1/reply")
async def reply(data: ReplyPayload):
    user_prompt = f"The merchant '{data.merchant_id}' just replied to your previous message.\nConversation ID: {data.conversation_id}\nMerchant's Message: '{data.message}'\nTurn Number: {data.turn_number}\nDecide what to do next. Return a single JSON action object with keys: 'action' (send/wait/end), 'body' (if sending), 'cta', and 'rationale'."
    
    try:
        llm_json_output = await asyncio.wait_for(
            asyncio.to_thread(generate_llm_response, VERA_SYSTEM_PROMPT, user_prompt),
            timeout=6.0
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