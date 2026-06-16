"""
Nexus: Unbound — Core Server (Fase 2)
Pilar 2: Motor Semântico de Objetos
Pilar 3 (parcial): Persistência dinâmica em chunks
FastAPI + WebSocket de baixa latência.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel, field_validator, ValidationError
from typing import Optional
from collections import defaultdict
from contextlib import asynccontextmanager
import aiosqlite
import asyncio
import json
import time

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — abre o banco e cria as tabelas
    await world_store.init()
    yield
    # Shutdown — fecha o banco com segurança
    await world_store.close()


app = FastAPI(title="Nexus: Unbound — Core Server v2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
WS_TIMEOUT = 600
RATE_LIMIT_MAX = 300
RATE_LIMIT_WINDOW = 10
FRONTEND_PATH = "frontend/index.html"
CHUNK_SIZE = 16.0   # unidades por chunk (malha 2D no plano XZ)
DB_PATH = "nexus_world.db"

rate_store: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(pid: str) -> bool:
    now = time.time()
    rate_store[pid] = [t for t in rate_store[pid] if now - t < RATE_LIMIT_WINDOW]
    if len(rate_store[pid]) >= RATE_LIMIT_MAX:
        return True
    rate_store[pid].append(now)
    return False


# ════════════════════════════════════════════════════════════
# SCHEMAS PYDANTIC
# ════════════════════════════════════════════════════════════
class ActionPayload(BaseModel):
    player_id: str
    action_type: str
    target_tags: list[str] = []
    target_id: Optional[str] = None
    position: Optional[list[float]] = None
    world_style: Optional[int] = None  # para SET_STYLE

    @field_validator("action_type")
    @classmethod
    def valid_action(cls, v: str) -> str:
        allowed = {"INTERACT", "COLLIDE", "SHOOT", "MOVE", "PING", "SET_STYLE"}
        if v not in allowed:
            raise ValueError(f"action_type inválido: {v}")
        return v

    @field_validator("target_tags")
    @classmethod
    def valid_tags(cls, v: list[str]) -> list[str]:
        if len(v) > 16:
            raise ValueError("Excesso de tags (máx 16)")
        return [t.strip().lower() for t in v if isinstance(t, str)]

    @field_validator("world_style")
    @classmethod
    def valid_style(cls, v):
        if v is not None and v not in (0, 1, 2):
            raise ValueError("world_style deve ser 0, 1 ou 2")
        return v


# ════════════════════════════════════════════════════════════
# CHUNK SYSTEM — persistência em malha 2D
# ════════════════════════════════════════════════════════════
def chunk_coords(x: float, z: float) -> tuple[int, int]:
    return (int(x // CHUNK_SIZE), int(z // CHUNK_SIZE))


class WorldStore:
    """
    Persistência permanente em SQLite (assíncrona via aiosqlite).
    Guarda modificações de objetos semânticos atreladas ao chunk (cx, cz).
    Sobrevive a reinícios do servidor — o mundo lembra para sempre.
    """
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self):
        """Cria a conexão e a tabela. Chamado no startup."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chunk_modifications (
                cx       INTEGER NOT NULL,
                cz       INTEGER NOT NULL,
                obj_id   TEXT    NOT NULL,
                state    TEXT    NOT NULL,
                updated  REAL    NOT NULL,
                PRIMARY KEY (cx, cz, obj_id)
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunk ON chunk_modifications (cx, cz)"
        )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def record_modification(self, cx: int, cz: int, obj_id: str, new_state: dict):
        async with self._lock:
            await self._db.execute(
                """INSERT INTO chunk_modifications (cx, cz, obj_id, state, updated)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(cx, cz, obj_id)
                   DO UPDATE SET state=excluded.state, updated=excluded.updated""",
                (cx, cz, obj_id, json.dumps(new_state), time.time()),
            )
            await self._db.commit()

    async def get_chunk_state(self, cx: int, cz: int) -> dict:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT obj_id, state FROM chunk_modifications WHERE cx=? AND cz=?",
                (cx, cz),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        modified = {}
        for row in rows:
            try:
                modified[row["obj_id"]] = json.loads(row["state"])
            except json.JSONDecodeError:
                modified[row["obj_id"]] = {}
        return {"modified": modified}

    async def chunk_has_changes(self, cx: int, cz: int) -> bool:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT 1 FROM chunk_modifications WHERE cx=? AND cz=? LIMIT 1",
                (cx, cz),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None

    async def count_chunks(self) -> int:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT COUNT(DISTINCT cx || ',' || cz) AS n FROM chunk_modifications"
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row["n"] if row else 0


