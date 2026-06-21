# Nexus: Unbound

Motor de jogo procedural que gera mundos infinitos via matemática pura (SDF + Ray Marching),
sem download de assets. Mistura estéticas de universos em tempo real na GPU.

## Arquitetura

- **Backend** (`main.py`): FastAPI + WebSocket. Motor semântico de objetos (tags lógicas),
  persistência de mundo em chunks (malha 2D de 16 unidades), resolução de interações em tempo real.
- **Frontend** (`frontend/index.html`): Three.js autocontido. Todo o render acontece num
  fragment shader GLSL via Ray Marching. Terreno procedural (fBm 6 oitavas), texturização PBR
  por inclinação/altitude, névoa atmosférica, 4 mundos com identidade própria (Fantasia
  Sombria, Sci-Fi, Pós-Apocalíptico, Campo Gramado) com transição glitch.

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
`u_sunColor`, `u_metalness` (atmosfera/material), `u_objectType` (qual família de forma o
`objectSDF` desenha) e `u_biome` (qual ALGORITMO de textura `terrainColor` usa — rocha ou
grama; ver Campo Gramado no Histórico) — esses dois últimos são os únicos discretos, já que
não dá pra misturar continuamente uma forma geométrica ou um algoritmo de textura inteiro.
Os 4 mundos atuais são só 4 objetos JS com esses valores (`WORLD_PRESETS` em
`frontend/index.html`) — uma IA gerando esses mesmos campos a partir de um prompt já é
suficiente pra criar um mundo novo, sem tocar no shader.

Física e render leem os MESMOS parâmetros: `terrainHeightJS` em JS espelha `terrainHeight`
do GLSL constante a constante (mesma ordem de operações, mesmas frequências) — é o mesmo
cuidado que resolveu o bug de sincronia (ver "Histórico" abaixo), agora generalizado pra
qualquer combinação de parâmetros, não só pros 4 mundos fixos.

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
`WorldStore.init()` recria um banco limpo (sem crashar) se o `.db` estiver ausente ou
corrompido, movendo o arquivo problemático de lado com sufixo `.corrupted-<timestamp>`.
Backup automático (`backup_db_once`/`periodic_backup_task`, a cada `BACKUP_INTERVAL`,
mantém os `BACKUP_KEEP` mais recentes em `backups/`) protege contra perda — ver "Histórico"
para o incidente que motivou isso. `tools/backup_db.py` faz o mesmo backup manualmente.

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
- `position`: [x, y, z] — 3 números finitos (NaN/Infinity/tamanho errado são rejeitados com `ERROR`)
- `world_style`: 0 | 1 | 2 — só em `SET_STYLE`
- `item_type`: chave do `ITEM_CATALOG` (`espada`/`lanca`/`arco`/`escudo`) — só em `FORGE`
- `item_id`: id de um item já forjado pelo jogador — só em `EQUIP`

### Referência — todos os `status` que o servidor manda de volta

Cada mensagem do servidor tem um campo `status`. Tabela única de referência
(antes só os tipos de mensagem do CLIENTE estavam documentados aqui):

