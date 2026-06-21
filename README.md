# Nexus: Unbound

A procedural infinite-world game engine that renders entire worlds from pure mathematics —
signed distance fields (SDF) evaluated via ray marching in a GLSL fragment shader — with zero
downloaded assets. World "identity" (terrain shape, palette, atmosphere, biome) is data, not
code: a single shader blends and re-parametrizes between distinct universes in real time on
the GPU.

<!-- TODO: add demo gif/video here -->

## Overview

Nexus pairs a stateless-by-design rendering core with a small but real multiplayer backend:

- **Backend** (`main.py`) — FastAPI + WebSocket server. Owns a semantic object engine (objects
  are resolved by logic tags, not 3D models), per-chunk world persistence in SQLite, and is the
  single source of truth for combat numbers and player identity.
- **Frontend** (`frontend/index.html`) — a self-contained Three.js page. There is no traditional
  mesh pipeline: the entire scene (terrain, structures, weapon viewmodel, other players) is
  ray-marched against analytic SDFs inside one fragment shader.

Everything needed to run the game ships in two files plus a SQLite database — no asset
pipeline, no build step, no bundler.

## Key Features

### SDF rendering, zero assets
The world is ray-marched against signed distance functions (80 steps, far-plane clipped).
Terrain height is one analytic function (6-octave fBm with ridge/crater shaping); object shapes
come from a second SDF family. Both are driven entirely by uniforms — `u_terrainScale`,
`u_peakHeight`, `u_ridgeAmount`, `u_craterAmount` for shape; `u_colorLow` / `u_colorHigh` /
`u_colorSlope` / `u_fogColor` / `u_sunColor` / `u_metalness` for atmosphere and material;
`u_objectType` and `u_biome` as the only *discrete* switches (shape family / texture algorithm
can't be interpolated continuously, everything else can). The four worlds shipped today —
Dark Fantasy, Sci-Fi, Post-Apocalyptic, Grassland — are just four parameter presets
(`WORLD_PRESETS` in `frontend/index.html`); generating those same fields from a text prompt
would be enough to add a new world without touching the shader.

World-to-world transitions interpolate parameters (`lerpParams`) and upload one uniform set per
frame, rather than evaluating two SDFs per pixel during the blend — `map()` never branches on
"which world."

Physics and rendering read the **same height data**: the terrain height is computed once in
JavaScript into a `Float32Array` heightmap, uploaded as a `DataTexture`, and the shader samples
that texture instead of recomputing the formula on the GPU — eliminating float32/float64
divergence by construction instead of trying to keep two implementations in sync.

### Photorealistic terrain pass
Surface shading (not shape) layers triplanar texture projection and an analytic-derivative
bump map on top of the existing terrain: `terrainDetail`/`triplanarWeights` blend a detail noise
across the three world-axis planes by normal weight, and `noised2` is the closed-form gradient
of the bicubic noise interpolation — no finite-difference resampling needed to get a usable
shading normal. The bump only perturbs lighting (diffuse/specular/fresnel/sky term), never the
geometric normal used for shadow ray offsets, so it can't create shadowing artifacts that don't
exist in the actual terrain shape. `u_metalness` (already varying per world) doubles as bump
strength and specular exponent, and a distance LOD (`FAR_DETAIL_DIST = 80`) drops to a flat,
single-sample lookup past that range.

### Lighting & post-processing
Ambient occlusion (`calcAO`, 5-sample technique) darkens only indirect light, never the direct
sun term (which already has soft shadows). A 3-pass manual post-processing pipeline — scene
render to an HDR linear render target, bloom extraction + blur at half resolution, then a
composite pass applying ACES tonemapping, vignette, contrast/saturation and gamma — runs without
any extra rendering libraries.

### Semantic objects (logic bridge)
Objects carry no 3D model — only semantic tags (`destructible`, `hostile`, `collectible`,
`solid`). `SemanticEngine.resolve()` on the backend decides the outcome of an interaction purely
from tags + action type (`SHOOT`, `INTERACT`, `COLLIDE`). The current per-cell procedural
structure (tower / monolith / ruin) covers all three: shooting a live, hostile+destructible
structure destroys it and awards points; touching it while alive deals contact damage; once
destroyed, it becomes lootable via `INTERACT`. A shared "destroyed cells" mask keeps the shader's
render and the JS collision code in agreement about what still exists in the world.

### Chunk persistence
`WorldStore` persists modifications per chunk coordinate (16-unit 2D grid) in SQLite, guarded by
an `asyncio.Lock`. Changing chunks triggers a `CHUNK_RECONCILE` message with whatever was
destroyed or collected there. A corrupted or missing database is recovered automatically on
startup — the bad file is renamed aside with a `.corrupted-<timestamp>` suffix instead of
crashing the server. A background task snapshots the database every 15 minutes (keeping the 10
most recent backups in `backups/`); `tools/backup_db.py` runs the same snapshot manually.

### Item Forge
A predefined catalog of four items (sword, spear, bow, shield) lives in `ITEM_CATALOG` on the
server — the **only** source of damage/range/speed/defense numbers. The full catalog is sent to
the client on connect; the client only maps item type to a render shape, never to a combat
value. An unarmed player replicates the pre-Forge damage/range exactly, so players who never
forge see no regression. Forged items persist per player in SQLite and are rehydrated on every
connection.

### Basic multiplayer
Players see each other move in real time (rendered as capsule SDFs, interpolated between
network updates), and one player's actions (destroying/collecting) are broadcast to everyone
else in the world. Reconnection is handled per-tab: a `player_id` can have multiple live
WebSocket connections (e.g. two browser tabs), and the connection manager tracks a *set* of
sockets per player rather than a single one, broadcasting every response to all of that
player's open tabs and only cleaning up state once the last one disconnects.

## Architecture

```
frontend/index.html   Three.js + GLSL ray marcher (production renderer, self-contained)
frontend/index_3d.html  Experimental prototype — see "Experimental" below
main.py                FastAPI app, WebSocket handler, SemanticEngine, WorldStore (SQLite)
tools/backup_db.py     Manual database backup (same routine the server runs periodically)
tools/mp_test_bot.py   Headless WebSocket bot that walks the world for multiplayer testing
```

The backend is the authority for combat numbers, player identity/presence, and persisted world
state. The frontend is the authority for rendering and local physics, sourced from the same
heightmap data it sends nowhere — physics never round-trips the network.

## Getting Started

```bash
pip install "fastapi[standard]" uvicorn websockets
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open port 8000 (in GitHub Codespaces, expose it from the **Ports** tab). FastAPI serves
`frontend/index.html` at `/` and the game WebSocket on the same port.

## Communication Protocol

Route: `/ws/{player_id}`. Clients send an `ActionPayload`:

| Field | Notes |
|---|---|
| `action_type` | `INTERACT` \| `COLLIDE` \| `SHOOT` \| `MOVE` \| `PING` \| `SET_STYLE` \| `FORGE` \| `EQUIP` |
| `target_tags` | list of semantic tag strings |
| `target_id` | object id (structures: `struct_<cx>_<cz>`; loot reuses the same id + `_loot`) |
| `position` | `[x, y, z]`, validated as 3 finite numbers — `NaN`/`Infinity`/wrong length are rejected |
| `world_style` | `0`–`3`, only on `SET_STYLE` |
| `item_type` | `ITEM_CATALOG` key (`espada`/`lanca`/`arco`/`escudo`), only on `FORGE` |
| `item_id` | id of an already-forged item, only on `EQUIP` |

The server replies with a `status` field — `CONNECTED`, `PLAYER_JOINED`/`MOVED`/`LEFT`,
`PLAYER_ACTION`, `DESTROYED`, `DAMAGED`, `COLLECTED`, `PLAYER_HIT`, `BLOCKED`, `INTERACTED`,
`NO_EFFECT`, `ALREADY_RESOLVED`, `CHUNK_RECONCILE`, `FORGED`, `EQUIPPED`, `STYLE_CHANGED`,
`PONG`, `RATE_LIMITED`, `TIMEOUT`, `ERROR`, or `SERVER_ERROR` — each either sent only to the
acting connection or broadcast to other players, depending on the event. See `CLAUDE.md` for the
full per-status routing table.

## Project Status

The core engine — SDF rendering, semantic objects, WebSocket protocol, parametrized shader,
four worlds with distinct identity — is complete. On top of it, four product phases have shipped
and been validated by hands-on testing (not just code review):

- ✅ **Phase 1 — Real raycast**: sphere-traced hit testing against terrain and objects.
- ✅ **Phase 2 — Persistent memory**: world state survives a server restart, across multiple chunks.
- ✅ **Phase 3 — Item Forge**: four forgeable items, server-authoritative combat stats, persists across reload and restart.
- ✅ **Phase 4 — Basic multiplayer**: real-time presence/movement/actions between players, validated with both manual testing and a battery of 17 automated WebSocket reconnection/robustness scenarios.

Shipped, pending final human approval before being marked complete:
- **Visual quality pass** — ambient occlusion, bloom + ACES tonemapping post-process, triplanar/analytic-derivative terrain detail, refreshed per-world palette.
- **Grassland** — a fourth world preset added as a visual-quality reference point (low rolling hills, grass texture algorithm, simple cloud layer).

## Experimental

`frontend/index_3d.html` is an early, standalone prototype exploring a **traditional Three.js
mesh-based renderer** (real geometry, instanced grass, displaced terrain mesh) as a possible
alternative rendering foundation to the SDF/ray-marching approach. It has no backend
connection, no multiplayer/inventory/forge, and does not modify or replace
`frontend/index.html` — the SDF renderer remains the production path. This file is exploratory
and not wired into the rest of the engine.

## Known Issues / Architecture Notes

- **Player identity** is a random ID persisted in the browser's `localStorage` — it survives a
  page reload but not a switch of device or browser.
- **Persistence is SQLite**, with local-only backups (`backups/`, not off-site/remote); Postgres
  is a candidate once concurrent player counts justify it.
- **Network payloads are JSON**, including per-frame position updates; binary struct packing is
  a planned optimization once player counts make the bandwidth matter — not worth the complexity
  at current scale.
- **Minor, low-priority race**: equipping an item from two browser tabs of the same player at
  nearly the same millisecond can leave the in-memory state and the database briefly disagreeing
  about which item is equipped, until the next action. No data loss, self-corrects.
- Two designs were evaluated and intentionally **not** implemented: runtime shader
  recompilation/JIT (recompiling GLSL on the fly risks frame stalls and AI-authored shaders
  failing to compile; the parametrized-uniform approach already covers this without the risk),
  and smooth-minimum blending between players (the right SDF technique for merging blobs, the
  wrong one for keeping player bodies distinct).
- Text-prompt-driven world/NPC/item generation via the Claude API is designed for but paused,
  pending API budget.

## Tech Stack

Python 3, FastAPI, WebSockets, Pydantic, SQLite via `aiosqlite` on the backend; Three.js (r128,
via CDN) and hand-written GLSL on the frontend. No bundler, no asset pipeline, no frontend
build step.
