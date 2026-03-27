"""
Mini-RAFT Gateway Service
─────────────────────────
Acts as a WebSocket proxy between browser clients and the RAFT cluster.
- Accepts drawing strokes from clients via WebSocket.
- Forwards strokes to the current RAFT Leader.
- Broadcasts committed strokes back to all connected clients.
"""

import os
import json
import asyncio
import logging
from typing import Set

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ─── Configuration ───────────────────────────────────────────────
REPLICA_ADDRESSES = os.getenv(
    "REPLICA_ADDRESSES",
    "http://replica1:8000,http://replica2:8000,http://replica3:8000"
).split(",")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [GATEWAY] %(message)s")
logger = logging.getLogger("gateway")

# ─── FastAPI App ─────────────────────────────────────────────────
app = FastAPI(title="Mini-RAFT Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── State ───────────────────────────────────────────────────────
connected_clients: Set[WebSocket] = set()
current_leader_url: str | None = None


# ─── Helper: Discover the leader ────────────────────────────────
async def discover_leader() -> str | None:
    """Poll all replicas to find who is the current leader."""
    global current_leader_url
    async with httpx.AsyncClient(timeout=2.0) as client:
        for addr in REPLICA_ADDRESSES:
            try:
                resp = await client.get(f"{addr}/status")
                data = resp.json()
                if data.get("state") == "leader":
                    current_leader_url = addr
                    logger.info(f"Discovered leader: {addr}")
                    return addr
                # If a follower knows the leader, use that info
                if data.get("leader_id"):
                    leader_addr = next(
                        (a for a in REPLICA_ADDRESSES
                         if f"replica{data['leader_id']}" in a),
                        None
                    )
                    if leader_addr:
                        current_leader_url = leader_addr
                        logger.info(f"Leader from follower hint: {leader_addr}")
                        return leader_addr
            except Exception:
                continue
    logger.warning("No leader found in cluster!")
    current_leader_url = None
    return None


# ─── Helper: Broadcast to all clients ───────────────────────────
async def broadcast(message: dict):
    """Send a message to every connected WebSocket client."""
    dead = set()
    payload = json.dumps(message)
    for ws in connected_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


# ─── Helper: Forward stroke to leader ───────────────────────────
async def forward_to_leader(stroke: dict) -> bool:
    """Send a stroke to the RAFT leader. Returns True on success."""
    global current_leader_url

    if not current_leader_url:
        await discover_leader()

    if not current_leader_url:
        return False

    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            resp = await client.post(
                f"{current_leader_url}/client-request",
                json=stroke
            )
            if resp.status_code == 200:
                return True
            # Leader may have changed
            current_leader_url = None
            return False
        except Exception as e:
            logger.error(f"Failed to reach leader: {e}")
            current_leader_url = None
            return False


# ─── REST Endpoints ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "gateway", "clients": len(connected_clients)}


@app.post("/notify-commit")
async def notify_commit(payload: dict):
    """
    Called by the RAFT leader when a stroke is committed.
    Broadcasts the committed stroke to all connected browser clients.
    """
    logger.info(f"Broadcasting committed stroke: index={payload.get('index')}")
    await broadcast({
        "type": "stroke_committed",
        "data": payload
    })
    return {"status": "broadcast_sent"}


@app.post("/notify-leader-change")
async def notify_leader_change(payload: dict):
    """Called by a replica when it becomes leader."""
    global current_leader_url
    new_leader_id = payload.get("leader_id")
    addr = next(
        (a for a in REPLICA_ADDRESSES if f"replica{new_leader_id}" in a),
        None
    )
    if addr:
        current_leader_url = addr
        logger.info(f"Leader changed to replica{new_leader_id} ({addr})")
    await broadcast({
        "type": "leader_change",
        "leader_id": new_leader_id
    })
    return {"status": "ok"}


# ─── WebSocket Endpoint ─────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    logger.info(f"Client connected. Total: {len(connected_clients)}")

    # Send current leader info and full log to new client
    try:
        leader = await discover_leader()
        if leader:
            # Fetch full log for catch-up
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{leader}/sync-log")
                if resp.status_code == 200:
                    log_data = resp.json()
                    await ws.send_text(json.dumps({
                        "type": "full_sync",
                        "log": log_data.get("log", [])
                    }))
    except Exception as e:
        logger.warning(f"Could not sync new client: {e}")

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)

            if data.get("type") == "stroke":
                stroke = data.get("data", {})
                success = await forward_to_leader(stroke)
                if not success:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": "No leader available. Retrying..."
                    }))
                    # Retry after leader discovery
                    await discover_leader()
                    success = await forward_to_leader(stroke)
                    if not success:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "Cluster unavailable. Please try again."
                        }))

    except WebSocketDisconnect:
        connected_clients.discard(ws)
        logger.info(f"Client disconnected. Total: {len(connected_clients)}")
    except Exception as e:
        connected_clients.discard(ws)
        logger.error(f"WebSocket error: {e}")


# ─── Background: Periodic leader check ──────────────────────────
@app.on_event("startup")
async def startup():
    asyncio.create_task(periodic_leader_check())


async def periodic_leader_check():
    """Every 2 seconds, verify the leader is still alive."""
    while True:
        await asyncio.sleep(2)
        await discover_leader()
