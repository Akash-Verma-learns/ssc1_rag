"""
FastAPI App
-----------
Main entry point. Wires together:
  - Database init (creates tables + seeds admin user)
  - All routes (auth, rfps, comments, users)
  - CORS (so Lovable frontend can call this from browser)

Start: uvicorn api:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import init_db
from routes import router

app = FastAPI(
    title="SSC1 PQ Risk Review API",
    description="Backend for Grant Thornton SSC1 Risk Review Portal",
    version="1.0.0",
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# Allows the Lovable frontend (any origin) to call this API from the browser.
# In production, replace "*" with your actual frontend URL.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ─────────────────────────────────────────────────────────────────────
app.include_router(router)

# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    print("[API] Initialising database...")
    init_db()
    print("[API] Ready.")

@app.get("/health")
def health():
    return {"status": "ok"}
