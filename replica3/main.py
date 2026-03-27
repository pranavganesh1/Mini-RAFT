"""
Mini-RAFT Replica Service
─────────────────────────
Implements a simplified RAFT consensus protocol:
  • Leader Election (RequestVote RPC)
  • Heartbeats (AppendEntries with empty log)
  • Log Replication (AppendEntries with stroke data)
  • Catch-up (SyncLog for restarted nodes)

States: FOLLOWER → CANDIDATE → LEADER
Quorum: majority of 3 = 2 nodes must agree
"""

import os
import json
import random
import asyncio
import time
import logging
from enum import Enum
from typing import Optional

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# ─── Configuration ───────────────────────────────────────────────
REPLICA_ID = int(os.getenv("REPLICA_ID", "1"))
PEER_ADDRESSES = os.getenv(
    "PEER_ADDRESSES", "http://replica2:8000,http://replica3:8000"
).split(",")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")

ELECTION_TIMEOUT_MIN = int(os.getenv("ELECTION_TIMEOUT_MIN", "500"))   # ms
ELECTION_TIMEOUT_MAX = int(os.getenv("ELECTION_TIMEOUT_MAX", "800"))   # ms
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "150"))       # ms

QUORUM = 2  # majority of 3 nodes

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [REPLICA-{REPLICA_ID}] %(message)s"
)
logger = logging.getLogger(f"replica-{REPLICA_ID}")

# ─── RAFT State ──────────────────────────────────────────────────