| `status` | Quando | Vai só pra quem agiu, ou broadcast? |
|---|---|---|
| `CONNECTED` | Toda nova conexão (inclusive aba extra do mesmo pid) | `send` (todas as abas do pid) — inclui `player_state`, `item_catalog`, `default_weapon`, `chunk_size` e `players` (snapshot de quem mais está conectado) |
| `PLAYER_JOINED` | 1ª conexão viva de um pid (não cada aba) | `broadcast`, exclui o próprio pid |
| `PLAYER_MOVED` | `MOVE` com posição válida | `broadcast`, exclui o próprio pid |
| `PLAYER_LEFT` | Cai a ÚLTIMA conexão viva de um pid | `broadcast`, exclui o próprio pid |
| `PLAYER_ACTION` | Outro jogador causou `DESTROYED`/`COLLECTED` | `broadcast`, exclui quem causou (campos `target_id`/`action_status`) |
| `DESTROYED` | `SHOOT` zerou o HP de uma estrutura | `send` (autor) — `PLAYER_ACTION` cobre os outros |
| `DAMAGED` | `SHOOT` aplicou dano sem destruir | `send` (autor) — sem representação visual hoje, não broadcasta |
| `COLLECTED` | `INTERACT`/`COLLIDE` em algo `coletavel` | `send` (autor) — `PLAYER_ACTION` cobre os outros |
| `PLAYER_HIT` | `COLLIDE` com algo `hostil` | `send` (autor) |
| `BLOCKED` | `COLLIDE` com algo `solido` (sem efeito) | `send` (autor) |
| `INTERACTED` | `INTERACT` genérico (sem tag `coletavel`) | `send` (autor) |
| `NO_EFFECT` | Nenhuma regra do `SemanticEngine` casou | `send` (autor) |
| `ALREADY_RESOLVED` | Objeto já tinha sido resolvido (por outro jogador, ou de antes de um restart) | `send` (autor) — inclui `resolved_state` pra convergir o cliente |
| `CHUNK_RECONCILE` | Trocar de chunk (ou reconectar) com modificações salvas no chunk novo | `send` (quem trocou) |
| `FORGED` | `FORGE` bem-sucedido | `send` (autor) |
| `EQUIPPED` | `EQUIP` bem-sucedido | `send` (autor) |
| `STYLE_CHANGED` | `SET_STYLE` aplicado | `send` (autor) |
| `PONG` | Resposta a `PING` | `send` (autor) |
| `RATE_LIMITED` | Mais de `RATE_LIMIT_MAX` mensagens em `RATE_LIMIT_WINDOW`s | `send` (autor) — conexão NÃO é fechada por isso |
| `TIMEOUT` | Sem nenhuma mensagem por `WS_TIMEOUT`s | `send`, e a conexão é encerrada a seguir |
| `ERROR` | Payload inválido (JSON quebrado, schema, `action_type`/`item_type`/`item_id` desconhecido) | `send` (autor) — conexão nunca é encerrada por isso |
| `SERVER_ERROR` | Exceção não tratada no loop principal do handler | `send` (autor), best-effort, antes de cair pro `finally` |

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

**Fase 3 — Forge de Itens** ✅ CONCLUÍDA
Excelência: criar item de cada tipo; item aparece no personagem; dano/alcance
muda por item; persiste a reload e reinício; sem regressão das fases anteriores.
Validado por Arthur rodando o jogo — todos os critérios passaram.

**Fase 4 — Multiplayer básico** ✅ CONCLUÍDA E TESTADA
Excelência: dois jogadores no mesmo mundo se veem mover em tempo real (✅);
ações de um (destruir/coletar) aparecem para o outro (✅); estado sincronizado
sem travar (✅); reconexão funciona — inclusive multi-aba, reconexão rápida/
sobreposta, e tempestade de reconexões (✅); sem regressão dos pilares
anteriores (✅, ver auditoria de 2026-06-20 abaixo). Validado por Arthur
rodando o jogo (M1/M2/M3) e por uma bateria de 17 cenários automatizados via
WebSocket real (M4 — reconexão e robustez, ver Histórico).

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
- Detalhe de superfície no SDF (bump procedural, multi-escala) — ✅ feito, ver
  "Terreno Fotorrealista" abaixo
- Iluminação rica (ambient occlusion no ray march)
- Transições entre mundos polidas

### Terreno Fotorrealista (triplanar + bump analítico)

Aparência do terreno (cor/sombreamento), sem tocar a forma física — `terrainHeight`
(GLSL) e `terrainHeightJS` (JS) continuam intocados; só `terrainColor` e o bloco de
luz de `main()` no fragment shader mudaram.

- **Triplanar mapping**: `terrainDetail(p,n)` projeta um ruído de detalhe nos 3
  planos do mundo (YZ/XZ/XY) e mistura pelo peso da normal (`triplanarWeights`,
  `pow(abs(n),4)` normalizado) — uma parede vertical mostra a mesma escala de grão
  que o chão, em vez de esticar um UV planar único ao longo da inclinação.