world_store = WorldStore()


# ════════════════════════════════════════════════════════════
# ESTADO DO JOGADOR
# ════════════════════════════════════════════════════════════
class PlayerState:
    def __init__(self, player_id: str):
        self.player_id = player_id
        self.position = [0.0, 4.0, 0.0]
        self.inventory: dict[str, int] = defaultdict(int)
        self.health = 100
        self.score = 0
        self.world_style = 0
        self.current_chunk: tuple[int, int] = (0, 0)
        self.destroyed_objects: set[str] = set()

    def snapshot(self) -> dict:
        return {
            "position": self.position,
            "inventory": dict(self.inventory),
            "health": self.health,
            "score": self.score,
            "world_style": self.world_style,
        }


# ════════════════════════════════════════════════════════════
# MOTOR SEMÂNTICO (Pilar 2)
# ════════════════════════════════════════════════════════════
class SemanticEngine:
    @staticmethod
    def resolve(action: ActionPayload, state: PlayerState) -> tuple[dict, Optional[dict]]:
        """Retorna (resposta_ao_cliente, modificação_de_chunk | None)."""
        tags = set(action.target_tags)
        atype = action.action_type
        oid = action.target_id or "anon"

        if oid in state.destroyed_objects:
            return {"status": "ALREADY_RESOLVED", "target_id": oid}, None

        result: dict = {
            "status": "NO_EFFECT",
            "target_id": oid,
            "visual_trigger": None,
            "player_state": None,
        }
        chunk_mod = None

        if atype == "SHOOT" and "destruivel" in tags:
            state.destroyed_objects.add(oid)
            state.score += 10
            result.update({
                "status": "DESTROYED",
                "visual_trigger": "EXPLODE_PARTICLES",
                "player_state": state.snapshot(),
            })
            if "hostil" in tags:
                state.score += 25
                result["bonus"] = "HOSTILE_ELIMINATED"
            chunk_mod = {"obj_id": oid, "state": {"status": "destroyed"}}
            return result, chunk_mod

        if atype == "SHOOT" and "hostil" in tags:
            result.update({"status": "DAMAGED", "visual_trigger": "HIT_SPARK"})
            return result, None

        if atype in ("INTERACT", "COLLIDE") and "coletavel" in tags:
            item = next((t for t in tags if t not in
                         {"coletavel", "destruivel", "hostil", "solido"}), "recurso")
            state.inventory[item] += 1
            state.destroyed_objects.add(oid)
            result.update({
                "status": "COLLECTED",
                "visual_trigger": "PICKUP_SHINE",
                "collected_item": item,
                "player_state": state.snapshot(),
            })
            chunk_mod = {"obj_id": oid, "state": {"status": "collected"}}
            return result, chunk_mod

        if atype == "COLLIDE" and "hostil" in tags:
            state.health = max(0, state.health - 15)
            result.update({
                "status": "PLAYER_HIT",
                "visual_trigger": "DAMAGE_FLASH",
                "player_state": state.snapshot(),
            })
            return result, None

        if atype == "COLLIDE" and "solido" in tags:
            result.update({"status": "BLOCKED"})
            return result, None

        if atype == "INTERACT":
            result.update({"status": "INTERACTED", "visual_trigger": "GLOW_PULSE"})
            return result, None

        return result, None


