import { useEffect, useRef, useState } from 'react';
import './App.css';

// Stroke Data Structure
interface Stroke {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  color: string;
  width: number;
}

interface SocketMessage {
  type: string;
  data?: any;
  log?: any[];
  leader_id?: number | null;
  message?: string;
}

const COLORS = ['#ef4444', '#f59e0b', '#10b981', '#3b82f6', '#8b5cf6', '#f1f5f9'];

function App() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [leaderId, setLeaderId] = useState<number | null>(null);
  const [logSize, setLogSize] = useState(0);
  
  // Drawing state
  const [isDrawing, setIsDrawing] = useState(false);
  const [color, setColor] = useState(COLORS[5]);
  const [lastPos, setLastPos] = useState({ x: 0, y: 0 });
  const ws = useRef<WebSocket | null>(null);

  const clearCanvas = () => {
    const ctx = canvasRef.current?.getContext('2d');
    if (ctx && canvasRef.current) {
      ctx.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height);
    }
  };

  // Initialize WebSocket
  useEffect(() => {
    const connect = () => {
      // Connect to the gateway service based on the document location
      const wsUrl = `ws://${window.location.hostname}:8000/ws`;
      ws.current = new WebSocket(wsUrl);

      ws.current.onopen = () => {
        setIsConnected(true);
      };

      ws.current.onclose = () => {
        setIsConnected(false);
        setLeaderId(null);
        setTimeout(connect, 2000); // Reconnect attempt
      };

      ws.current.onmessage = (event) => {
        const msg: SocketMessage = JSON.parse(event.data);
        
        if (msg.type === 'leader_change') {
          setLeaderId(msg.leader_id || null);
        } else if (msg.type === 'full_sync') {
          // Replay whole log
          const log = msg.log || [];
          setLogSize(log.length);
          const ctx = canvasRef.current?.getContext('2d');
          if (ctx && canvasRef.current) {
            ctx.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height);
            log.forEach((entry: any) => {
              if (entry.data?.action === 'clear') {
                ctx.clearRect(0, 0, canvasRef.current!.width, canvasRef.current!.height);
              } else {
                drawStroke(entry.data);
              }
            });
          }
        } else if (msg.type === 'stroke_committed') {
          // New stroke verified by RAFT
          setLogSize(prev => prev + 1);
          if (msg.data.data?.action === 'clear') {
            clearCanvas();
          } else {
            drawStroke(msg.data.data);
          }
        } else if (msg.type === 'error') {
          console.warn("Gateway error:", msg.message);
        }
      };
    };

    connect();

    return () => {
      ws.current?.close();
    };
  }, []);

  // Handle Resize
  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas) {
      canvas.width = canvas.parentElement?.clientWidth || window.innerWidth;
      canvas.height = canvas.parentElement?.clientHeight || window.innerHeight;
    }
    const handleResize = () => {
      if (canvas) {
        canvas.width = canvas.parentElement?.clientWidth || window.innerWidth;
        canvas.height = canvas.parentElement?.clientHeight || window.innerHeight;
      }
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const drawStroke = (stroke: Stroke) => {
    const ctx = canvasRef.current?.getContext('2d');
    if (!ctx) return;
    ctx.beginPath();
    ctx.moveTo(stroke.x0, stroke.y0);
    ctx.lineTo(stroke.x1, stroke.y1);
    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.lineCap = 'round';
    ctx.stroke();
    ctx.closePath();
  };

  const startDrawing = (e: React.MouseEvent | React.TouchEvent) => {
    setIsDrawing(true);
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX;
    const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY;
    setLastPos({
      x: clientX - rect.left,
      y: clientY - rect.top
    });
  };

  const draw = (e: React.MouseEvent | React.TouchEvent) => {
    if (!isDrawing) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    
    const rect = canvas.getBoundingClientRect();
    const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX;
    const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY;
    
    const currentPos = {
      x: clientX - rect.left,
      y: clientY - rect.top
    };

    const stroke: Stroke = {
      x0: lastPos.x,
      y0: lastPos.y,
      x1: currentPos.x,
      y1: currentPos.y,
      color: color,
      width: 4
    };

    // Draw locally instantly for premium feel
    drawStroke(stroke);
    
    // Send to RAFT gateway
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify({
        type: 'stroke',
        data: stroke
      }));
    }

    setLastPos(currentPos);
  };

  const stopDrawing = () => {
    setIsDrawing(false);
  };

  const handleClear = () => {
    clearCanvas();
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify({
        type: 'stroke',
        data: { action: 'clear' }
      }));
    }
  };

  return (
    <div className="app-container">
      {/* Cluster Status Sidebar */}
      <div className="sidebar">
        <h1>Mini-RAFT</h1>
        <p className="subtitle">Distributed Drawing Board</p>

        <div className="status-section">
          <h2>Gateway Connection</h2>
          <div className="status-row">
            <span className="status-label">Status</span>
            <span className="status-value">
              <span className={`indicator ${isConnected ? 'online' : 'offline'}`}></span>
              {isConnected ? 'Connected' : 'Disconnected'}
            </span>
          </div>
        </div>

        <div className="status-section">
          <h2>RAFT Cluster State</h2>
          <div className="status-row">
            <span className="status-label">Active Leader</span>
            <span className="status-value">{leaderId ? `Replica ${leaderId}` : 'Electing...'}</span>
          </div>
          <div className="status-row">
            <span className="status-label">Committed Logs</span>
            <span className="status-value">{logSize}</span>
          </div>
        </div>

      </div>

      {/* Canvas Area */}
      <div className="canvas-wrapper">
        <div className="tools-overlay">
          <div className="color-picker">
            {COLORS.map((c) => (
              <button
                key={c}
                className={`color-btn ${color === c ? 'active' : ''}`}
                style={{ backgroundColor: c }}
                onClick={() => setColor(c)}
                aria-label={`Select color ${c}`}
              />
            ))}
          </div>
          <button className="clear-btn" onClick={handleClear}>
            Clear
          </button>
        </div>
        
        <canvas
          ref={canvasRef}
          onMouseDown={startDrawing}
          onMouseMove={draw}
          onMouseUp={stopDrawing}
          onMouseOut={stopDrawing}
          onTouchStart={startDrawing}
          onTouchMove={draw}
          onTouchEnd={stopDrawing}
        />

        {!isConnected && (
          <div className="connection-banner">
            Connection lost. Reconnecting to Gateway...
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
