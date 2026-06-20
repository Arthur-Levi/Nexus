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
`map()` do GLSL quanto `applyObjectCollisions()`/`raycastScene()` em JS leem a mesma
lista, então um objeto destruído desaparece do render E do mundo físico ao mesmo tempo.

### Pilar 3 — Chunk Persistence
`WorldStore` guarda modificações por coordenada de chunk (protegido por asyncio.Lock).
Ao mudar de chunk, o servidor envia `CHUNK_RECONCILE` com o que foi destruído/coletado.

### Pilar 4 — Forge de Itens
Catálogo predefinido (`ITEM_CATALOG` em `main.py`) com 4 itens (espada, lança, arco, escudo) —
ainda sem IA generativa. O servidor é a ÚNICA fonte dos números de combate (damage/range/speed/
defense): manda o catálogo inteiro no `CONNECTED`, e o cliente nunca hardcoda esses valores —
só guarda localmente o mapeamento tipo→forma geométrica (`ITEM_TYPES`/`u_weaponType`), que é
dado de render, não de combate (mesmo cuidado de fonte única que resolveu o bug de física/render,
ver "Histórico" abaixo). `DEFAULT_WEAPON` (jogador desarmado) replica o dano/alcance de antes da
Forge existir — zero regressão pra quem nunca forjar. `STRUCTURE_MAX_HP` é fixo e único, o que
torna o dano de cada arma um efeito real (espada/lança matam em 1 tiro, arco em 2, escudo em 4).
Itens persistem por jogador em SQLite (`player_items`), hidratados a cada conexão.

## Comunicação WebSocket

Rota: `/ws/{player_id}`. Mensagens do cliente usam o schema `ActionPayload`:
- `action_type`: INTERACT | COLLIDE | SHOOT | MOVE | PING | SET_STYLE | FORGE | EQUIP
- `target_tags`: lista de strings
- `target_id`: id do objeto (estruturas usam `struct_<cx>_<cz>`, saque usa o mesmo id + `_loot`)
- `position`: [x, y, z]
- `world_style`: 0 | 1 | 2 — só em `SET_STYLE`
- `item_type`: chave do `ITEM_CATALOG` (`espada`/`lanca`/`arco`/`escudo`) — só em `FORGE`
- `item_id`: id de um item já forjado pelo jogador — só em `EQUIP`

## Roadmap rumo ao MVP

> Cada fase tem uma definição objetiva de "pronto e excelente". Uma fase só é considerada
> concluída quando TODOS os critérios são validados RODANDO o jogo, com teste humano (não só
> leitura de código). O Claude Code propõe plano, implementa, e prova cada critério. O humano
> (Arthur) testa e aprova. **Sem aprovação humana, nenhuma fase avança.** Qualidade > velocidade,
> sempre.
>
> (O motor em si — SDF rendering, semantic objects, WebSocket, 3 mundos com identidade própria,
> shader parametrizado por uniforms — já está concluído; ver entradas de 2026-06-12 a 2026-06-19
> no "Histórico de decisões técnicas" abaixo. As fases a seguir são sobre o PRODUTO em cima desse motor.)

### Núcleo de produto (os 4 passos)

**Fase 1 — Raycast real** ✅ CONCLUÍDA
Excelência: mirar acerta o objeto real; mirar no vazio não acerta; alcance
respeitado; crosshair indica alvo válido; sem código morto. (9/9 validados)

**Fase 2 — Memória viva** ✅ CONCLUÍDA
Excelência: mundo idêntico após reinício do servidor; múltiplos chunks persistem;
estados ricos por objeto.

**Fase 3 — Forge de Itens** — implementada, aguardando validação humana
Excelência: criar item de cada tipo; item aparece no personagem; dano/alcance
muda por item; persiste a reload e reinício; sem regressão das fases anteriores.

**Fase 4 — Multiplayer básico** — próxima (não iniciar antes da Fase 3 ser aprovada)
Excelência: dois jogadores no mesmo mundo se veem mover em tempo real; ações de
um (destruir objeto) aparecem para o outro; estado sincronizado sem travar;
reconexão funciona; sem regressão.

### Antes de considerar "lançável" (pós-4-passos)

**Estabilidade**
- Roda 30+ minutos sem crash, vazamento de memória ou queda de FPS progressiva
- Reconexão automática robusta em queda de rede
- Tratamento de erro em todo caminho crítico (nunca derruba o servidor)

**Performance**
- 60 FPS estável em hardware modesto (resolução adaptativa funcionando)
- Funciona em celular e tablet (detecção e ajuste por dispositivo)
- Tempo de carregamento inicial aceitável

