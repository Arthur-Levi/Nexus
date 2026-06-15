from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
import os
import asyncio
import json
import time
import random
import re
import unicodedata
import hashlib
from collections import defaultdict

app = FastAPI(title="Nexus: Unbound - Core Server")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

# --- Config ---
API_KEYS = {"dev-key-abc123", "prod-key-xyz789"}
RATE_LIMIT_MAX = 20
RATE_LIMIT_WINDOW = 60
WS_TIMEOUT = 600

rate_store: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(client_id: str) -> bool:
    now = time.time()
    rate_store[client_id] = [t for t in rate_store[client_id] if now - t < RATE_LIMIT_WINDOW]
    if len(rate_store[client_id]) >= RATE_LIMIT_MAX:
        return True
    rate_store[client_id].append(now)
    return False

# --- Gerador Procedural por Palavras-Chave ---

TEMAS = {
    "cyberpunk": {
        "skybox": "cyberpunk_neon",
        "cor": 0x00ffcc,
        "heroi_classe": "Hacker de Rua",
        "inimigo_nome": "Drone Sentinel MK-7",
        "habilidades": [
            {"nome": "Pulso EMP", "dano": 35, "custo_mana": 20, "descricao": "Desativa circuitos inimigos"},
            {"nome": "Hack Neural", "dano": 50, "custo_mana": 40, "descricao": "Invade a mente do alvo"},
            {"nome": "Barreira de Dados", "dano": 0, "custo_mana": 15, "descricao": "Cria escudo de firewall (+30 defesa temporária)"},
        ],
        "ambiente": "prédios neon",
    },
    "medieval": {
        "skybox": "medieval_castle",
        "cor": 0xff8800,
        "heroi_classe": "Cavaleiro da Ordem Antiga",
        "inimigo_nome": "Dragão das Ruínas",
        "habilidades": [
            {"nome": "Golpe Sagrado", "dano": 40, "custo_mana": 25, "descricao": "Bênção divina em aço"},
            {"nome": "Escudo de Ferro", "dano": 0, "custo_mana": 10, "descricao": "Reduz dano recebido em 50%"},
            {"nome": "Fúria Berserker", "dano": 70, "custo_mana": 50, "descricao": "Ataque devastador sem defesa"},
        ],
        "ambiente": "torres medievais",
    },
    "magia": {
        "skybox": "arcane_void",
        "cor": 0xaa00ff,
        "heroi_classe": "Arquimago das Sombras",
        "inimigo_nome": "Golem de Cristal Arcano",
        "habilidades": [
            {"nome": "Bola de Fogo", "dano": 45, "custo_mana": 30, "descricao": "Chama concentrada do inferno"},
            {"nome": "Raio Gélido", "dano": 30, "custo_mana": 20, "descricao": "Congela e fragmenta o alvo"},
            {"nome": "Teletransporte", "dano": 20, "custo_mana": 15, "descricao": "Ataque surpresa pós-teleporte"},
        ],
        "ambiente": "torres arcanas",
    },
    "rpg": {
        "skybox": "fantasy_plains",
        "cor": 0x44ff88,
        "heroi_classe": "Aventureiro Lendário",
        "inimigo_nome": "Orc Guerreiro Chefe",
        "habilidades": [
            {"nome": "Espada Dupla", "dano": 38, "custo_mana": 0, "descricao": "Ataque físico rápido e preciso"},
            {"nome": "Flecha Envenenada", "dano": 25, "custo_mana": 10, "descricao": "Veneno que drena vida por turno"},
            {"nome": "Cura Herbal", "dano": -40, "custo_mana": 30, "descricao": "Recupera pontos de vida"},
        ],
        "ambiente": "ruínas mágicas",
    },
}

DEFAULT_TEMA = {
    "skybox": "void_space",
    "cor": 0xffffff,
    "heroi_classe": "Viajante Dimensional",
    "inimigo_nome": "Entidade Desconhecida",
    "habilidades": [
        {"nome": "Golpe Básico", "dano": 30, "custo_mana": 0, "descricao": "Ataque físico simples"},
        {"nome": "Explosão de Energia", "dano": 55, "custo_mana": 35, "descricao": "Rajada de energia pura"},
        {"nome": "Escudo Dimensional", "dano": 0, "custo_mana": 20, "descricao": "Proteção temporária"},
    ],
    "ambiente": "estruturas alienígenas",
}

