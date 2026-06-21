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
import glob
import json
import logging
import math
import os
import re
import shutil
import time

logger = logging.getLogger("nexus")
logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — abre o banco (recriando do zero se estiver corrompido, ver
    # WorldStore.init) e cria as tabelas
    await world_store.init()
    # Backup periódico em background — ver periodic_backup_task. Existe
    # porque um nexus_world.db já foi apagado por engano durante uma sessão
    # de testes (sem mecanismo de proteção nenhum até agora).
    backup_task = asyncio.create_task(periodic_backup_task())
    yield
    # Shutdown — encerra o backup periódico e fecha o banco com segurança
    backup_task.cancel()
    try:
        await backup_task
    except asyncio.CancelledError:
        pass
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
OBJ_CELL = 48.0      # tamanho da célula de estrutura — mesmo valor de OBJ_CELL no frontend
DB_PATH = "nexus_world.db"
BACKUP_DIR = "backups"
BACKUP_INTERVAL = 15 * 60  # segundos entre snapshots automáticos
BACKUP_KEEP = 10           # quantos snapshots manter (os mais antigos são descartados)


def backup_db_once(db_path: str = DB_PATH, backup_dir: str = BACKUP_DIR, keep: int = BACKUP_KEEP) -> Optional[str]:
    """Copia o .db atual pra backups/nexus_world_<timestamp>.db e descarta
    os mais antigos além de `keep`. Síncrono de propósito (cópia de arquivo
    é barata e rápida) — chamado tanto pela task periódica (via
    asyncio.to_thread, pra não bloquear o loop de eventos) quanto pelo
    script manual tools/backup_db.py, pra nunca duplicar essa lógica.
    Retorna o caminho do backup criado, ou None se ainda não havia banco
    pra copiar (não é erro — só não tem o que proteger ainda)."""
    if not os.path.exists(db_path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"nexus_world_{stamp}.db")
    # Backup manual + task periódica podem, em tese, cair no mesmo segundo
    # (granularidade do timestamp) — sem isso, o segundo a chegar sobrescrevia
    # o arquivo do primeiro em vez de criar um snapshot novo.
    suffix = 1
    while os.path.exists(dest):
        dest = os.path.join(backup_dir, f"nexus_world_{stamp}_{suffix}.db")
        suffix += 1
    shutil.copy2(db_path, dest)
    existing = sorted(glob.glob(os.path.join(backup_dir, "nexus_world_*.db")))
    for old in existing[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


async def periodic_backup_task():
    """Roda em background a vida toda do processo: um snapshot imediato no
    boot (cobre o caso de o servidor cair antes do primeiro intervalo) e
    depois um a cada BACKUP_INTERVAL segundos. Não substitui o script manual
    (tools/backup_db.py) — é a rede de segurança pro cenário que já nos
    pegou uma vez nesta sessão: apagar/corromper o .db sem ter rodado
    nenhum backup manual antes."""
    while True:
        try:
            path = await asyncio.to_thread(backup_db_once)
            if path:
                logger.info(f"[backup] snapshot salvo em {path}")
        except Exception as e:
            logger.warning(f"[backup] falhou: {e}")
        await asyncio.sleep(BACKUP_INTERVAL)

# ════════════════════════════════════════════════════════════
# FORGE DE ITENS — catálogo predefinido (sem IA generativa ainda)
# ════════════════════════════════════════════════════════════
# Fonte única dos NÚMEROS de combate — mandado ao cliente na conexão
# (CONNECTED.item_catalog) pra nunca duplicar damage/range/speed/defense
# em dois lugares (o erro que já causou divergência uma vez no projeto,
# ver terrainHeight/terrainHeightJS no histórico do CLAUDE.md). O cliente
# só guarda localmente o mapeamento tipo→forma geométrica, que é
# inerentemente um dado de render, não de combate.
ITEM_CATALOG: dict[str, dict] = {
    "espada": {"label": "Espada", "damage": 40, "range": 4.0,  "speed": 3.0, "defense": 0},
    "lanca":  {"label": "Lança",  "damage": 40, "range": 8.0,  "speed": 1.5, "defense": 0},
    "arco":   {"label": "Arco",   "damage": 20, "range": 60.0, "speed": 1.0, "defense": 0},
    "escudo": {"label": "Escudo", "damage": 10, "range": 3.0,  "speed": 1.0, "defense": 10},
}
# Jogador sem nada equipado: mesmo dano/alcance de antes da Forge existir
# (destrói em 1 tiro, alcance 50) — zero regressão pra quem nunca forjar.
DEFAULT_WEAPON: dict = {"label": "Desarmado", "damage": 40, "range": 50.0, "speed": 999.0, "defense": 0}
# Vida das estruturas "vivas" — fixo e único (elas não são item, não tem
# por que variar por tipo). Com damage=40 do desarmado/espada/lança, ainda
# morre em 1 tiro só; arco (20) precisa de 2; escudo (10) precisa de 4 —
# é o que torna o dano da arma um efeito REAL, não só um número guardado.
STRUCTURE_MAX_HP = 40

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
    item_type: Optional[str] = None    # para FORGE
    item_id: Optional[str] = None      # para EQUIP

    @field_validator("action_type")
    @classmethod
    def valid_action(cls, v: str) -> str:
        allowed = {"INTERACT", "COLLIDE", "SHOOT", "MOVE", "PING", "SET_STYLE", "FORGE", "EQUIP"}
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
        if v is not None and v not in (0, 1, 2, 3):
            raise ValueError("world_style deve ser 0, 1, 2 ou 3")
        return v

    @field_validator("position")
    @classmethod
    def valid_position(cls, v):
        # NaN/Infinity passariam por aqui sem isso e seriam rebroadcastados
        # pra outros jogadores; um valor não-serializável dentro do broadcast
        # faz _safe_send tratar o envio a CADA outro jogador como "falhou" e
        # desconectá-los — efeito colateral grave de um payload malformado
        # de UM jogador só. Rejeitar aqui devolve um ERROR normal pro emissor
        # em vez disso.
        if v is not None and (len(v) != 3 or not all(math.isfinite(x) for x in v)):
            raise ValueError("position deve ter exatamente 3 números finitos")
        return v


# ════════════════════════════════════════════════════════════
# CHUNK SYSTEM — persistência em malha 2D
# ════════════════════════════════════════════════════════════
def chunk_coords(x: float, z: float) -> tuple[int, int]:
    return (int(x // CHUNK_SIZE), int(z // CHUNK_SIZE))


# Casa "struct_<cx>_<cz>" e também "struct_<cx>_<cz>_loot" (sufixo de saque) —
# ambos pertencem à mesma estrutura física, então devem cair no(s) mesmo(s) chunk(s).
STRUCT_ID_RE = re.compile(r"^struct_(-?\d+)_(-?\d+)")


def object_home_chunks(obj_id: str) -> list[tuple[int, int]]:
    """Todos os chunks (16u) cobertos pela célula de 48u de uma estrutura,
    derivados do PRÓPRIO id — não de onde o jogador estava ao agir sobre ela.
    OBJ_CELL é múltiplo exato de CHUNK_SIZE (48 = 3×16), então isso sempre
    resulta numa grade 3×3 de chunks. Sem isso, a mesma estrutura podia ser
    "lembrada" em chunks diferentes dependendo de onde o tiro partiu, e voltar
    por outro lado da estrutura não reconciliava (objeto reaparecia).
    Retorna lista vazia se o id não seguir esse formato (chamador decide o
    fallback, ex: chunk atual do jogador)."""
    m = STRUCT_ID_RE.match(obj_id)
    if not m:
        return []
    ocx, ocz = int(m.group(1)), int(m.group(2))
    x0, z0 = ocx * OBJ_CELL, ocz * OBJ_CELL
    cx0, cz0 = chunk_coords(x0, z0)
    cx1, cz1 = chunk_coords(x0 + OBJ_CELL - 1e-6, z0 + OBJ_CELL - 1e-6)
    return [(cx, cz) for cx in range(cx0, cx1 + 1) for cz in range(cz0, cz1 + 1)]


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
        """Cria a conexão e as tabelas. Chamado no startup. Se o arquivo
        existir mas não for um SQLite válido (corrompido — disco cheio
        truncando o arquivo, cópia interrompida, etc.), move ele de lado em
        vez de deixar o startup inteiro quebrar: o servidor sempre sobe com
        um mundo (vazio, nesse caso) em vez de nunca subir."""
        try:
            await self._open_and_migrate()
        except aiosqlite.Error as e:
            if self._db:
                await self._db.close()
                self._db = None
            if os.path.exists(self.db_path):
                stamp = time.strftime("%Y%m%d_%H%M%S")
                corrupted_path = f"{self.db_path}.corrupted-{stamp}"
                os.rename(self.db_path, corrupted_path)
                logger.warning(
                    f"[world_store] banco corrompido ({e}) — movido para "
                    f"'{corrupted_path}'. Recriando um banco limpo em '{self.db_path}'."
                )
            await self._open_and_migrate()

    async def _open_and_migrate(self):
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
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS player_items (
                player_id TEXT    NOT NULL,
                item_id   TEXT    NOT NULL,
                item_type TEXT    NOT NULL,
                equipped  INTEGER NOT NULL DEFAULT 0,
                created   REAL    NOT NULL,
                PRIMARY KEY (player_id, item_id)
            )
        """)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def _read_state_locked(self, obj_id: str, chunks: list[tuple[int, int]]) -> Optional[dict]:
        """Só chamar com self._lock já adquirido pelo caller."""
        for cx, cz in chunks:
            cursor = await self._db.execute(
                "SELECT state FROM chunk_modifications WHERE cx=? AND cz=? AND obj_id=?",
                (cx, cz, obj_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                try:
                    return json.loads(row["state"])
                except json.JSONDecodeError:
                    return {}
        return None

    async def _write_state_locked(self, obj_id: str, chunks: list[tuple[int, int]], new_state: dict) -> None:
        """Só chamar com self._lock já adquirido pelo caller."""
        payload = json.dumps(new_state)
        now = time.time()
        for cx, cz in chunks:
            await self._db.execute(
                """INSERT INTO chunk_modifications (cx, cz, obj_id, state, updated)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(cx, cz, obj_id)
                   DO UPDATE SET state=excluded.state, updated=excluded.updated""",
                (cx, cz, obj_id, payload, now),
            )
        await self._db.commit()

    async def try_claim(self, obj_id: str, chunks: list[tuple[int, int]], new_state: dict) -> tuple[bool, dict]:
        """Tenta ser o único a resolver obj_id nesses chunks, de uma vez só
        (sem vida/múltiplos hits — usado por coleta). Dentro de UMA única
        aquisição do lock, confere se já existe alguma resolução e, se não
        existir, grava `new_state` — check e write atômicos, sem brecha pra
        outro jogador colar entre os dois. Sem isso, dois jogadores agindo
        no mesmo objeto quase ao mesmo tempo passavam os dois pelo check
        (ainda vazio) antes de qualquer um escrever, e os dois pontuavam
        pelo mesmo efeito.
        Retorna (True, new_state) se este caller venceu a corrida, ou
        (False, estado_já_existente) se alguém já resolveu."""
        async with self._lock:
            existing = await self._read_state_locked(obj_id, chunks)
            if existing is not None:
                return False, existing
            await self._write_state_locked(obj_id, chunks, new_state)
            return True, new_state

    async def apply_damage(self, obj_id: str, chunks: list[tuple[int, int]], damage: int,
                            max_hp: int, meta: dict) -> tuple[dict, bool]:
        """Aplica `damage` num objeto com vida: lê o hp atual (ou parte de
        max_hp se for o primeiro hit), e escreve o novo hp — ou o estado
        terminal "destroyed" se a vida zerar — tudo dentro de UMA aquisição
        do lock. Sem isso, dois hits simultâneos no mesmo objeto podiam ler
        o MESMO hp antigo e cada um aplicar dano sobre ele, perdendo um dos
        hits (o mesmo tipo de corrida que try_claim resolve pra efeitos de
        um hit só — aqui generalizado pra hits que se acumulam).
        `meta` são os campos comuns (by/tags/position/timestamp) já
        calculados pelo caller — não dependem da corrida, só o hp depende.
        Retorna (estado_final, causei_eu_a_destruição) — o segundo valor
        diferencia "eu destruí agora" de "já estava destruído quando cheguei"
        (não pontuar por um kill que não foi seu)."""
        async with self._lock:
            existing = await self._read_state_locked(obj_id, chunks)
            if existing is not None and existing.get("status") in ("destroyed", "collected"):
                return existing, False
            current_hp = existing["hp_remaining"] if existing and existing.get("status") == "damaged" else max_hp
            new_hp = current_hp - damage
            if new_hp <= 0:
                new_state = {**meta, "status": "destroyed"}
                await self._write_state_locked(obj_id, chunks, new_state)
                return new_state, True
            new_state = {**meta, "status": "damaged", "hp_remaining": new_hp, "max_hp": max_hp}
            await self._write_state_locked(obj_id, chunks, new_state)
            return new_state, False

    async def find_modification(self, obj_id: str, chunks: list[tuple[int, int]]) -> Optional[dict]:
        """Procura um obj_id já resolvido em qualquer um dos chunks dados —
        memória global do mundo, usada pra impedir pontuar de novo algo que
        já está destruído/coletado (outro jogador, ou antes de um restart)."""
        async with self._lock:
            for cx, cz in chunks:
                cursor = await self._db.execute(
                    "SELECT state FROM chunk_modifications WHERE cx=? AND cz=? AND obj_id=?",
                    (cx, cz, obj_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row:
                    try:
                        return json.loads(row["state"])
                    except json.JSONDecodeError:
                        return {}
        return None

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

    async def add_player_item(self, player_id: str, item_id: str, item_type: str) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO player_items (player_id, item_id, item_type, equipped, created) VALUES (?, ?, ?, 0, ?)",
                (player_id, item_id, item_type, time.time()),
            )
            await self._db.commit()

    async def set_equipped_item(self, player_id: str, item_id: str) -> None:
        async with self._lock:
            await self._db.execute("UPDATE player_items SET equipped=0 WHERE player_id=?", (player_id,))
            await self._db.execute(
                "UPDATE player_items SET equipped=1 WHERE player_id=? AND item_id=?", (player_id, item_id)
            )
            await self._db.commit()

    async def load_player_items(self, player_id: str, state: "PlayerState") -> None:
        """Hidrata state.items/equipped_item_id a partir do SQLite — chamado
        a cada conexão. O PlayerState em memória nasce vazio tanto num
        reload de página (mesmo player_id salvo no localStorage do
        cliente) quanto num reinício do servidor; nos dois casos é o banco
        quem garante a continuidade, nunca a memória do processo."""
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT item_id, item_type, equipped FROM player_items WHERE player_id=?",
                (player_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        for row in rows:
            state.items[row["item_id"]] = {"type": row["item_type"]}
            if row["equipped"]:
                state.equipped_item_id = row["item_id"]


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
        self.current_chunk: Optional[tuple[int, int]] = None
        self.destroyed_objects: set[str] = set()
        self.items: dict[str, dict] = {}
        self.equipped_item_id: Optional[str] = None

    def equipped_weapon(self) -> dict:
        """Stats de combate em uso agora — do item equipado (se existir e
        ainda for um tipo válido do catálogo) ou do desarmado por padrão."""
        if self.equipped_item_id and self.equipped_item_id in self.items:
            item_type = self.items[self.equipped_item_id]["type"]
            return ITEM_CATALOG.get(item_type, DEFAULT_WEAPON)
        return DEFAULT_WEAPON

    def snapshot(self) -> dict:
        return {
            "position": self.position,
            "inventory": dict(self.inventory),
            "health": self.health,
            "score": self.score,
            "world_style": self.world_style,
            "items": [{"id": iid, "type": idata["type"]} for iid, idata in self.items.items()],
            "equipped_item_id": self.equipped_item_id,
        }


# ════════════════════════════════════════════════════════════
# MOTOR SEMÂNTICO (Pilar 2)
# ════════════════════════════════════════════════════════════
def make_persisted_meta(action: ActionPayload, state: PlayerState) -> dict:
    """Campos comuns de qualquer estado persistido de objeto: quem alterou,
    com que tags, onde e quando. Usado tanto por efeitos de um hit só
    (destroyed/collected, via try_claim — status já conhecido antes do
    write) quanto por efeitos com vida que podem virar "damaged" ou
    "destroyed" dependendo do hp atual (via apply_damage — só decidido
    DENTRO do lock, depois de ler o hp), pra nunca duplicar essa montagem."""
    return {
        "by": action.player_id,
        "tags": sorted(set(action.target_tags)),
        "position": action.position or state.position,
        "timestamp": time.time(),
    }


def make_persisted_state(status: str, action: ActionPayload, state: PlayerState, **extra) -> dict:
    return {**make_persisted_meta(action, state), "status": status, **extra}


class SemanticEngine:
    @staticmethod
    async def resolve(action: ActionPayload, state: PlayerState, store: WorldStore) -> dict:
        """Retorna a resposta a mandar pro cliente. Persistência (quando o
        efeito precisa sobreviver no mundo) acontece aqui dentro, via
        store.try_claim ou store.apply_damage — nunca depois, num passo
        separado: foi exatamente o gap entre "checar se já foi resolvido" e
        "escrever que resolvi" em dois awaits diferentes que permitia dois
        jogadores destruindo o MESMO objeto quase ao mesmo tempo pontuarem
        os dois."""
        tags = set(action.target_tags)
        atype = action.action_type
        oid = action.target_id or "anon"

        if oid in state.destroyed_objects:
            return {"status": "ALREADY_RESOLVED", "target_id": oid}

        homes = object_home_chunks(oid) or [chunk_coords(state.position[0], state.position[2])]

        # Fast-path só pra coleta (um hit só, sem vida): se o objeto já tem
        # efeito persistido, nem entra na lógica — só confirma o estado
        # salvo. O fluxo de dano com vida (destruivel, abaixo) NÃO passa por
        # aqui — apply_damage já faz essa checagem atomicamente por conta
        # própria, e fazer os dois seria a mesma checagem em dois lugares.
        if "coletavel" in tags:
            persisted = await store.find_modification(oid, homes)
            if persisted is not None:
                state.destroyed_objects.add(oid)
                return {"status": "ALREADY_RESOLVED", "target_id": oid, "resolved_state": persisted}

        result: dict = {
            "status": "NO_EFFECT",
            "target_id": oid,
            "visual_trigger": None,
            "player_state": None,
        }

        if atype == "SHOOT" and "destruivel" in tags:
            weapon = state.equipped_weapon()
            meta = make_persisted_meta(action, state)
            final_state, caused_destroy = await store.apply_damage(oid, homes, weapon["damage"], STRUCTURE_MAX_HP, meta)
            if final_state.get("status") in ("destroyed", "collected") and not caused_destroy:
                # Já estava destruído quando chegamos (outro jogador ganhou
                # a corrida) — não é nosso kill, não pontua.
                state.destroyed_objects.add(oid)
                return {"status": "ALREADY_RESOLVED", "target_id": oid, "resolved_state": final_state}
            if caused_destroy:
                state.destroyed_objects.add(oid)
                state.score += 10
                bonus = None
                if "hostil" in tags:
                    state.score += 25
                    bonus = "HOSTILE_ELIMINATED"
                result.update({
                    "status": "DESTROYED",
                    "visual_trigger": "EXPLODE_PARTICLES",
                    "player_state": state.snapshot(),
                })
                if bonus:
                    result["bonus"] = bonus
                return result
            # Hp não zerou ainda — estrutura continua viva e shootável,
            # então NÃO entra em destroyed_objects.
            result.update({
                "status": "DAMAGED",
                "visual_trigger": "HIT_SPARK",
                "hp_remaining": final_state["hp_remaining"],
                "max_hp": final_state["max_hp"],
            })
            return result

        if atype == "SHOOT" and "hostil" in tags:
            result.update({"status": "DAMAGED", "visual_trigger": "HIT_SPARK"})
            return result

        if atype in ("INTERACT", "COLLIDE") and "coletavel" in tags:
            item = next((t for t in tags if t not in
                         {"coletavel", "destruivel", "hostil", "solido"}), "recurso")
            new_state = make_persisted_state("collected", action, state, item=item)
            won, final_state = await store.try_claim(oid, homes, new_state)
            state.destroyed_objects.add(oid)
            if not won:
                return {"status": "ALREADY_RESOLVED", "target_id": oid, "resolved_state": final_state}
            state.inventory[item] += 1
            result.update({
                "status": "COLLECTED",
                "visual_trigger": "PICKUP_SHINE",
                "collected_item": item,
                "player_state": state.snapshot(),
            })
            return result

        if atype == "COLLIDE" and "hostil" in tags:
            defense = state.equipped_weapon()["defense"]
            incoming = max(1, 15 - defense)  # nunca zero -- defesa reduz, não anula
            state.health = max(0, state.health - incoming)
            result.update({
                "status": "PLAYER_HIT",
                "visual_trigger": "DAMAGE_FLASH",
                "player_state": state.snapshot(),
            })
            return result

        if atype == "COLLIDE" and "solido" in tags:
            result.update({"status": "BLOCKED"})
            return result

        if atype == "INTERACT":
            result.update({"status": "INTERACTED", "visual_trigger": "GLOW_PULSE"})
            return result

        return result


# ════════════════════════════════════════════════════════════
# GERENCIADOR DE CONEXÕES
# ════════════════════════════════════════════════════════════
class ConnectionManager:
    def __init__(self):
        # Um pid pode ter MAIS DE UMA conexão viva ao mesmo tempo (duas abas
        # do navegador com o mesmo player_id, já que o id é persistido no
        # localStorage — basta abrir o jogo de novo sem fechar a aba antiga).
        # Por isso um set por pid, não uma websocket só: com uma só, a aba
        # nova sobrescrevia o registro da aba velha e send() passava a
        # mandar TODA resposta (FORGE/SHOOT/dano) só pra aba nova — a aba
        # velha continuava agindo (mutando o PlayerState, que é compartilhado
        # pelo pid) mas nunca via nenhuma resposta, parecendo travada.
        self._conns: dict[str, set[WebSocket]] = defaultdict(set)
        self._states: dict[str, PlayerState] = {}
        self._lock = asyncio.Lock()

    async def connect(self, pid: str, ws: WebSocket) -> bool:
        """Retorna True se esta é a PRIMEIRA conexão viva deste pid — é esse
        momento (não cada aba nova) que deve virar um PLAYER_JOINED pros
        outros, já que múltiplas abas do mesmo pid são o MESMO jogador
        (compartilham PlayerState, ver comentário da classe)."""
        await ws.accept()
        async with self._lock:
            is_new_presence = len(self._conns.get(pid, ())) == 0
            self._conns[pid].add(ws)
            if pid not in self._states:
                self._states[pid] = PlayerState(pid)
        return is_new_presence

    async def disconnect(self, pid: str, ws: WebSocket) -> bool:
        """Retorna True se esta foi a ÚLTIMA conexão viva deste pid — só
        nesse caso o jogador "saiu de verdade" e os outros devem ver um
        PLAYER_LEFT (fechar uma de duas abas não tira o jogador de cena).

        Idempotente de propósito: _safe_send chama isso quando um
        broadcast/send falha pra uma conexão já morta, e o `finally` do
        handler principal chama de novo pra MESMA (pid, ws) quando o loop de
        recepção dele também percebe a queda — as duas coisas podem
        acontecer pra a mesma desconexão. Sem o `self._conns.get(pid)` aqui,
        a segunda chamada recriava a entrada (defaultdict) e devolvia True
        de novo, mandando um PLAYER_LEFT duplicado."""
        async with self._lock:
            conns = self._conns.get(pid)
            if conns is None or ws not in conns:
                return False  # já foi limpo por uma chamada anterior
            conns.discard(ws)
            if not conns:
                self._conns.pop(pid, None)
                # player_id é aleatório por sessão de página — sem isso,
                # rate_store acumula uma entrada por visita pra sempre num
                # servidor de longa duração. Só limpa quando a ÚLTIMA aba
                # desse pid cai, pra não zerar o limite de outra aba ainda viva.
                rate_store.pop(pid, None)
                return True
        return False

    def get_state(self, pid: str) -> PlayerState:
        if pid not in self._states:
            self._states[pid] = PlayerState(pid)
        return self._states[pid]

    def list_players(self, exclude_pid: Optional[str] = None) -> list[dict]:
        """Snapshot de quem está conectado agora (um por pid, não por aba) —
        mandado pro jogador que acabou de entrar, pra ele não "perder" quem
        já estava lá antes do primeiro PLAYER_JOINED que ele vai receber."""
        return [
            {"player_id": pid, "position": self._states[pid].position}
            for pid in self._conns
            if pid != exclude_pid and pid in self._states
        ]

    async def _safe_send(self, pid: str, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            await self.disconnect(pid, ws)

    async def send(self, pid: str, msg: dict):
        # Manda pra TODAS as abas vivas desse pid — elas compartilham o
        # mesmo PlayerState, então todas precisam ver o resultado de qualquer
        # ação (de qualquer uma delas) pra não ficarem com inventário/score
        # divergente da que realmente está no servidor.
        for ws in list(self._conns.get(pid, ())):
            await self._safe_send(pid, ws, msg)

    async def broadcast(self, msg: dict, exclude_pid: Optional[str] = None):
        # Pra TODOS os pids conectados, exceto exclude_pid (todas as abas
        # dele) — usado pra presença/posição de jogadores remotos, nunca
        # ecoa de volta pro próprio jogador que disparou o evento.
        for pid, conns in list(self._conns.items()):
            if pid == exclude_pid:
                continue
            for ws in list(conns):
                await self._safe_send(pid, ws, msg)


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
        "connections": sum(len(s) for s in manager._conns.values()),
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
    is_new_presence = await manager.connect(player_id, websocket)
    state = manager.get_state(player_id)
    # Hidrata o inventário forjado a partir do SQLite a cada conexão —
    # cobre tanto reload de página (mesmo player_id no localStorage do
    # cliente) quanto reinício do servidor, com o mesmo código nos dois
    # casos (o PlayerState em memória nasce vazio igual nas duas situações).
    await world_store.load_player_items(player_id, state)

    await manager.send(player_id, {
        "status": "CONNECTED",
        "player_id": player_id,
        "player_state": state.snapshot(),
        "chunk_size": CHUNK_SIZE,
        "item_catalog": ITEM_CATALOG,
        "default_weapon": DEFAULT_WEAPON,
        # Quem já está no mundo agora — sem isso, um jogador que entra depois
        # de outros só fica sabendo deles no próximo MOVE de cada um (ou nunca,
        # se ninguém se mover). Um por pid, não por aba (ver list_players).
        "players": manager.list_players(exclude_pid=player_id),
    })

    # Só multi-aba do MESMO pid não conta como "jogador novo" pros outros
    # (ver connect() — eles já sabem desse pid desde a primeira aba dele).
    if is_new_presence:
        await manager.broadcast({
            "status": "PLAYER_JOINED",
            "player_id": player_id,
            "position": state.position,
        }, exclude_pid=player_id)

    # Fase M4 — robustez de reconexão: reconcile_chunk só manda CHUNK_RECONCILE
    # quando o chunk MUDA (ver lá), mas state.current_chunk sobrevive no
    # PlayerState entre conexões (nunca é limpo no disconnect). Sem isso, um
    # cliente que cai e volta SEM trocar de chunk (rede caiu, F5, ou outro
    # dispositivo entrando direto onde já estava) nunca recebia o estado
    # salvo do chunk em que já estava — a memória do JS é zerada a cada nova
    # conexão (recarrega a página ou abre em outra aba/dispositivo), mas o
    # servidor pensava "ele já sabe disso". Resetar aqui força o reconcile
    # de novo nesta conexão, mesmo estando no mesmo lugar de antes.
    state.current_chunk = None
    try:
        await reconcile_chunk(player_id, state, state.position[0], state.position[2])
    except Exception:
        pass

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

            # FORGE — cria um item do catálogo e adiciona ao inventário
            if payload.action_type == "FORGE":
                item_type = payload.item_type
                if item_type not in ITEM_CATALOG:
                    await manager.send(player_id, {
                        "status": "ERROR",
                        "message": f"Tipo de item inválido: {item_type}",
                    })
                    continue
                # Contador local é seguro como sufixo do id porque o
                # inventário já foi hidratado do banco ANTES do primeiro
                # FORGE desta sessão (ver load_player_items) — nunca colide
                # com um item de uma sessão anterior do mesmo jogador.
                item_id = f"{item_type}_{len(state.items)}"
                state.items[item_id] = {"type": item_type}
                await world_store.add_player_item(player_id, item_id, item_type)
                await manager.send(player_id, {
                    "status": "FORGED",
                    "item": {"id": item_id, "type": item_type, "label": ITEM_CATALOG[item_type]["label"]},
                    "player_state": state.snapshot(),
                })
                continue

            # EQUIP — troca o item ativo (1 slot só)
            if payload.action_type == "EQUIP":
                item_id = payload.item_id
                if not item_id or item_id not in state.items:
                    await manager.send(player_id, {
                        "status": "ERROR",
                        "message": f"Item desconhecido: {item_id}",
                    })
                    continue
                state.equipped_item_id = item_id
                await world_store.set_equipped_item(player_id, item_id)
                await manager.send(player_id, {
                    "status": "EQUIPPED",
                    "item_id": item_id,
                    "item_type": state.items[item_id]["type"],
                    "player_state": state.snapshot(),
                })
                continue

            # MOVE — atualiza posição + reconciliação de chunk
            if payload.action_type == "MOVE":
                if payload.position and len(payload.position) == 3:
                    state.position = payload.position
                    # Broadcast pros outros jogadores verem este se mexer —
                    # exclude_pid evita eco pra qualquer aba do próprio
                    # jogador (já throttled a ~10Hz no cliente, ver loop()).
                    await manager.broadcast({
                        "status": "PLAYER_MOVED",
                        "player_id": player_id,
                        "position": state.position,
                    }, exclude_pid=player_id)
                    try:
                        await reconcile_chunk(
                            player_id, state,
                            payload.position[0], payload.position[2]
                        )
                    except Exception:
                        pass
                continue

            # Resolução semântica — a persistência (quando o efeito precisa
            # sobreviver no mundo) já acontece DENTRO do resolve(), atômica
            # com a checagem de "alguém já resolveu isso" (ver
            # WorldStore.try_claim). Nunca em dois passos separados aqui.
            try:
                result = await engine.resolve(payload, state, world_store)
                await manager.send(player_id, result)
                # Destruir/coletar muda o mundo pra TODO MUNDO (a persistência
                # por chunk já garante isso pra quem entra depois — ver
                # CHUNK_RECONCILE — isso aqui é só o broadcast em tempo real
                # pra quem já está no chunk agora). DAMAGED fica de fora de
                # propósito: não tem representação visual hoje (sem barra de
                # vida na estrutura), nada pra sincronizar ainda.
                action_status = {"DESTROYED": "destroyed", "COLLECTED": "collected"}.get(result.get("status"))
                if action_status:
                    await manager.broadcast({
                        "status": "PLAYER_ACTION",
                        "target_id": result.get("target_id"),
                        "action_status": action_status,
                    }, exclude_pid=player_id)
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
        is_last_connection = await manager.disconnect(player_id, websocket)
        if is_last_connection:
            await manager.broadcast({
                "status": "PLAYER_LEFT",
                "player_id": player_id,
            }, exclude_pid=player_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    