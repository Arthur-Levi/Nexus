# Nexus: Unbound

Motor de jogo procedural que gera mundos infinitos via matemática pura (SDF + Ray Marching),
sem download de assets. Mistura estéticas de universos em tempo real na GPU.

## Arquitetura

- **Backend** (`main.py`): FastAPI + WebSocket. Motor semântico de objetos (tags lógicas),
  persistência de mundo em chunks (malha 2D de 16 unidades), resolução de interações em tempo real.
- **Frontend** (`frontend/index.html`): Three.js autocontido. Todo o render acontece num
  fragment shader GLSL via Ray Marching. Terreno procedural (fBm 6 oitavas), texturização PBR
  por inclinação/altitude, névoa atmosférica, 3 estilos visuais (Abstrato, Voxel, Cyberpunk)
  com transição glitch.

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
Terreno = função analítica `terrainHeight(xz, style)`. Limitado a 96 steps com far clipping.

### Pilar 2 — Semantic Objects (Logic Bridge)
Objetos não têm modelo 3D, só tags semânticas (`destruivel`, `hostil`, `coletavel`, `solido`).
O `SemanticEngine.resolve()` no backend decide a interação por tags + tipo de ação
(`SHOOT`, `INTERACT`, `COLLIDE`).

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
- **Fase A** (em andamento): 3 mundos com identidade própria (Fantasia Sombria, Sci-Fi, Pós-Apocalíptico)
- **Próximo**: raycast real contra o SDF, Forge de Itens por IA, banco Postgres na escala

## BUG ATIVO — prioridade máxima (debugar rodando)

A física do player está dessincronizada do render. Sintomas:
- Player anda ABAIXO do chão em alguns pontos
- Player flutua ACIMA do chão em outros
- Player atravessa as estruturas (torres/monólitos/ruínas) — sem colisão com objetos

### Causa raiz
O terreno é desenhado no fragment shader GLSL (`terrainHeight`, em float32 na GPU)
e a física de colisão usa uma RÉPLICA em JavaScript (`terrainHeightJS`, float64 na CPU).
As duas precisam produzir valores idênticos para a mesma coordenada, mas divergem por:
1. Precisão de ponto flutuante (GPU float32 vs JS float64) — mitigado com `Math.fround` no hash2, mas ainda imperfeito
2. `noise2` JS foi corrigido para bilinear idêntico ao `mix()` aninhado do GLSL
3. Colisão com objetos (objectSDF) não existe em JS — por isso atravessa torres

### Como debugar (precisa rodar de verdade)
1. Rodar: `uvicorn main:app --host 0.0.0.0 --port 8000` (SEM --reload, ou com --reload-exclude "*.db")
2. Abrir numa aba REAL do navegador (não no preview do VS Code — preview não tem WebGL)
3. Comparar visualmente: adicionar um marcador de debug que mostre groundY (JS) vs altura real do terreno na tela
4. Ajustar terrainHeightJS até casar com o shader pixel a pixel

### Decisão arquitetural a considerar
Manter duas implementações da mesma matemática é frágil. Opções:
- **A**: Fazer o JS ser a fonte única — gerar o heightmap em JS e passar como textura/uniform pro shader ler (elimina a divergência de raiz)
- **B**: Reduzir a complexidade do terreno (menos oitavas) para a réplica ser mais fácil de manter
- **C**: Aceitar física aproximada + um "snap" suave que puxa o player pro chão

Recomendação: avaliar a opção A — é a mais robusta a longo prazo.

## Convenções

- Sem placeholders no código — tudo pronto para rodar.
- Tratamento de erro robusto no loop WebSocket (nunca derrubar o servidor).
- Geração procedural matemática preferível a assets externos.