# ════════════════════════════════════════════════════════════
# GERENCIADOR DE CONEXÕES
# ════════════════════════════════════════════════════════════
class ConnectionManager:
    def __init__(self):
        self._conns: dict[str, WebSocket] = {}
        self._states: dict[str, PlayerState] = {}
        self._lock = asyncio.Lock()

    async def connect(self, pid: str, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._conns[pid] = ws
            if pid not in self._states:
                self._states[pid] = PlayerState(pid)

    async def disconnect(self, pid: str):
        async with self._lock:
            self._conns.pop(pid, None)

    def get_state(self, pid: str) -> PlayerState:
        if pid not in self._states:
            self._states[pid] = PlayerState(pid)
        return self._states[pid]

    async def send(self, pid: str, msg: dict):
        ws = self._conns.get(pid)
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                await self.disconnect(pid)


manager = ConnectionManager()
engine = SemanticEngine()


# ════════════════════════════════════════════════════════════
# RECONCILIAÇÃO DE CHUNK — envia modificações salvas
# ════════════════════════════════════════════════════════════
async def reconcile_chunk(pid: str, state: PlayerState, x: float, z: float):
    cx, cz = chunk_coords(x, z)
    if (cx, cz) == state.current_chunk:
        return
    state.current_chunk = (cx, cz)

    if await world_store.chunk_has_changes(cx, cz):
        chunk_data = await world_store.get_chunk_state(cx, cz)
        await manager.send(pid, {
            "status": "CHUNK_RECONCILE",
            "chunk": [cx, cz],
            "modified": chunk_data["modified"],
        })


# ════════════════════════════════════════════════════════════
# ROTAS HTTP
# ════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "connections": len(manager._conns),
        "sessions": len(manager._states),
        "chunks_stored": await world_store.count_chunks(),
        "persistence": "sqlite",
    }


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    f = Path(FRONTEND_PATH)
    if not f.exists():
        return HTMLResponse(
            f"<h1>{FRONTEND_PATH} não encontrado.</h1>", status_code=404
        )
    return HTMLResponse(f.read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════
# WEBSOCKET
# ════════════════════════════════════════════════════════════
@app.websocket("/ws/{player_id}")
async def game_socket(websocket: WebSocket, player_id: str):
    await manager.connect(player_id, websocket)
    state = manager.get_state(player_id)

    await manager.send(player_id, {
        "status": "CONNECTED",
        "player_id": player_id,
        "player_state": state.snapshot(),
        "chunk_size": CHUNK_SIZE,
    })

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_TIMEOUT)
            except asyncio.TimeoutError:
                await manager.send(player_id, {"status": "TIMEOUT"})
                break

            if is_rate_limited(player_id):
                await manager.send(player_id, {"status": "RATE_LIMITED"})
                continue

            try:
                data = json.loads(raw)
                payload = ActionPayload(**data)
            except (json.JSONDecodeError, ValidationError, TypeError) as e:
                await manager.send(player_id, {
                    "status": "ERROR",
                    "message": f"Payload inválido: {str(e)[:120]}",
                })
                continue

            # PING
            if payload.action_type == "PING":
                await manager.send(player_id, {"status": "PONG"})
                continue

            # SET_STYLE — troca de estética
            if payload.action_type == "SET_STYLE":
                if payload.world_style is not None:
                    state.world_style = payload.world_style
                    await manager.send(player_id, {
                        "status": "STYLE_CHANGED",
                        "world_style": state.world_style,
                    })
                continue

            # MOVE — atualiza posição + reconciliação de chunk
            if payload.action_type == "MOVE":
                if payload.position and len(payload.position) == 3:
                    state.position = payload.position
                    try:
                        await reconcile_chunk(
                            player_id, state,
                            payload.position[0], payload.position[2]
                        )
                    except Exception:
                        pass
                continue

            # Resolução semântica + persistência em chunk
            try:
                result, chunk_mod = engine.resolve(payload, state)
                await manager.send(player_id, result)

                if chunk_mod:
                    px = state.position[0]
                    pz = state.position[2]
                    cx, cz = chunk_coords(px, pz)
                    await world_store.record_modification(
                        cx, cz, chunk_mod["obj_id"], chunk_mod["state"]
                    )
            except Exception as e:
                await manager.send(player_id, {
                    "status": "ERROR",
                    "message": f"Falha na resolução: {str(e)[:120]}",
                })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await manager.send(player_id, {"status": "SERVER_ERROR", "message": str(e)[:120]})
        except Exception:
            pass
    finally:
        await manager.disconnect(player_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