def detectar_tema(prompt: str) -> dict:
    prompt_lower = prompt.lower()
    tema_final = dict(DEFAULT_TEMA)
    tema_final["habilidades"] = list(DEFAULT_TEMA["habilidades"])

    for palavra, dados in TEMAS.items():
        if palavra in prompt_lower:
            tema_final = dict(dados)
            tema_final["habilidades"] = list(dados["habilidades"])
            break

    # Fusão de temas (ex: "cyberpunk com magia")
    extras_hab = []
    for palavra, dados in TEMAS.items():
        if palavra in prompt_lower and dados != tema_final:
            extras_hab.extend(dados["habilidades"][:1])
            if "nome" not in tema_final.get("heroi_classe", ""):
                tema_final["heroi_classe"] += f" / {dados['heroi_classe']}"
            break

    tema_final["habilidades"] = (tema_final["habilidades"] + extras_hab)[:4]
    return tema_final

# --- Gene Classifier: classifica o gênero do jogo por vetor de palavras-chave ---
# Cada gênero é um "gene" = conjunto de tokens ponderados. A classificação é o
# produto escalar entre o saco-de-palavras do prompt e os pesos de cada gênero,
# normalizado para virar uma confiança. Puro: sem assets, sem modelo externo.

GENEROS = {
    "creature_collector": {
        "nome": "Colecionador de Criaturas",
        "modo": "captura",
        "genes": {
            "criatura": 3, "criaturas": 3, "monstro": 3, "monstros": 3, "bicho": 2,
            "capturar": 3, "captura": 3, "coletar": 3, "colecionar": 3, "colecao": 2,
            "pokemon": 4, "treinador": 3, "domar": 2, "evoluir": 2, "fera": 2, "besta": 2,
        },
    },
    "space_combat": {
        "nome": "Combate Espacial",
        "modo": "batalha",
        "genes": {
            "espaco": 3, "espacial": 3, "nave": 3, "naves": 3, "alien": 3, "alienigena": 3,
            "galaxia": 3, "estrela": 2, "planeta": 2, "laser": 2, "foguete": 2, "astronauta": 3,
            "cosmos": 2, "orbita": 2, "asteroide": 2, "robo": 2, "ficcao": 2,
        },
    },
    "dungeon_crawler": {
        "nome": "Explorador de Masmorras",
        "modo": "batalha",
        "genes": {
            "masmorra": 3, "masmorras": 3, "dungeon": 3, "caverna": 2, "labirinto": 3,
            "explorar": 2, "tesouro": 2, "armadilha": 2, "cripta": 2, "ruina": 2, "ruinas": 2,
        },
    },
    "rpg_battle": {
        "nome": "Batalha RPG",
        "modo": "batalha",
        "genes": {
            "rpg": 3, "batalha": 3, "luta": 3, "heroi": 2, "guerreiro": 2, "espada": 2,
            "mago": 2, "cavaleiro": 2, "dragao": 2, "aventura": 2, "combate": 2, "magia": 2,
        },
    },
}

DEFAULT_GENERO = "rpg_battle"

def _normalizar(texto: str) -> str:
    # remove acentos para que os genes ASCII casem com entradas como "espaço" / "herói"
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def classificar_genero(prompt: str) -> dict:
    tokens = re.findall(r"[a-z0-9]+", _normalizar(prompt or ""))
    contagem: dict[str, int] = {}
    for tok in tokens:
        contagem[tok] = contagem.get(tok, 0) + 1

    scores = {}
    for gid, dados in GENEROS.items():
        s = 0
        for gene, peso in dados["genes"].items():
            if gene in contagem:
                s += peso * contagem[gene]
        scores[gid] = s

    total = sum(scores.values())
    if total <= 0:
        melhor = DEFAULT_GENERO
        confianca = 0.0
    else:
        melhor = max(scores, key=lambda g: scores[g])
        confianca = round(scores[melhor] / total, 3)

    return {
        "genero": melhor,
        "nome": GENEROS[melhor]["nome"],
        "modo": GENEROS[melhor]["modo"],
        "confianca": confianca,
        "scores": scores,
    }

