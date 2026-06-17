# Nexus: Unbound

Motor de jogo procedural que gera mundos infinitos via matemática pura (SDF + Ray Marching),
sem download de assets. Mistura estéticas de universos em tempo real na GPU.

## Arquitetura

- **Backend** (`main.py`): FastAPI + WebSocket. Motor semântico de objetos (tags lógicas),
  persistência de mundo em chunks (malha 2D de 16 unidades), resolução de interações em tempo real.
- **Frontend** (`frontend/index.html`): Three.js autocontido. Todo o render acontece num
  fragment shader GLSL via Ray Marching. Terreno procedural (fBm 6 oitavas), texturização PBR
  por inclinação/altitude, névoa atmosférica, 3 mundos com identidade própria (Fantasia
  Sombria, Sci-Fi, Pós-Apocalíptico) com transição glitch.

## Como rodar

```bash
pip install "fastapi[standard]" uvicorn websockets
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Acesse a porta 8000 (no Codespaces, exponha pela aba Ports). O FastAPI serve o `index.html`
e o WebSocket na mesma porta.

## Conceitos-chave

### Pilar 1 — SDF Rendering
Mundo desenhado por funções de distância assinada no fragment shader. Zero polígonos, zero assets.
Terreno = função analítica `terrainHeight(xz, scale, peakHeight, ridgeAmount, craterAmount)`.
Limitado a 80 steps com far clipping.

**Universos infinitos via parâmetros, não `if(style)`.** A função de altura (e a de cor,
`terrainColor`) não conhecem "Fantasia"/"Sci-Fi"/"Pós-Apocalíptico" — só leem uniforms:
`u_terrainScale`, `u_peakHeight`, `u_ridgeAmount`, `u_craterAmount` (forma do terreno),
`u_colorLow`/`u_colorHigh`/`u_colorSlope` (cor por altitude/inclinação), `u_fogColor`,
`u_sunColor`, `u_metalness` (atmosfera/material) e `u_objectType` (qual família de forma o
`objectSDF` desenha — só esse é discreto, já que não dá pra misturar continuamente uma forma
geométrica). Os 3 mundos atuais são só 3 objetos JS com esses valores (`WORLD_PRESETS` em
`frontend/index.html`) — uma IA gerando esses mesmos campos a partir de um prompt já é
suficiente pra criar um mundo novo, sem tocar no shader.

Física e render leem os MESMOS parâmetros: `terrainHeightJS` em JS espelha `terrainHeight`
do GLSL constante a constante (mesma ordem de operações, mesmas frequências) — é o mesmo
cuidado que resolveu o bug de sincronia (ver "Histórico" abaixo), agora generalizado pra
qualquer combinação de parâmetros, não só pros 3 mundos fixos.

Transição entre mundos: o shader nunca mistura duas SDFs — quem anima é o JS, interpolando
os PARÂMETROS (`lerpParams`) e subindo um único conjunto de uniforms por frame
(`syncWorldUniforms`). Isso elimina o custo de avaliar dois mundos por pixel durante a fusão
(que existia na versão anterior) e também faz `map(p)` não precisar mais de branch nenhum.
`u_objectType` (discreto) troca exatamente na metade da transição, junto do pico do glitch.

### Pilar 2 — Semantic Objects (Logic Bridge)
Objetos não têm modelo 3D, só tags semânticas (`destruivel`, `hostil`, `coletavel`, `solido`).
O `SemanticEngine.resolve()` no backend decide a interação por tags + tipo de ação
(`SHOOT`, `INTERACT`, `COLLIDE`).

Hoje a única estrutura procedural por célula (torre/monólito/ruína) cobre as 3 ações:
- **Viva**: `SHOOT` com `destruivel+hostil` destrói (+10 pontos, +25 bônus) e some do mundo
  (render e colisão); encostar nela enquanto viva dispara `COLLIDE` com `hostil+solido`
  (-15 hp, cooldown de 0.8s no cliente pra não ser instakill em contato contínuo).
- **Destruída**: vira saqueável — `INTERACT` com `coletavel+sucata` (tecla E, raio curto)
  dá um item de inventário. Usa um `target_id` sufixado (`..._loot`) pra não colidir com o
  guard de `ALREADY_RESOLVED` do objeto original.

A "morte visual" do objeto é feita por uma máscara de células destruídas (`destroyedCells`
em JS, espelhada num uniform `u_destroyedCells[]`/`u_destroyedCount` no shader) — tanto o
`map()` do GLSL quanto `applyObjectCollisions()`/`raycastStructure()` em JS leem a mesma
lista, então um objeto destruído desaparece do render E do mundo físico ao mesmo tempo.

### Pilar 3 — Chunk Persistence
`WorldStore` guarda modificações por coordenada de chunk (protegido por asyncio.Lock).
Ao mudar de chunk, o servidor envia `CHUNK_RECONCILE` com o que foi destruído/coletado.

## Comunicação WebSocket

Rota: `/ws/{player_id}`. Mensagens do cliente usam o schema `ActionPayload`:
- `action_type`: INTERACT | COLLIDE | SHOOT | MOVE | PING | SET_STYLE
- `target_tags`: lista de strings
- `position`: [x, y, z]
- `world_style`: 0 | 1 | 2

## Roadmap

- **Fase 1** (concluída): SDF engine, semantic objects, WebSocket
- **Fase 2** (concluída): 3 estilos visuais + glitch, chunks persistentes, terreno realista PBR
- **Fase A** (concluída): 3 mundos com identidade própria (Fantasia Sombria, Sci-Fi, Pós-Apocalíptico);
  física sincronizada com o render (heightmap JS como fonte única, ver "Histórico" abaixo);
  raycast real contra a estrutura (`SHOOT`/`COLLIDE`/`INTERACT` ligados ao mundo visível, não a
  ids arbitrários); objetos destruídos somem do render e da colisão.
- **Fase B** (concluída): shader parametrizado — `terrainHeight`/`terrainColor` sem `if(style)`,
  controlados só por uniforms (ver Pilar 1); os 3 mundos viraram presets de dados
  (`WORLD_PRESETS`); transição entre mundos interpola os parâmetros em vez de misturar SDFs.
- **Próximo**: a IA (Claude API no backend) gera um `WORLD_PRESETS`-like a partir de um prompt
  do jogador; Forge de Itens por IA; banco Postgres na escala.

## Histórico — bug de sincronia física/render (RESOLVIDO)

O terreno era desenhado no fragment shader GLSL (`terrainHeight`, float32 na GPU) e a física
usava uma réplica em JavaScript (`terrainHeightJS`, float64 na CPU); as duas precisavam
produzir valores idênticos pra mesma coordenada e divergiam por precisão de ponto flutuante
e por uma matriz de rotação do fBm com sinal invertido entre GLSL e JS.

Resolvido pela opção **A** do roadmap original (JS como fonte única): o JS gera um
`Float32Array` (heightmap) em `buildHeightmap()`, sobe como `DataTexture` (`u_heightmap`),
e o shader lê esses mesmos valores via `texture2D` em vez de recalcular `terrainHeight` —
zero divergência enquanto a textura cobre a área (±64u ao redor do jogador, rebuild a cada
32u percorridos). A colisão com objetos (torres/monólitos/ruínas) também foi corrigida: o
wrap de repetição da grade no GLSL (`mod(q.xz,48.0)-24.0`) agora centra o objeto exatamente
onde `applyObjectCollisions()` em JS já esperava (centro da célula de 48u), eliminando o
desalinhamento de meia-célula que deixava o jogador atravessar as estruturas.

## Convenções

- Sem placeholders no código — tudo pronto para rodar.
- Tratamento de erro robusto no loop WebSocket (nunca derrubar o servidor).
- Geração procedural matemática preferível a assets externos.