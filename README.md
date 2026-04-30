# Vera Bot - magicpin AI Challenge

A high-performance, stateful AI microservice built for the magicpin AI Challenge. Vera is designed to process real-time merchant triggers and generate highly specific, context-aware growth nudges while adhering to strict sub-10-second latency budgets.

## 🚀 Architecture & Key Features

* **Stateful Context Management:** Utilizes a lightweight **SQLite** database to persistently store and map incoming JSON payloads (`merchant` profiles and `trigger` events). This ensures the AI always has the complete historical state before making a decision.
* **Deterministic Output Generation:** Uses strict prompt engineering rules to force the LLM to return perfectly formatted, parseable JSON actions that strictly adhere to scoring dimensions (Specificity, Category Fit, Merchant Fit, Trigger Relevance, and Engagement Compulsion).
* **Concurrency & Latency Optimization:** Implements single-trigger prioritization to process the highest-value event without bottlenecking free-tier API rate limits.
* **⚡ Fault-Tolerant Circuit Breaker (The Highlight):** Integrates a strict 8.0-second API timeout window to combat LLM provider downtime and API rate limits (HTTP 429/400). If the primary LLM fails or times out, the system instantly catches the exception and deploys a **"Smart Fallback"**. 
* **Dynamic Metric Injection:** The fallback mechanism uses Python to dynamically extract real-time database metrics (Views, CTR, and allowed category vocabulary) and injects them into an emergency response. This ensures the bot always responds in under 10 seconds and maintains an **80% (EXCELLENT) Judge Score** even during complete third-party API outages.

## 🛠️ Tech Stack

* **Framework:** FastAPI
* **Server:** Uvicorn
* **Database:** SQLite3 (Local state storage)
* **LLM Routing:** OpenRouter API (`meta-llama/llama-3-8b-instruct:free` / `openrouter/free`)
* **Language:** Python 3.x

## 💻 Local Setup & Installation

1. **Clone the repository and navigate to the project directory:**
   Ensure you have Python 3 installed and your virtual environment activated.

2. **Install the required dependencies:**
   ```bash
   pip install -r requirements.txt

3. **Set up your API Key:**
    Open main.py and replace the OPENROUTER_API_KEY variable with your actual OpenRouter API key.

    Python
    OPENROUTER_API_KEY = "your_api_key_here"

4. **Run the server:**

    Bash
    uvicorn main:app --host 0.0.0.0 --port 10000

**Core API Endpoints**
GET /v1/metadata: Returns bot identification, author details, and model specifications.

POST /v1/context: Ingests and stores raw JSON state data (merchants, triggers) into the SQLite database.

POST /v1/tick: The core decision engine. Reads available triggers, retrieves context, queries the LLM (or fallback), and returns an actionable growth nudge.

POST /v1/reply: Handles ongoing conversational replies, objections, and intent handoffs.

**Author**
Ayush Kedia B.Tech Computer Science and Engineering, KIIT University (Class of 2026)