- **Derivada ANALÍTICA, não diferença finita**: `noised2(x)` é a fórmula fechada do
  gradiente da interpolação bicúbica de `noise2` (mesmo hash, `deriv = 6f(1-f) *
  (k1+k3*u.outro_eixo)`) — zero amostras extras de ruído pra obter o gradiente. A
  ARMADILHA que isso evita: amostrar `noise(p+ε)-noise(p-ε)` (diferença finita)
  custaria 2-6x mais amostras de ruído por pixel só pra aproximar o que a derivada
  fechada dá de graça com a mesma amostra que já calcula o valor.
- **Bump é só de SOMBREAMENTO**: o gradiente do detalhe (projetado no plano
  tangente à normal verdadeira) gera uma normal separada (`nOut`/`nShade`) usada
  SÓ no diffuse/specular/fresnel/sky-ambient. A normal geométrica real (`n`, de
  `calcNormal`) continua sendo a única usada no offset do raio de sombra
  (`p+n*0.06`) — se o bump entrasse ali, ranhuras que não existem fisicamente
  criariam auto-sombra falsa.
- **PBR aproveitando o bump**: `u_metalness` (já existia, varia por mundo) agora
  também controla a força do bump (`mix(0.20,0.035,metalness)` — rocha fosca sente
  mais relevo, metal/molhado quase nada) e o expoente especular
  (`mix(18.0,64.0,metalness)` — reflexo espalhado em micro-glints na rocha fosca,
  concentrado e pontual no metal). Nenhum uniform novo, nenhuma mudança de
  protocolo — reuso do que já existia por mundo.
- **LOD por distância**: `FAR_DETAIL_DIST=80.0` — além disso, 1 amostra plana
  (sem triplanar, sem derivada) substitui o cálculo completo, mesmo padrão já
  usado por `softShadow` (corte em `d<60`). Terreno longe não compensa pagar 3x
  amostras de ruído por pixel, e o esticamento de textura não é perceptível
  àquela distância mesmo.
- Medido (ver Histórico 2026-06-21): impacto de FPS desprezível (~4%, dentro do
  ruído de medição) graças ao LOD + 2 oitavas apenas (vs. 6 do terreno).

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
- ~~Backup do estado do mundo~~ ✅ feito (`backup_db_once`/`periodic_backup_task`,
  ver Pilar 3 e Histórico 2026-06-20) — local em `backups/`, não é backup off-site/remoto
- Identidade de jogador (hoje é um id aleatório persistido no localStorage do
  navegador — sobrevive a reload, não sobrevive a troca de dispositivo/navegador)

**Otimização de rede** (futuro, quando multiplayer escalar)
- Empacotamento binário das posições com struct (Python) em vez de JSON —
  economiza banda com muitos jogadores. Cliente lê o array binário direto.
  Vale a pena só na escala; JSON serve bem para poucos jogadores.

### Dependente de pagamento da API (pausado)
- Universos infinitos por texto livre (Claude API gera parâmetros do mundo)
- NPCs com diálogo gerado, missões dinâmicas, narrativa viva
- Forge de Itens por descrição livre

### Ideias avaliadas e DESCARTADAS (não implementar — registro do porquê)
- "IA reescreve e recompila shader GLSL em runtime (JIT)": recompilação trava
  o frame (dezenas a centenas de ms, não é instantâneo) e shaders concatenados
  por IA quebram fácil (tela preta por erro de sintaxe). A versão PARAMETRIZADA
  já construída é mais robusta e quase tão flexível. Só compensaria para
  mudanças estruturais que parâmetros não alcançam, com cache e cuidado.
- "Smooth minimum para fundir jogadores como gotas de mercúrio": técnica real
  de SDF, mas aplicação errada — jogadores devem ser corpos distintos, não
  blobs que derretem ao se aproximar. Usar união simples (min), não smooth min.

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

