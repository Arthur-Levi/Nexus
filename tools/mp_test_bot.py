"""
Bot de teste pra validar multiplayer SEM precisar de uma segunda pessoa.
NÃO é parte do jogo (Nexus) — é uma ferramenta de QA que fala o mesmo
protocolo WebSocket de um cliente real (mesma rota /ws/{player_id}, mesmo
formato de ActionPayload), então pro servidor é indistinguível de um
jogador de verdade.

Comportamento: entra no mundo e anda em círculo perto do spawn, mandando
MOVE na mesma cadência (~10Hz) que o cliente real usa — dá pra ver os
PLAYER_JOINED/PLAYER_MOVED chegando no console do navegador (Fase M1) e,
desde a Fase M2, ver a cápsula remota orbitando na tela.

A altura Y segue o terreno de verdade (terrainHeight, espelhado de
frontend/index.html — mesmos parâmetros do preset 0 "Fantasia Sombria",
o mundo padrão antes de qualquer SET_STYLE) em vez de um valor fixo: o
terreno NÃO é plano (peakHeight=30, bem acidentado), então uma altura
constante deixava o bot enterrado embaixo do chão em quase todo o
círculo — só coincidia bem exatamente na origem. Ver CLAUDE.md (Fase M2)
pra esse achado.

Uso:
    python3 tools/mp_test_bot.py [--url ws://localhost:8000] [--id bot_01]
                                  [--radius 15] [--speed 0.3]

Ctrl+C pra parar (fecha a conexão de propósito — os outros jogadores
devem ver um PLAYER_LEFT).
"""
import argparse
import asyncio
import json
import math
import time

import websockets

# ════════════════════════════════════════════════════════════
# Espelha terrainHeight (GLSL) / terrainHeightJS (frontend/index.html)
# bit-a-bit — mesmas constantes, mesma ordem de operações. Só pros
# parâmetros do preset 0 (não vale a pena receber SET_STYLE no bot só
# pra isso; é uma ferramenta de QA, não um jogador real escolhendo mundo).
# ════════════════════════════════════════════════════════════
TERRAIN_SCALE, PEAK_HEIGHT, RIDGE_AMOUNT, CRATER_AMOUNT = 0.016, 30.0, 0.9, 0.0
EYE_HEIGHT = 2.5  # physics.eyeHeight no cliente


def _hash2(px, py):
    s = math.sin(px * 127.1 + py * 311.7) * 43758.5453
    return s - math.floor(s)


def _noise2(x, y):
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    a, b = _hash2(ix, iy), _hash2(ix + 1, iy)
    c, d = _hash2(ix, iy + 1), _hash2(ix + 1, iy + 1)
    return (a + (b - a) * fx) + (c - a) * fy + (a - b - c + d) * fx * fy


def _fbm(x, y):
    v, a = 0.0, 0.5
    for _ in range(6):
        v += a * _noise2(x, y)
        x, y = 1.6 * x - 1.2 * y, 1.2 * x + 1.6 * y
        a *= 0.5
    return v


def _ridged_fbm(x, y):
    v, a = 0.0, 0.5
    for _ in range(6):
        n = _noise2(x, y)
        n = 1 - abs(n * 2 - 1)
        n = n * n
        v += a * n
        x, y = 1.6 * x - 1.2 * y, 1.2 * x + 1.6 * y
        a *= 0.5
    return v


def _smoothstep(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


def terrain_height(x: float, z: float) -> float:
    sx, sz = x * TERRAIN_SCALE, z * TERRAIN_SCALE
    base = _fbm(sx, sz) * 14.0
    mask = _smoothstep(0.25, 0.75, _fbm(sx * 0.45, sz * 0.45))
    peak_shape = _fbm(sx * 0.9, sz * 0.9) * (1 - RIDGE_AMOUNT) + _ridged_fbm(sx * 0.9, sz * 0.9) * RIDGE_AMOUNT
    peaks = peak_shape * PEAK_HEIGHT * mask
    craters = -_ridged_fbm(sx * 3.0, sz * 3.0) * CRATER_AMOUNT
    detail = _fbm(x * TERRAIN_SCALE * 11.0, z * TERRAIN_SCALE * 11.0) * 1.6
    return base + peaks + craters + detail - 7.0


async def run_bot(url: str, player_id: str, radius: float, angular_speed: float):
    ws_url = f"{url}/ws/{player_id}"
    print(f"[bot] conectando em {ws_url} ...")
    async with websockets.connect(ws_url) as ws:
        print(f"[bot] conectado como '{player_id}'. Ctrl+C pra sair.")
        start = time.monotonic()

        async def mover():
            while True:
                t = time.monotonic() - start
                angle = t * angular_speed
                x = math.cos(angle) * radius
                z = math.sin(angle) * radius
                y = terrain_height(x, z) + EYE_HEIGHT  # mesma convenção do player.pos real (altura do OLHO)
                await ws.send(json.dumps({
                    "player_id": player_id,
                    "action_type": "MOVE",
                    "position": [x, y, z],
                }))
                await asyncio.sleep(0.1)  # ~10Hz, mesma cadência do cliente real

        async def escutar():
            async for raw in ws:
                msg = json.loads(raw)
                status = msg.get("status")
                # Só loga o que é relevante pra não poluir — mensagens de
                # MOVE/PONG de outros jogadores não interessam pro bot.
                if status in ("CONNECTED", "PLAYER_JOINED", "PLAYER_LEFT", "ERROR", "SERVER_ERROR"):
                    print(f"[bot] <- {msg}")

        await asyncio.gather(mover(), escutar())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="ws://localhost:8000", help="Base da URL do WebSocket (sem /ws/...)")
    ap.add_argument("--id", default="bot_01", help="player_id do bot")
    ap.add_argument("--radius", type=float, default=15.0, help="Raio do círculo (unidades do mundo)")
    ap.add_argument("--speed", type=float, default=0.3, help="Velocidade angular (rad/s)")
    args = ap.parse_args()

    try:
        asyncio.run(run_bot(args.url, args.id, args.radius, args.speed))
    except KeyboardInterrupt:
        print("\n[bot] encerrado.")


if __name__ == "__main__":
    main()
