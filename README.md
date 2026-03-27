# 🎨 Mini-RAFT: Distributed Real-Time Drawing Board

**Mini-RAFT** is a fault-tolerant, real-time collaborative drawing platform built with **Python (FastAPI)** and **React (TypeScript)**. It implements a simplified version of the **RAFT Consensus Algorithm** to ensure that your drawing data (strokes) is replicated across multiple servers with strong consistency.

Even if the "Active Leader" server crashes, the remaining servers will automatically elect a new leader and continue syncing the board with **zero downtime**!

---

## 🏗️ Architecture
- **3 Replicas (Consensus Engine)**: Python servers that manage the stroke log via RAFT (Follower/Candidate/Leader states).
- **1 Gateway (Client Proxy)**: A stateless proxy that routes browser WebSocket connections to the current RAFT leader.
- **1 Frontend (Real-time UI)**: A React/Vite/TypeScript app that renders an HTML5 canvas and cluster status dashboard.

---

## 🚀 Getting Started (Local Machine)

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (ensure it's running)
- [Docker Compose](https://docs.docker.com/compose/install/)

### Installation
1.  Clone or download this project folder.
2.  Open a terminal in the project root.
3.  **Spin up the entire cluster**:
    ```bash
    docker-compose up --build
    ```
4.  Once the logs show `Vite ... server running at http://localhost:5173/`, open your browser to:
    **[http://localhost:5173/](http://localhost:5173/)**

---

## 🌐 Connecting from Other Systems (Friends)

Your friends can join the same drawing board if they are on the **same Wi-Fi or local network**!

1.  **Find your Local IP**:
    - **Windows**: Run `ipconfig` (look for "IPv4 Address", e.g., `192.168.1.15`).
    - **Mac/Linux**: Run `ifconfig` or `ip addr`.
2.  **Ensure your Firewall allows traffic**:
    Make sure your system isn't blocking incoming connections on ports **5173** (UI) and **8000** (Gateway).
3.  **Have your Friend Connect**:
    Your friend just needs to enter your IP in their browser:
    ```text
    http://<YOUR_IP_HERE>:5173
    ```
4.  **How it syncs**: 
    The browser code is smart! It automatically talks back to your machine at `http://<YOUR_IP_HERE>:8000` for the WebSockets, so their strokes will appear on your canvas in real-time.

---

## 🧪 Testing Fault Tolerance (Chaos Mode)

The coolest part of this project is seeing the consensus algorithm in action. Try these:

### 1. The Follower Test
Stop one of the replicas that is NOT the leader:
```bash
docker stop miniraft-replica2
```
*Result:* You and your friends can still draw perfectly. Start it back up (`docker start miniraft-replica2`), and it will magically catch up on all missing strokes from the Leader!

### 2. The Leader Crash Test
Check your UI to see who the "Active Leader" is (e.g., Replica 1). Now kill that leader:
```bash
docker stop miniraft-replica1
```
*Result:* Watch the UI's Status Dashboard! You'll see "Electing..." for a split second, then a new Replica (e.g., Replica 3) will take over as Leader. Your drawing session continues without any data loss.

---

## 🛠️ Tech Stack
- **Backend Logic**: Python 3.12, FastAPI (Asynchronous IO).
- **Internal RPCs**: HTTP (Client-to-Leader, Leader-to-Follower).
- **Client Comm**: WebSockets (Real-time broadcasting).
- **Frontend**: React 18, TypeScript, Vite, Vanilla CSS (Glassmorphism theme).
- **Infrastructure**: Docker & Docker Compose.