class State(str, Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


# Persistent state (would be on disk in production)
current_term: int = 0
voted_for: Optional[int] = None
log: list[dict] = []          # Each entry: {term, index, data}

# Volatile state
state: State = State.FOLLOWER
commit_index: int = -1
leader_id: Optional[int] = None

# Leader-only volatile state
next_index: dict[str, int] = {}    # peer_addr -> next log index to send
match_index: dict[str, int] = {}   # peer_addr -> highest replicated index

# Timing
last_heartbeat: float = time.time()
election_timer_task: Optional[asyncio.Task] = None
heartbeat_task: Optional[asyncio.Task] = None


# ─── Pydantic Models ────────────────────────────────────────────

class RequestVoteRequest(BaseModel):
    term: int
    candidate_id: int
    last_log_index: int
    last_log_term: int

class RequestVoteResponse(BaseModel):
    term: int
    vote_granted: bool

class AppendEntriesRequest(BaseModel):
    term: int
    leader_id: int
    prev_log_index: int
    prev_log_term: int
    entries: list[dict] = []
    leader_commit: int

class AppendEntriesResponse(BaseModel):
    term: int
    success: bool
    match_index: int = -1


# ─── FastAPI App ─────────────────────────────────────────────────
app = FastAPI(title=f"Mini-RAFT Replica {REPLICA_ID}")


# ─── Helper Functions ────────────────────────────────────────────

def get_last_log_index() -> int:
    return len(log) - 1

def get_last_log_term() -> int:
    if log:
        return log[-1]["term"]
    return 0

def reset_election_timer():
    global last_heartbeat
    last_heartbeat = time.time()

def become_follower(term: int, new_leader: Optional[int] = None):
    global state, current_term, voted_for, leader_id, heartbeat_task
    state = State.FOLLOWER
    current_term = term
    voted_for = None
    leader_id = new_leader
    reset_election_timer()
    if heartbeat_task and not heartbeat_task.done():
        heartbeat_task.cancel()
        heartbeat_task = None
    logger.info(f"→ FOLLOWER (term={term}, leader={new_leader})")

def become_candidate():
    global state, current_term, voted_for
    state = State.CANDIDATE
    current_term += 1
    voted_for = REPLICA_ID
    reset_election_timer()
    logger.info(f"→ CANDIDATE (term={current_term})")

def become_leader():
    global state, leader_id, heartbeat_task, next_index, match_index
    state = State.LEADER
    leader_id = REPLICA_ID
    # Initialize leader volatile state
    for peer in PEER_ADDRESSES:
        next_index[peer] = len(log)
        match_index[peer] = -1
    logger.info(f"→ LEADER (term={current_term}) 🎉")
    # Notify gateway
    asyncio.create_task(notify_gateway_leader())
    # Start heartbeats
    heartbeat_task = asyncio.create_task(send_heartbeats())


async def notify_gateway_leader():
    """Tell the gateway that this replica is now the leader."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            await client.post(
                f"{GATEWAY_URL}/notify-leader-change",
                json={"leader_id": REPLICA_ID, "term": current_term}
            )
        except Exception as e:
            logger.warning(f"Could not notify gateway: {e}")


# ─── Election Logic ─────────────────────────────────────────────

async def start_election():
    """Become candidate and request votes from all peers."""
    become_candidate()

    votes_received = 1  # vote for self
    last_idx = get_last_log_index()
    last_term = get_last_log_term()

    async def request_vote(peer: str):
        nonlocal votes_received
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.post(f"{peer}/request-vote", json={
                    "term": current_term,
                    "candidate_id": REPLICA_ID,
                    "last_log_index": last_idx,
                    "last_log_term": last_term
                })
                data = resp.json()
                if data.get("term", 0) > current_term:
                    become_follower(data["term"])
                    return
                if data.get("vote_granted"):
                    votes_received += 1
        except Exception:
            pass

    # Send vote requests in parallel
    await asyncio.gather(*[request_vote(peer) for peer in PEER_ADDRESSES])

    # Check if we won (and are still a candidate)
    if state == State.CANDIDATE and votes_received >= QUORUM:
        become_leader()
    elif state == State.CANDIDATE:
        become_follower(current_term)


# ─── Heartbeat / Log Replication ─────────────────────────────────

async def send_heartbeats():
    """Leader sends periodic heartbeats (AppendEntries) to all peers."""
    while state == State.LEADER:
        await asyncio.gather(*[
            send_append_entries(peer) for peer in PEER_ADDRESSES
        ])
        await asyncio.sleep(HEARTBEAT_INTERVAL / 1000.0)


async def send_append_entries(peer: str):
    """Send AppendEntries RPC to a single peer."""
    global commit_index

    ni = next_index.get(peer, len(log))
    prev_idx = ni - 1
    prev_term = log[prev_idx]["term"] if 0 <= prev_idx < len(log) else 0
    entries = log[ni:] if ni < len(log) else []

    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.post(f"{peer}/append-entries", json={
                "term": current_term,
                "leader_id": REPLICA_ID,
                "prev_log_index": prev_idx,
                "prev_log_term": prev_term,
                "entries": entries,
                "leader_commit": commit_index
            })
            data = resp.json()

            if data.get("term", 0) > current_term:
                become_follower(data["term"])
                return

            if data.get("success"):
                new_match = data.get("match_index", prev_idx)
                next_index[peer] = new_match + 1
                match_index[peer] = new_match
                # Check if we can advance commit_index
                update_commit_index()
            else:
                # Decrement next_index and retry
                next_index[peer] = max(0, ni - 1)
    except Exception:
        pass


def update_commit_index():
    """Advance commit_index if a majority has replicated a log entry."""
    global commit_index
    for n in range(len(log) - 1, commit_index, -1):
        if log[n]["term"] == current_term:
            # Count replicas that have this entry
            count = 1  # leader has it
            for peer in PEER_ADDRESSES:
                if match_index.get(peer, -1) >= n:
                    count += 1
            if count >= QUORUM:
                old = commit_index
                commit_index = n
                # Notify gateway about newly committed entries
                for i in range(old + 1, n + 1):
                    asyncio.create_task(notify_commit(i))
                break


async def notify_commit(index: int):
    """Tell the gateway that a log entry has been committed."""
    entry = log[index]
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            await client.post(f"{GATEWAY_URL}/notify-commit", json={
                "index": index,
                "term": entry["term"],
                "data": entry["data"]
            })
        except Exception as e:
            logger.warning(f"Could not notify commit: {e}")


# ─── Election Timer ─────────────────────────────────────────────

async def election_timer_loop():
    """Background task: triggers election if heartbeat times out."""
    while True:
        timeout = random.randint(ELECTION_TIMEOUT_MIN, ELECTION_TIMEOUT_MAX) / 1000.0
        await asyncio.sleep(timeout)

        if state == State.LEADER:
            continue

        elapsed = time.time() - last_heartbeat
        if elapsed >= timeout:
            logger.info(f"Election timeout ({elapsed:.2f}s). Starting election.")
            await start_election()


# ─── RPC Endpoints ───────────────────────────────────────────────

@app.post("/request-vote")
async def handle_request_vote(req: RequestVoteRequest) -> RequestVoteResponse:
    global current_term, voted_for

    # If requester has higher term, step down
    if req.term > current_term:
        become_follower(req.term)

    # Decide whether to grant vote
    grant = False
    if req.term >= current_term:
        if voted_for is None or voted_for == req.candidate_id:
            # Check log freshness
            my_last_term = get_last_log_term()
            my_last_idx = get_last_log_index()
            log_ok = (req.last_log_term > my_last_term or
                      (req.last_log_term == my_last_term and
                       req.last_log_index >= my_last_idx))
            if log_ok:
                voted_for = req.candidate_id
                reset_election_timer()
                grant = True
                logger.info(f"Voted for replica {req.candidate_id} (term={req.term})")

    return RequestVoteResponse(term=current_term, vote_granted=grant)


@app.post("/append-entries")
async def handle_append_entries(req: AppendEntriesRequest) -> AppendEntriesResponse:
    global current_term, commit_index

    # Stale term
    if req.term < current_term:
        return AppendEntriesResponse(term=current_term, success=False)

    # Valid leader heartbeat
    become_follower(req.term, req.leader_id)

    # Log consistency check
    if req.prev_log_index >= 0:
        if req.prev_log_index >= len(log):
            return AppendEntriesResponse(
                term=current_term, success=False,
                match_index=len(log) - 1
            )
        if log[req.prev_log_index]["term"] != req.prev_log_term:
            # Delete conflicting entry and everything after
            del log[req.prev_log_index:]
            return AppendEntriesResponse(
                term=current_term, success=False,
                match_index=len(log) - 1
            )

    # Append new entries
    for i, entry in enumerate(req.entries):
        idx = req.prev_log_index + 1 + i
        if idx < len(log):
            if log[idx]["term"] != entry["term"]:
                del log[idx:]
                log.append(entry)
            # else: already have this entry
        else:
            log.append(entry)

    # Update commit index
    if req.leader_commit > commit_index:
        commit_index = min(req.leader_commit, len(log) - 1)

    return AppendEntriesResponse(
        term=current_term,
        success=True,
        match_index=len(log) - 1
    )


@app.post("/client-request")
async def handle_client_request(payload: dict):
    """
    Called by gateway when a client submits a new stroke.
    Only the leader should handle this.
    """
    if state != State.LEADER:
        return {"success": False, "error": "not_leader", "leader_id": leader_id}

    # Append to local log
    entry = {
        "term": current_term,
        "index": len(log),
        "data": payload
    }
    log.append(entry)
    logger.info(f"Appended stroke to log (index={entry['index']})")

    # Immediately replicate to peers
    await asyncio.gather(*[
        send_append_entries(peer) for peer in PEER_ADDRESSES
    ])

    return {"success": True, "index": entry["index"]}


@app.get("/sync-log")
async def sync_log():
    """
    Returns the full committed log.
    Used by: (1) gateway for new client catch-up,
             (2) restarted replicas for log recovery.
    """
    committed = log[:commit_index + 1] if commit_index >= 0 else []
    return {"log": committed, "commit_index": commit_index}


@app.get("/status")
async def status():
    """Health check + state info for leader discovery."""
    return {
        "replica_id": REPLICA_ID,
        "state": state.value,
        "term": current_term,
        "leader_id": leader_id,
        "log_length": len(log),
        "commit_index": commit_index,
        "voted_for": voted_for
    }


@app.get("/health")
async def health():
    return {"status": "ok", "replica_id": REPLICA_ID}


# ─── Startup ────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info(f"Starting replica {REPLICA_ID}...")
    logger.info(f"Peers: {PEER_ADDRESSES}")
    # Start election timer
    asyncio.create_task(election_timer_loop())
    # Try to catch up from existing leader
    asyncio.create_task(attempt_catchup())


async def attempt_catchup():
    """On startup, try to sync log from the current leader."""
    await asyncio.sleep(1)  # give peers time to start
    for peer in PEER_ADDRESSES:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{peer}/status")
                peer_status = resp.json()
                if peer_status.get("state") == "leader":
                    sync_resp = await client.get(f"{peer}/sync-log")
                    sync_data = sync_resp.json()
                    global log, commit_index
                    log = sync_data.get("log", [])
                    commit_index = sync_data.get("commit_index", -1)
                    logger.info(
                        f"Caught up from leader (peer). "
                        f"Log length={len(log)}, commit={commit_index}"
                    )
                    return
        except Exception:
            continue
    logger.info("No leader found for catch-up. Starting fresh.")