def gerar_seed(prompt: str) -> int:
    return int(hashlib.md5(prompt.encode()).hexdigest(), 16) % 10000

def gerar_mundo(prompt: str) -> dict:
    tema = detectar_tema(prompt)
    genero = classificar_genero(prompt)
    seed = gerar_seed(prompt)
    rng = random.Random(seed)

    # Herói
    heroi = {
        "nome": "Jogador",
        "classe": tema["heroi_classe"],
        "vida_max": rng.randint(90, 130),
        "mana_max": rng.randint(60, 100),
        "ataque": rng.randint(18, 30),
        "defesa": rng.randint(8, 18),
    }
    heroi["vida"] = heroi["vida_max"]
    heroi["mana"] = heroi["mana_max"]

    # Inimigo (matematicamente balanceado)
    fator = rng.uniform(0.9, 1.3)
    inimigo = {
        "nome": tema["inimigo_nome"],
        "vida_max": int(heroi["vida_max"] * fator),
        "ataque": int(heroi["ataque"] * rng.uniform(0.8, 1.1)),
        "defesa": int(heroi["defesa"] * rng.uniform(0.6, 1.0)),
    }
    inimigo["vida"] = inimigo["vida_max"]

    # O gênero classificado molda o modo de jogo (batalha vs. captura)
    modo = genero["modo"]
    if modo == "captura":
        inimigo["selvagem"] = True
        inimigo["capturavel"] = True
        log_inicial = f"🌿 Uma criatura selvagem surgiu: {inimigo['nome']}! Enfraqueça-a e capture-a."
    else:
        log_inicial = f"⚔️ {heroi['classe']} encontrou {inimigo['nome']}! A batalha começa!"

    return {
        "status": "UNIVERSO_PRONTO",
        "seed": seed,
        "skybox": tema["skybox"],
        "cor_tema": tema["cor"],
        "ambiente": tema["ambiente"],
        "prompt_original": prompt,
        "heroi": heroi,
        "inimigo": inimigo,
        "habilidades": tema["habilidades"],
        "genero": genero,
        "modo": modo,
        "colecao": [],
        "turno": "jogador",
        "log": [log_inicial],
    }