**Qualidade visual** (refinamento, depois dos 4 passos)
- Detalhe de superfície no SDF (bump procedural, multi-escala)
- Iluminação rica (ambient occlusion no ray march)
- Transições entre mundos polidas

**Áudio procedural** (refinamento, depois dos 4 passos)
- Som gerado pela mesma semente do mundo, sem arquivos WAV/MP3
- Passos, impactos e ambiente sintetizados por evento geométrico
  (ex: colisão do pé com o SDF do solo gera o som conforme o material)
- Síntese por modelagem física (waveguide) é a técnica de referência
- Realista para SOM AMBIENTE e efeitos. NOTA HONESTA: voz de NPC por
  síntese de formante soa robótica hoje — voz humana convincente exige
  TTS neural (modelo pesado ou API), não cabe em síntese procedural leve
- Encaixa na arquitetura "semente única gera geometria + cor + som"

**Experiência**
- Onboarding: jogador novo entende os controles sem explicação externa
- Feedback claro de cada ação (visual + sonoro quando houver som)
- UI consistente e legível

**Persistência e escala**
- SQLite → avaliar Postgres quando houver muitos jogadores simultâneos
- Backup do estado do mundo
- Identidade de jogador (hoje é um id aleatório persistido no localStorage do
  navegador — sobrevive a reload, não sobrevive a troca de dispositivo/navegador)

### Dependente de pagamento da API (pausado)
- Universos infinitos por texto livre (Claude API gera parâmetros do mundo)
- NPCs com diálogo gerado, missões dinâmicas, narrativa viva
- Forge de Itens por descrição livre

### NÃO é engenharia (não confundir com tarefa de código)
- Tração de usuários, marketing, streamers
- Marketplace com economia real (requer avaliação jurídica antes — CVM)
- "P2P distribuindo render entre GPUs" — não construível como descrito;
  o SDF já renderiza local em cada cliente (objetivo já atingido)
- Avaliação de mercado / metas de milhões de usuários (consequência, não tarefa)

## Histórico de decisões técnicas

> Atualizado pelo Claude Code ao fim de cada sessão de trabalho relevante.
> Formato: data, o que foi feito, o que funcionou, o que não funcionou e por quê,
> decisões de arquitetura tomadas. Isso evita repetir erros e dá contexto a
> qualquer sessão futura. Peça ao fim de cada sessão: "atualize a seção 'Histórico
> de decisões técnicas' do CLAUDE.md com o que fizemos nesta sessão."

### Log

**2026-06-12 a 2026-06-15 — Motor core**
- Feito: SDF rendering via ray marching no fragment shader, terreno procedural fBm,
  Three.js autocontido (Sprint 1).

**2026-06-16 — Física GPU/JS + Fase A**
- Feito: gravidade e colisão de terreno na física do player; 3 mundos com identidade
  própria (Fantasia Sombria, Sci-Fi, Pós-Apocalíptico).
- Não funcionou: `terrainHeight` do shader (GPU, float32) e a réplica `terrainHeightJS`
  (CPU, float64) divergiam — player atravessava o chão/estruturas em alguns pontos.