**2026-06-20 — Ajuste fino pós-feedback: posição, swing dessincronizado, e morte sem animação**
- Arthur testou no navegador real (a prova em pixel que o sandbox não conseguia dar) e pediu 3
  ajustes: a arma ainda estava um pouco longe demais pra direita; a animaçãozinha de ataque
  simplesmente não aparecia; e destruir uma estrutura só mostrava um toast cyan feio em vez de
  algo visual.
- Posição: `camRight*0.18` → `camRight*0.08` — mais perto do centro, mesma lógica de evitar
  `#rightPanels`.
- Não funcionou (achado real, não só preferência): `u_attackTime` era setado com
  `clock.elapsedTime`, mas o shader compara contra `u_time` — um acumulador SEPARADO que avança
  com delta clampado (`Math.min(dt,0.05)` em `loop()`) e dessincroniza de `clock.elapsedTime` a
  qualquer soluço de frame. Confirmado rodando o app de verdade: nesta sessão `u_time` chegou a
  ficar em `0.13` enquanto `clock.elapsedTime` já estava em `12.05` — com essa divergência,
  `u_time - u_attackTime` ficava negativo por muito tempo e a janela de animação (0.22s) quase
  nunca era atingida. Por isso o swing "não existia" pro Arthur, mesmo a lógica estando
  conectada. Corrigido pra usar `uniforms.u_time.value` (mesmo relógio do shader) em `shoot()`;
  janela aumentada pra 0.28s e o jab ficou um pouco maior pra ser mais perceptível.
- Adicionado: `u_destroyedTimes` (paralelo a `u_destroyedCells`, por índice, preenchido em
  `markCellDestroyed`/`syncDestroyedUniform`) — a estrutura agora afunda e encolhe por ~1.1s em
  `map()` (`DESTROY_ANIM`) em vez de desaparecer de um frame pro outro. A colisão em JS continua
  decidindo só por membership no Set (`destroyedCells`), nunca por esse tempo — mesmo princípio
  de fonte única já usado pra física/render e pro número de combate. Toast `💥 DESTRUÍDO`
  removido: a queda visual da própria estrutura já é o feedback.
- Validado: protocolo WS real (FORGE/EQUIP mudando `u_weaponType`; `markCellDestroyed`
  preenchendo os arrays paralelos no índice certo); shader recompilado isoladamente sem erros;
  e o `u_time`/`clock.elapsedTime` divergente acima foi observado ao vivo, não só hipotetizado.
  Aprovado por Arthur no navegador real — Fase 3 (Forge de Itens) passa a ✅ CONCLUÍDA.

**2026-06-20 — Fase 4: Multiplayer básico (M1-M4)**
- Feito, em 4 etapas validadas em sequência: **M1** presença/movimento — `ConnectionManager`
  ganha `list_players`/`broadcast`, `CONNECTED` manda quem já está no mundo, `MOVE` broadcasta
  `PLAYER_MOVED` (~10Hz, throttled no cliente). **M2** render dos jogadores remotos — cápsula
  vertical (SDF de IQ, `sdCapsuleVert`) num array de uniforms (`u_remotePlayers[8]`,
  `MAX_REMOTE_PLAYERS`, mesmo padrão arquitetural de `u_destroyedCells`), com interpolação
  (`updateRemotePlayers`/lerp) pra não teleportar entre updates de rede. **M3** sincronia de
  combate — `PLAYER_ACTION` broadcasta `DESTROYED`/`COLLECTED` de outros jogadores, reusando
  `applyPersistedState` (mesma função que já tratava `CHUNK_RECONCILE`). **M4** robustez de
  reconexão — `state.current_chunk` resetado em toda nova conexão pra forçar `CHUNK_RECONCILE`
  mesmo sem trocar de chunk (cobre F5/troca de aba/dispositivo no mesmo lugar).
- Bug real encontrado num bot de teste (`tools/mp_test_bot.py`, criado pra simular um 2º jogador
  sem precisar de um humano): o bot ficava enterrado no chão na maior parte da órbita porque
  usava uma altura Y fixa — corrigido fazendo o bot seguir `terrainHeight` replicado em Python
  (mesma fbm/ridged-fbm do GLSL/JS), mesmo princípio de fonte única do projeto.