def processar_acao(payload: dict) -> dict:
    acao = payload.get("acao", "")
    estado = payload.get("estado", {})

    heroi = estado.get("heroi", {})
    inimigo = estado.get("inimigo", {})
    habilidades = estado.get("habilidades", [])
    log = estado.get("log", [])

    if estado.get("batalha_encerrada"):
        return {"status": "BATALHA_ENCERRADA", "estado": estado}

    resultado = ""

    if acao == "ATACAR":
        dano_bruto = heroi.get("ataque", 20)
        dano = max(1, dano_bruto - inimigo.get("defesa", 5) + random.randint(-5, 10))
        inimigo["vida"] = max(0, inimigo["vida"] - dano)
        resultado = f"🗡️ Você atacou {inimigo['nome']} causando {dano} de dano!"

    elif acao.startswith("HABILIDADE_"):
        idx = int(acao.split("_")[1])
        if 0 <= idx < len(habilidades):
            hab = habilidades[idx]
            custo = hab.get("custo_mana", 0)
            if heroi.get("mana", 0) < custo:
                resultado = f"❌ Mana insuficiente para {hab['nome']}! ({custo} necessário)"
            else:
                heroi["mana"] = max(0, heroi["mana"] - custo)
                dano = hab.get("dano", 0)
                if dano > 0:
                    dano_real = max(1, dano - inimigo.get("defesa", 0) // 2 + random.randint(-3, 8))
                    inimigo["vida"] = max(0, inimigo["vida"] - dano_real)
                    resultado = f"✨ {hab['nome']}: {hab['descricao']} — {dano_real} de dano!"
                elif dano < 0:
                    cura = abs(dano)
                    heroi["vida"] = min(heroi["vida_max"], heroi["vida"] + cura)
                    resultado = f"💚 {hab['nome']}: Recuperou {cura} pontos de vida!"
                else:
                    resultado = f"🛡️ {hab['nome']}: {hab['descricao']} ativado!"

    elif acao == "CAPTURAR":
        modo = estado.get("modo") or estado.get("genero", {}).get("modo")
        if modo != "captura" or not inimigo.get("capturavel"):
            resultado = "❌ Esta criatura não pode ser capturada."
        else:
            # chance cresce conforme a criatura é enfraquecida (math puro)
            vida_frac = inimigo["vida"] / max(1, inimigo.get("vida_max", 1))
            chance = min(0.95, max(0.05, (1 - vida_frac) * 0.9 + 0.05))
            if random.random() < chance:
                inimigo["capturado"] = True
                colecao = estado.get("colecao", [])
                colecao.append(inimigo["nome"])
                estado["colecao"] = colecao
                resultado = f"🎯 Captura bem-sucedida ({int(chance * 100)}%)! {inimigo['nome']} entrou na sua coleção!"
            else:
                resultado = f"💨 A criatura escapou ({int(chance * 100)}% de chance)! Enfraqueça-a mais antes de tentar."

    log.append(resultado)

    # Turno do inimigo: contra-ataca em ações ofensivas ou após captura falha
    acao_ofensiva = acao == "ATACAR" or acao.startswith("HABILIDADE_")
    captura_falhou = acao == "CAPTURAR" and inimigo.get("capturavel") and not inimigo.get("capturado")
    if inimigo["vida"] > 0 and not inimigo.get("capturado") and (acao_ofensiva or captura_falhou):
        dano_ini = max(1, inimigo.get("ataque", 15) - heroi.get("defesa", 5) + random.randint(-3, 8))
        heroi["vida"] = max(0, heroi["vida"] - dano_ini)
        log.append(f"👹 {inimigo['nome']} contra-ataca causando {dano_ini} de dano!")

    batalha_encerrada = False
    if inimigo.get("capturado"):
        batalha_encerrada = True
    elif inimigo["vida"] <= 0:
        log.append(f"🏆 VITÓRIA! {inimigo['nome']} foi derrotado!")
        batalha_encerrada = True
    elif heroi["vida"] <= 0:
        log.append("💀 DERROTA! Seu herói caiu em batalha...")
        batalha_encerrada = True

    estado["heroi"] = heroi
    estado["inimigo"] = inimigo
    estado["colecao"] = estado.get("colecao", [])
    estado["log"] = log[-8:]
    estado["turno"] = "jogador"
    estado["batalha_encerrada"] = batalha_encerrada

    return {"status": "ESTADO_ATUALIZADO", "estado": estado}

# --- Validação ---
class PlayerPayload(BaseModel):
    api_key: str
    prompt: str | None = None
    acao: str | None = None
    estado: dict | None = None

    @field_validator("api_key")
    @classmethod
    def chave_valida(cls, v):
        if v not in API_KEYS:
            raise ValueError("API key inválida")
        return v

class ConnectionManager:
    def __init__(self):
        self._conns: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, cid: str, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._conns[cid] = ws

    async def disconnect(self, cid: str):
        async with self._lock:
            self._conns.pop(cid, None)

    async def send(self, cid: str, msg: dict):
        ws = self._conns.get(cid)
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass

manager = ConnectionManager()

@app.websocket("/v1/nexus/stream")
async def nexus_stream(websocket: WebSocket):
    cid = str(id(websocket))
    await manager.connect(cid, websocket)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_TIMEOUT)
            except asyncio.TimeoutError:
                await manager.send(cid, {"status": "TIMEOUT", "message": "Sessão encerrada por inatividade."})
                break

            try:
                data = json.loads(raw)
                payload = PlayerPayload(**data)
            except Exception as e:
                await manager.send(cid, {"status": "ERRO_PAYLOAD", "message": str(e)})
                continue

            if is_rate_limited(payload.api_key):
                await manager.send(cid, {"status": "RATE_LIMITED", "message": "Limite de requisições atingido."})
                continue

            if payload.prompt:
                await manager.send(cid, {"status": "MOLDANDO_UNIVERSO", "message": "Gerando seu universo..."})
                await asyncio.sleep(0.1)
                mundo = gerar_mundo(payload.prompt)
                await manager.send(cid, mundo)

            elif payload.acao and payload.estado:
                resultado = processar_acao({"acao": payload.acao, "estado": payload.estado})
                await manager.send(cid, resultado)

    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(cid)