- Decisão de arquitetura: resolvido no mesmo dia adotando JS como FONTE ÚNICA — gera um
  heightmap (`Float32Array`) e sobe como `DataTexture` pro shader ler via `texture2D` em
  vez de recalcular a fórmula. Zero divergência por construção (ver "Histórico — bug de
  sincronia física/render" abaixo para o detalhe completo).

**2026-06-17 — Shader parametrizado**
- Feito: `terrainHeight`/`terrainColor` deixam de receber "qual estilo" e passam a só ler
  uniforms (`u_terrainScale`, `u_peakHeight`, `u_ridgeAmount`, `u_craterAmount`, cores) — os
  3 mundos viraram presets de dados (`WORLD_PRESETS`).
- Decisão: isso é a base para "universos infinitos" futuros via IA — gerar esses mesmos
  campos a partir de um prompt já basta, sem tocar no shader.

**2026-06-18 — Raycast real + robustez do heightmap**
- Feito: `raycastScene` (sphere tracing real contra chão+objeto) substitui o teste binário
  de cilindro — corrige tiro atravessando morros. Crosshair e `shoot()` passam a usar o
  mesmo resultado cacheado por frame.
- Não funcionou inicialmente: rebuild do heightmap bloqueava ~180ms a thread principal a
  cada 32u andadas, derrubando o WebSocket sob o túnel do Codespaces.
- Correção: rebuild incremental (poucas linhas por frame, ~32 frames), nunca expõe
  heightmap parcial. Pior bloqueio cai de 180ms para 8.1ms — confirmado estável pelo
  usuário testando no navegador real via Codespaces.

**2026-06-19 — Robustez geral + Forge de Itens**
- Feito: corrige URL do WebSocket em nomes de Codespace com múltiplos hifens; overflow do
  array de células destruídas (>64 estruturas mortas); leak no rate limiter (entrada por
  `player_id` nunca era limpa no desconectar). Corrida de pontuação corrigida com check+write
  atômico em `WorldStore.try_claim`/`apply_damage` (dois jogadores destruindo o mesmo objeto
  quase ao mesmo tempo não pontuam os dois). Teclas de movimento presas ao perder foco da
  janela corrigidas limpando o registro de teclas em `blur`/`visibilitychange`.
- Feito: Forge de Itens — catálogo predefinido de 4 itens no servidor (única fonte de
  dano/alcance/velocidade/defesa), `FORGE`/`EQUIP` no protocolo, viewmodel da arma equipada
  como SDF própria no shader (ver Pilar 4 acima). Ainda não validado por teste humano.

**2026-06-20 — Conexões multi-aba**
- Não funcionou: `ConnectionManager` guardava só UMA WebSocket por `player_id`. Como o
  `player_id` é persistido no `localStorage`, abrir uma segunda aba do jogo sobrescrevia o
  registro da primeira — toda resposta do servidor (FORGE/SHOOT/dano) passava a ir só pra
  aba nova; a aba antiga continuava agindo (mutava o `PlayerState` compartilhado) mas nunca
  via nenhuma resposta, parecendo travada silenciosamente. Reproduzido com um script de teste
  de WebSocket antes da correção.
- Correção: `ConnectionManager` guarda um SET de conexões por `player_id` e faz broadcast
  de toda resposta pra todas as abas vivas desse jogador; limpeza (`disconnect`) e rate-limit
  só liberam quando a última conexão daquele pid cai.
- Decisão de arquitetura confirmada: física e render sempre leem a MESMA fonte de dados
  (nunca duas implementações independentes da mesma matemática) continua sendo o princípio
  mais importante do projeto — e se generalizou pro lado da rede também (números de combate
  e, agora, identidade de conexão só existem numa fonte única no servidor).

**2026-06-20 — Viewmodel da arma invisível + animação de ataque**
- Não funcionou: a arma equipada renderizava certinho — confirmado matematicamente replicando
  `map()` em Python e fazendo sphere tracing contra a posição real da viewmodel (sem depender
  do navegador, que num WebGL via SwiftShader neste sandbox é lento demais pra screenshot/
  evaluate confiáveis) — mas ficava no canto inferior-direito da tela (`camRight*0.68`,
  comentário antigo dizia isso explicitamente). Esse é exatamente o canto onde `#rightPanels`
  (painel de Fusão de Universos + a própria Forge usada pra equipar!) sempre desenha por cima
  do canvas, fixo (`position:fixed;bottom:16px;right:16px;width:236px`). Em qualquer janela
  mais estreita que ~16:9 (comum em aba de navegador do Codespaces, não fullscreen), a área de
  projeção da arma cai atrás do painel — confirmado calculando a sobreposição em pixels para
  vários tamanhos de viewport (800×600 até 1366×768).
- Correção: moveu o anchor da viewmodel pro centro-inferior (`camRight*0.18` em vez de `0.68`,
  mesmo `camFwd`/`camUp`) — sem HUD em nenhum tamanho de janela testado, com margem de pelo
  menos ~80px mesmo no caso mais estreito (800×600). `#inv` (canto inferior-esquerdo) também
  nunca sobrepõe, é pequeno e fica mais abaixo na tela.
- Adicionado: animaçãozinha de ataque — um jab curto (~0.22s, seno) pra frente/cima no
  `wCenter`, disparado por um novo uniform `u_attackTime` (setado em `shoot()` junto com
  `lastShotTime`, mesmo instante). Mesma SDF pros 4 tipos de arma, sem branch — só desloca
  onde ela fica, mantendo o princípio do projeto de parametrizar em vez de bifurcar por tipo.
- Lição de tooling: screenshot/evaluate via Playwright neste sandbox trava sob WebGL real
  (SwiftShader, software rendering do raymarcher de 80 steps em tela cheia satura a CPU e a
  thread principal nunca libera tempo pro CDP responder) — pra validar SDF/posicionamento,
  replicar a matemática em Python puro (sem GPU) foi muito mais rápido e confiável que tentar
  abrir o jogo de verdade num browser headless. Validar compilação do shader isoladamente
  (só `gl.compileShader` num contexto WebGL bare, sem Three.js/jogo) também evita o mesmo travamento.

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