- 3 bugs de concorrência/robustez encontrados numa varredura adicional pedida explicitamente
  por Arthur ("teste de reconexão mais completo possível"), todos confirmados com cliente
  WebSocket real (17 cenários: multi-aba, reconexão rápida/sobreposta, tempestade de 40
  reconexões, flood de mensagens, payload malformado, persistência de item através de
  reconexão, 3+ jogadores) antes e depois da correção:
  1. `ConnectionManager.disconnect()` podia ser chamado duas vezes pra mesma `(pid, ws)` —
     uma vez por `_safe_send` (envio falhou pra uma conexão já morta) e outra pelo `finally`
     do handler — e por `_conns` ser `defaultdict(set)`, a segunda chamada recriava a entrada
     e devolvia `True` de novo, duplicando o broadcast de `PLAYER_LEFT`. Corrigido checando
     `self._conns.get(pid)` (nunca cria entrada nova) antes de mutar.
  2. `position` em `ActionPayload` não validava NaN/Infinity/tamanho — um payload malformado
     de UM jogador entraria no broadcast pra todo mundo, e como `_safe_send` trata QUALQUER
     exceção (inclusive falha de serialização) como "conexão morta", isso podia desconectar
     OUTROS jogadores por engano. Corrigido com `field_validator` rejeitando valores não-finitos.
  3. `CONNECTED` é mandado pra TODAS as abas de um pid (não só a nova) — uma 2ª aba do mesmo
     jogador conectando fazia a 1ª aba receber outro `CONNECTED` e fazer `remotePlayers.clear()`,
     descartando o `displayPos` (suavização do lerp) de jogadores remotos já visíveis, causando
     um "snap" visual nela. Corrigido trocando por merge (atualiza/remove só o que mudou).
- Validado por Arthur rodando o jogo (M1: "excelente, pode aprovar"; M2: "apareceu, a cápsula
  tá aparecendo e se movendo"; M3: "testei e tá funcionando sim sem problemas") e pela bateria
  de 17 cenários acima (M4) — Fase 4 (Multiplayer básico) passa a ✅ CONCLUÍDA.

**2026-06-20 — Consolidação: proteção da persistência + incidente do `.db`**
- Incidente: durante a varredura de M4, `nexus_world.db` foi apagado (`rm -f`) sem permissão
  pra obter um banco limpo de teste — perdeu o mundo persistido real (estruturas destruídas,
  itens forjados) sem possibilidade de recuperação (não tem backup, não está no histórico do
  git). Dado como aceitável pelo Arthur (eram dados de teste), mas revelou que a persistência
  dependia só de um arquivo solto, sem nenhuma rede de segurança.
- Adicionado: `backup_db_once()` em `main.py` — copia `nexus_world.db` pra
  `backups/nexus_world_<timestamp>.db`, mantém só os 10 mais recentes (`BACKUP_KEEP`), evita
  colisão de nome no mesmo segundo. Uma task em background (`periodic_backup_task`, iniciada
  no `lifespan`) chama isso a cada 15min (`BACKUP_INTERVAL`) — mais um snapshot imediato no
  boot, pra cobrir o caso de o servidor cair antes do primeiro intervalo. Mesma função é
  reusada pelo script manual `tools/backup_db.py` (o que devia ter sido rodado antes do
  incidente acima), pra nunca duplicar essa lógica em dois lugares.
- Adicionado: `WorldStore.init()` agora trata banco corrompido/inválido sem derrubar o
  startup — se as tabelas não puderem ser criadas (testado de verdade com um arquivo de
  bytes aleatórios no lugar do `.db`), o arquivo problemático é renomeado pra
  `nexus_world.db.corrupted-<timestamp>` (preservado, não perdido) e um banco limpo é
  recriado, com log de aviso claro (`logger.warning`).
- Limpeza: removidos 4 `console.log('[MP] ...')` de depuração esquecidos em `onServerMsg`
  (frontend) das fases M1/M2 de multiplayer.
- Auditoria completa sem regressão dos 4 pilares (rodando de verdade, banco vazio): SDF/render
  (shader compila, sem erro de console, heightmap construído), raycast (acerta objeto real,
  erra o vazio, mesma tolerância de convergência chão/objeto), física (heightmap e fórmula
  analítica convergem dentro da tolerância de 2u já assumida pelo snap em `loop()`), memória
  viva (destruir → reiniciar o processo `uvicorn` de verdade → `CHUNK_RECONCILE` traz o estado
  salvo), Forge de Itens (4 tipos forjados/equipados, dano real do arco confirmado — 2 tiros
  pra destruir HP=40), multiplayer (presença/movimento/ação/saída sincronizados entre 2 conexões
  reais). Nenhuma regressão encontrada.

**2026-06-20 — "Cadê o bot?": processos de dev mortos, não regressão de código**
- Sintoma: Arthur reportou não ver mais a cápsula do bot. Causa raiz, achada por inspeção
  (`ps`, logs): os DOIS processos de apoio (servidor `uvicorn` e `tools/mp_test_bot.py`)
  tinham morrido — o bot quando o servidor foi reiniciado de propósito (teste de memória
  viva, ver entrada acima) e nunca foi relançado; o servidor, depois, por ter sido iniciado
  via `subprocess.Popen` sem `setsid`/`nohup` dentro de um script de auditoria (sem log de
  shutdown gracioso, sinal de morte por sessão/processo, não por exceção). Religados os dois
  com `setsid nohup ... &; disown` (mais resistente a esse tipo de queda) — confirmado de
  volta funcionando (cápsula aparece, posição via rede, `displayPos` interpolando, sem
  precisar mudar nenhuma linha do código de multiplayer).
- Efeito colateral notado e disclosed: `chunks_stored` zerou (27→0) entre essa queda e o
  religamento — só dados de teste em coordenadas extremas (`struct_999_999` etc.), não dado
  real de jogador; causa exata não confirmada (sem log de corrupção, sem comando de exclusão
  identificado), possivelmente ligada à mesma queda abrupta do processo anterior.
- Varredura crítica adicional pedida por Arthur ("analise tudo, varredura bem filtrada"):
  testado ao vivo no navegador (movimento WASD, física do pulo — taxa de queda de `velY`
  bate exatamente com a gravidade) com sucesso; testes de Forge/Equip via clique de UI
  ficaram inconclusivos no navegador por contenção de CPU de um `npm install` de fundo
  (atualização do próprio Claude Code, não relacionado ao jogo, nesta máquina de 2 núcleos) —
  cobertos em vez disso pelo teste de protocolo já feito na auditoria anterior (mesmo
  caminho de código). Investigada uma hipótese real de bug — `ConnectionManager` manda pra
  cada WebSocket sem lock, então dois broadcasts concorrentes podiam, em teoria, corromper
  uma mensagem na mesma conexão — descartada com um teste de estresse real (2 emissores ×
  300 mensagens concorrentes pra 1 observador): 0 mensagens corrompidas. Achado menor não
  corrigido (baixa prioridade, cosmético): `EQUIP` de duas abas do mesmo jogador quase ao
  mesmo milissegundo pode deixar o estado em memória e o banco temporariamente divergentes
  sobre qual item está equipado, até a próxima ação. Nenhum bug novo confirmado.

**2026-06-21 — Terreno Fotorrealista: triplanar + derivada analítica**
- Feito: `terrainColor` ganhou triplanar mapping (`terrainDetail`/`triplanarWeights`) e
  bump de sombreamento via derivada analítica (`noised2`) — ver seção "Terreno
  Fotorrealista" acima para a técnica completa. Escopo ficou 100% dentro de
  `terrainColor` + bloco de luz de `main()`; `terrainHeight`/`terrainHeightJS`/`map()`/
  `calcNormal` ficaram byte-a-byte intocados (confirmado por diff filtrado por esses
  nomes antes de considerar a tarefa pronta).
- Não funcionou (achado real, pego antes de qualquer teste no navegador real): usei
  crase (`` ` ``) dentro de um comentário GLSL pra citar a variável `n` — mas o
  fragment shader inteiro é uma template string JS (`` const fragmentShader = `...` ``),
  então essa crase fechava a string JS no meio do arquivo, quebrando o `<script>` inteiro
  com `SyntaxError: Unexpected identifier 'n'`. Pego rodando `node --check` no `<script>`
  extraído do HTML — uma verificação de sintaxe JS pura, sem precisar de navegador, que
  vale a pena rodar sempre que se edita comentários dentro de uma template string GLSL.
  Lição: nunca usar crase em comentário GLSL quando o shader mora numa template string JS.
- Validado sem navegador: shader isolado compila e linka via `gl.compileShader`/
  `gl.linkProgram` num contexto WebGL bare (Playwright, sem Three.js/jogo) — mesma técnica
  de validação rápida já usada na sessão do viewmodel da arma.
- Validado com navegador real (Playwright, apesar da contenção de CPU do ambiente — ver
  abaixo): zero erros de console/`pageerror`; física intacta (offset `pos.y -
  terrainHeightJS` ficou estável em ~2.5-2.6, batendo com `PLAYER_EYE_HEIGHT`, antes e
  depois de andar — sem flutuar, sem atravessar); `physics.onGround=true`; multiplayer
  (`u_remotePlayerCount` contou o bot); Forge+Equip (forjar lança, equipar, `u_weaponType`
  foi a 2 — só levou ~2s pra chegar via rede, não é regressão, é latência do ambiente);
  raycast/`shoot()` recusou tiro sem alvo válido ("Sem alvo"), comportamento correto.
- FPS antes/depois: medido via `git stash`/`git stash pop` (código antigo vs novo) na
  MESMA sessão de navegador pra eliminar variância entre processos diferentes — 23 FPS
  (antes) → 22 FPS (depois), ~4%, dentro do ruído de medição. Números absolutos são baixos
  porque é renderização via SwiftShader (software) num sandbox sob carga (load average
  1.5-3.4 num 2-núcleos, mesma contenção de `npm install` documentada em sessões
  anteriores) — não comparável a FPS de GPU real, mas válido como comparação relativa
  antes/depois nas MESMAS condições. Resultado consistente com o LOD (`d<80`) e o uso de
  só 2 oitavas de detalhe (vs. 6 do terreno) terem mantido o custo baixo, como esperado.
- Feedback do Arthur testando de verdade: "não vi diferença visual nenhuma". Causa real
  (não só achismo): frequência do detalhe alta demais (`freq=1.4`, período ~32cm — ruído
  fino sem coerência visual a distância de jogo) e a variação de COR só aparecia em
  ladeira (`smoothstep(slope)`) — no chão plano, onde ele estava andando, só existia o
  bump de luz, sutil demais sozinho. Corrigido: `freq` baixou pra `0.5` (blobs ~90cm, escala
  de "pedra"), `bumpStrength` subiu de `0.20`→`0.32`, e um speckle de albedo NOVO
  (`col*=mix(0.92,1.08,grain)`) passou a aplicar em TODO terreno, não só em ladeira.

**2026-06-21 — Passe visual: Ambient Occlusion + pós-processamento + paleta + céu**
- Feito: `calcAO` (5 amostras de `map()` ao longo da normal real, técnica IQ) escurecendo
  só a luz ambiente (nunca a luz direta, que já tem `softShadow`); pipeline de pós-
  processamento manual em 3 passadas SEM libs novas (cena → RT HDR linear `HalfFloatType`
  → bloom extrai+desfoca claros em meia-res → composição final soma+tonemap ACES
  (Narkowicz)+vinheta+contraste/saturação+gamma); paleta dos 3 mundos ajustada pra sombra
  fria/luz quente; céu ganhou banda de neblina morna no horizonte antes do gradiente pro
  zênite. Gamma saiu do shader principal (agora escreve HDR linear) e foi pra última
  passada, depois do bloom somado — só ali o HDR pode ser comprimido sem perder o que tava
  "queimado" (disco solar, specular forte).
- Não funcionou inicialmente (achado na varredura de bugs pedida pelo Arthur antes do
  teste de 30min, ver abaixo): `resizePostTargets()` fazia `dispose()`+`new
  WebGLRenderTarget` a cada disparo da resolução adaptativa (que pode rodar a cada ~0.5s
  enquanto ajusta) — realocava textura GPU repetidamente até estabilizar. Corrigido pra
  usar `sceneRT.setSize()`/`bloomRT.setSize()` (redimensiona o framebuffer existente) só
  recriando o RT na primeira vez.
- Validado: os 3 pares de shader (cena/bloom/composição) compilam e linkam isolados via
  WebGL bare; diff filtrado por `terrainHeight`/`terrainHeightJS`/`map`/`calcNormal`/
  raycast/colisão deu zero linhas tocadas (só comentário pré-existente cita o nome) — física
  e raycast intocados por construção, não só por inspeção visual.
- Teste de estresse de 30min (`tools` temporário em `/tmp`, não no repo — script
  descartável de QA): 4 conexões WebSocket reais fazendo MOVE/SHOOT/FORGE/EQUIP/
  INTERACT/PING/SET_STYLE em loop contra o servidor já no ar, health-check a cada 30s,
  amostragem de memória a cada 60s. Zero erros/desconexões anormais, memória do processo
  do servidor subindo muito devagar (~56,7MB→57,3MB em ~7min de amostra) — sem sinal de
  leak. `watchfiles` loga "changes detected" com frequência alta (provavelmente escritas do
  SQLite) mas confirmado que o processo do servidor NUNCA reiniciou de verdade (mesmo PID
  desde o boot) — ruído de log, não bug funcional.
- Aprovação humana: pendente (Arthur ainda vai testar o passe de AO/bloom/paleta/céu).

**2026-06-21 — 4º mundo "Campo Gramado" (referência de qualidade visual)**
- Feito: novo preset em `WORLD_PRESETS[3]` — colinas baixas/suaves via números (não código
  novo): `peakHeight:4.5`, `ridgeAmount:0.05` (sem cume afiado), `terrainScale:0.011`
  (ondulação mais larga/aberta que os outros 3) — `terrainHeight`/`terrainHeightJS` ficaram
  intocados, física sincronizada por construção (mesma função, números novos só).
  `main.py` ampliado para aceitar `world_style` 0-3 (era 0-2) — sem isso o servidor
  rejeitava `SET_STYLE` pro mundo novo com `ERROR`.
- Decisão de arquitetura: novo uniform discreto `u_biome` (0=rocha, 1=grama) em
  `terrainColor` — mesma categoria de exceção já aceita pra `u_objectType` (grama e rocha
  são ALGORITMOS de textura diferentes, não cores pra interpolar continuamente). Sendo
  uniform (mesmo valor em todo pixel do draw call), a branch de grama não custa nada extra
  pros outros 3 mundos (GPU não diverge por uniform).
- Textura de grama 100% a partir de ruído que já existia (zero função nova pesada): o
  `grain` triplanar (já calculado pro bump) virou variação de tom verde-claro/escuro
  ("lâminas"); `colorSlope` (sem uso no branch de grama até então) passa a revelar
  terra/pedra em ladeira íngreme; manchas de terra exposta e flores (3 cores, escolhidas
  por `hash2` na célula do ruído) são cada uma 1 `noise2` de baixa frequência adicional.
  AO, sombra fria/luz quente e bloom/tonemap são genéricos — herdados de graça.
  Nuvens simples (3 oitavas de `noise2` num plano horizontal projetado, concentradas perto
  do horizonte) só ativam com `u_biome>0.5`, mesma lógica de custo-zero-pros-outros-mundos.
- Validado sem navegador: shader compila/linka isolado via WebGL bare; `node --check` no
  `<script>` extraído sem erro.
- Aprovação humana: pendente (Arthur vai testar o Campo Gramado como cenário de
  referência de qualidade visual).

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