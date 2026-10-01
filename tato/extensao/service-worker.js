/* Cada RPC resolve a sessão para uma aba criada pela extensão.
 * Nenhum comando aceita tabId do backend, não enumera abas pessoais e não
 * conecta CDP ao navegador inteiro. A permissão debugger é aplicada somente
 * ao tabId que chrome.tabs.create acabou de devolver. */

// A porta que o primeiro Tato abre (TATO_PORTA).
const ENDPOINTS = ['ws://127.0.0.1:47812/tato/ws'];
const OWNED_KEY = 'tatoOwnedTabs';
// Aceita os dois nomes de chave para ler tokens de pareamento já armazenados.
const TOKEN_KEYS = ['tatoBridgeToken'];
// Quem está usando cada conversa, como o servidor manda em cada comando.
const agentes = {};
const nomeDoAgente = nome => String(nome || '').trim().slice(0, 40) || 'Agente';
async function savedToken() {
  const saved = await chrome.storage.local.get(TOKEN_KEYS);
  return saved.tatoBridgeToken;
}
const ROLES = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio',
  'tab', 'menuitem', 'option', 'heading', 'text', 'StaticText', 'image', 'table', 'row', 'cell', 'listitem']);
const INTERACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox',
  'radio', 'tab', 'menuitem', 'option']);

let socket = null;
let connecting = null;
let heartbeat = null;
let paired = false;
let owned = {};
const attached = new Set();
const pageState = new Map();
// O frame principal de cada aba: é o carregamento dele que a leitura espera.
const mainFrames = new Map();
const validations = new Map();
let overlaySource = null;

async function overlayScript() {
  if (!overlaySource) {
    const response = await fetch(chrome.runtime.getURL('browser_sobreposicao.js'));
    if (!response.ok) throw new Error('sobreposicao da aba indisponivel');
    overlaySource = await response.text();
  }
  return overlaySource;
}

const loaded = (async () => {
  owned = (await chrome.storage.session.get(OWNED_KEY))[OWNED_KEY] || {};
  for (const [conversation, record] of Object.entries(owned)) {
    if (record.closed) continue;
    try { await chrome.tabs.get(record.tabId); }
    catch { owned[conversation] = { closed: true }; }
  }
  await chrome.storage.session.set({ [OWNED_KEY]: owned });
})();

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

// Tenta cada servidor da lista. Um token emitido por outro servidor é
// recusado: aí a extensão pede de novo sem ele, e o ID fixo dela basta.
async function connect(secret) {
  await loaded;
  if (socket?.readyState === WebSocket.OPEN && paired) return true;
  if (connecting) return connecting;
  connecting = (async () => {
    let erro = null;
    for (const endpoint of ENDPOINTS) {
      for (const tentativa of secret ? [secret, ''] : ['']) {
        try { return await connectTo(endpoint, tentativa); } catch (falha) { erro = falha; }
      }
    }
    throw erro || new Error('nenhum servidor respondeu');
  })().finally(() => { connecting = null; });
  return connecting;
}

function connectTo(endpoint, secret) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(endpoint);
    socket = ws;
    let ready = false;
    const timeout = setTimeout(() => { if (!ready) ws.close(); }, 10000);
    ws.onopen = () => ws.send(JSON.stringify({ kind: 'pair', secret }));
    ws.onmessage = async ({ data }) => {
      let message;
      try { message = JSON.parse(data); } catch { return; }
      if (message.kind === 'ready') {
        ready = true;
        paired = true;
        clearTimeout(timeout);
        await chrome.storage.local.set({ tatoBridgeToken: message.token });
        heartbeat = setInterval(() => {
          send({ kind: 'ping' });
          // O sinal de vida mantém a borda das abas do agente acesa.
          for (const tabId of attached) void toggleOverlay(tabId, 'vivo');
        }, 20000);
        for (const [conversation, record] of Object.entries(owned)) {
          if (record.closed) send({ kind: 'closed', conversation });
        }
        resolve(true);
      } else if (message.kind === 'validation') {
        const pending = validations.get(message.id);
        if (pending) { validations.delete(message.id); pending(message); }
      } else if (message.kind === 'command') {
        void handleCommand(message);
      }
    };
    ws.onclose = () => {
      clearTimeout(timeout);
      if (heartbeat) clearInterval(heartbeat);
      heartbeat = null;
      if (socket === ws) {
        socket = null;
        paired = false;
      }
      for (const resolveValidation of validations.values()) resolveValidation({ allowed: false, reason: 'ponte desconectada' });
      validations.clear();
      if (!ready) reject(new Error('Conexão recusada. Confira se o agente está aberto.'));
    };
    ws.onerror = () => { /* onclose dá a resposta ao popup */ };
  });
}

async function validate(url) {
  if (socket?.readyState !== WebSocket.OPEN) return { allowed: false, reason: 'ponte desconectada' };
  const id = crypto.randomUUID();
  return new Promise((resolve) => {
    const timer = setTimeout(() => { validations.delete(id); resolve({ allowed: false, reason: 'validação expirou' }); }, 8000);
    validations.set(id, (message) => { clearTimeout(timer); resolve(message); });
    send({ kind: 'validate', id, url });
  });
}

function conversationFor(tabId) {
  return Object.keys(owned).find(key => owned[key].tabId === tabId);
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const conversation = conversationFor(source.tabId);
  if (!conversation) return;
  if (method === 'Fetch.requestPaused') {
    void (async () => {
      const answer = await validate(params.request?.url || '');
      try {
        if (answer.allowed) {
          await chrome.debugger.sendCommand(source, 'Fetch.continueRequest', { requestId: params.requestId });
        } else {
          stateFor(conversation).blocked = answer.reason || 'navegação recusada';
          await chrome.debugger.sendCommand(source, 'Fetch.failRequest',
            { requestId: params.requestId, errorReason: 'BlockedByClient' });
        }
      } catch { /* A aba pode ter sido fechada enquanto a validação corria. */ }
    })();
  } else if ((method === 'Page.frameStartedLoading' || method === 'Page.frameStoppedLoading')
             && params.frameId && params.frameId === mainFrames.get(source.tabId)) {
    stateFor(conversation).carregando = method === 'Page.frameStartedLoading';
  } else if (method === 'Page.frameNavigated' && params.frame && !params.frame.parentId
             && params.frame.id === mainFrames.get(source.tabId)) {
    stateFor(conversation).novoDocumento = true;
  } else if (method === 'Runtime.consoleAPICalled') {
    const state = stateFor(conversation);
    state.console.push({ tipo: params.type || 'log', texto: (params.args || []).map(a => a.value ?? a.description ?? '').join(' ').slice(0, 1000) });
    if (state.console.length > 100) state.console.shift();
  }
});

chrome.debugger.onDetach.addListener(source => attached.delete(source.tabId));
chrome.tabs.onRemoved.addListener(tabId => {
  const conversation = conversationFor(tabId);
  if (!conversation) return;
  owned[conversation] = { closed: true };
  attached.delete(tabId);
  pageState.delete(conversation);
  void chrome.storage.session.set({ [OWNED_KEY]: owned });
  send({ kind: 'closed', conversation });
});

function stateFor(conversation) {
  if (!pageState.has(conversation)) pageState.set(conversation, {
    epoch: 0, nextRef: 1, byBackend: new Map(), byRef: new Map(), previous: null,
    lastUrl: '', blocked: '', console: [], carregando: false, novoDocumento: false,
  });
  return pageState.get(conversation);
}

async function ownTab(conversation) {
  await loaded;
  if (!conversation || conversation.length > 200) throw new Error('conversa inválida');
  const current = owned[conversation];
  if (current) {
    if (current.closed) throw new Error('A aba do agente foi fechada. Reabra pelo cartão da conversa antes de continuar.');
    try { await chrome.tabs.get(current.tabId); return current.tabId; }
    catch {
      owned[conversation] = { closed: true };
      await chrome.storage.session.set({ [OWNED_KEY]: owned });
      send({ kind: 'closed', conversation });
      throw new Error('A aba do agente foi fechada. Reabra pelo cartão da conversa antes de continuar.');
    }
  }
  const tab = await chrome.tabs.create({ url: 'about:blank', active: true });
  if (typeof tab.id !== 'number') throw new Error('Chrome não devolveu o ID da aba');
  try {
    // Uma aba nova é um evento visível. Continuar numa aba existente não rouba foco.
    const janela = await chrome.windows.get(tab.windowId);
    await chrome.windows.update(tab.windowId, {
      ...(janela.state === 'minimized' ? { state: 'normal' } : {}), focused: true,
    });
    const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
    const nome = agentes[conversation] || 'Agente';
    await chrome.tabGroups.update(groupId, { title: `${nome} ${conversation.replace(/^tato-/, '').slice(0, 8)}`,
                                             color: 'purple' });
    owned[conversation] = { tabId: tab.id, groupId };
    await chrome.storage.session.set({ [OWNED_KEY]: owned });
    return tab.id;
  } catch (error) {
    await chrome.tabs.remove(tab.id);
    throw error;
  }
}

async function attach(tabId, agente = 'Agente') {
  if (attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, '1.3');
  attached.add(tabId);
  try {
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');
    const arvore = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');
    if (arvore?.frameTree?.frame?.id) mainFrames.set(tabId, arvore.frameTree.frame.id);
    await chrome.debugger.sendCommand({ tabId }, 'DOM.enable');
    await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable');
    await chrome.debugger.sendCommand({ tabId }, 'Accessibility.enable');
    try {
      // Quem usa vem antes da sobreposição: a página nova já nasce com a cor dele.
      const quem = `window.__tatoAgente = ${JSON.stringify(agente)};`;
      await chrome.debugger.sendCommand({ tabId }, 'Page.addScriptToEvaluateOnNewDocument', { source: quem });
      await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', { expression: quem });
      const source = await overlayScript();
      await chrome.debugger.sendCommand({ tabId }, 'Page.addScriptToEvaluateOnNewDocument', { source });
      await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', { expression: source });
    } catch (error) {
      console.warn('Tato: indicacao visual indisponivel nesta pagina', error);
    }
    // Pausa toda navegação (incluindo redirects) para revalidar URL/DNS no backend.
    await chrome.debugger.sendCommand({ tabId }, 'Fetch.enable', {
      patterns: [{ urlPattern: '*', resourceType: 'Document', requestStage: 'Request' }],
    });
  } catch (error) {
    attached.delete(tabId);
    try { await chrome.debugger.detach({ tabId }); } catch { /* já desconectou */ }
    throw error;
  }
}

async function command(tabId, name, args = {}) {
  return chrome.debugger.sendCommand({ tabId }, name, args);
}

async function evaluate(tabId, expression) {
  const result = await command(tabId, 'Runtime.evaluate', { expression, returnByValue: true, awaitPromise: false });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'falha ao ler página');
  return result.result?.value;
}

async function pageInfo(tabId) {
  return await evaluate(tabId, '({url: location.href, titulo: document.title})') || { url: '', titulo: '' };
}

async function showOverlay(tabId, x, y, label, click = false, box = null) {
  try {
    const args = [x, y, label, click, box];
    await evaluate(tabId, `window.__tato?.apontar(...${JSON.stringify(args)})`);
  } catch { /* A pagina pode estar navegando; a acao continua. */ }
}

async function toggleOverlay(tabId, method) {
  try { await evaluate(tabId, `window.__tato?.${method}()`); }
  catch { /* A aba pode ter mudado de documento. */ }
}

// A leitura que vai ao modelo: sem o texto que só repete o nome do link ou
// botão de cima, sem os números internos da árvore e com teto na primeira
// leitura. `completo` traz tudo.
const LIMITE_DA_LEITURA = 250;
const LIMITE_DO_NOME = 200;
// O texto corrido da página recebe um limite próprio.
const LIMITE_DO_TEXTO = 500;
const LIMITE_DA_FRASE = 500;

// A árvore na ordem de leitura da página: da raiz para as folhas, filho a
// filho. A ordem de criação dos nós pode diferir da ordem visual.
function emOrdem(nodes) {
  const porId = new Map(nodes.map(node => [String(node.nodeId), node]));
  const pilha = nodes.filter(node => !node.parentId || !porId.has(String(node.parentId))).reverse();
  const saida = [];
  const vistos = new Set();
  while (pilha.length) {
    const node = pilha.pop();
    const id = String(node.nodeId);
    if (vistos.has(id)) continue;
    vistos.add(id);
    saida.push(node);
    const filhos = (node.childIds || []).map(filho => porId.get(String(filho))).filter(Boolean);
    for (let i = filhos.length - 1; i >= 0; i -= 1) pilha.push(filhos[i]);
  }
  for (const node of nodes) if (!vistos.has(String(node.nodeId))) saida.push(node);
  return saida;
}

// Junta trechos de texto contíguos no mesmo bloco.
function juntar(antes, depois) {
  return `${antes} ${depois}`.replace(/\s+([,.;:!?)\]”"])/g, '$1').replace(/([(\[“])\s+/g, '$1');
}

const normalizar = texto => String(texto || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();

// `nome` no lugar de `ref`: o elemento clicável com esse texto, lido agora.
// É o que deixa um roteiro clicar numa página que ele ainda não leu.
async function refPorNome(tabId, conversation, nome) {
  const alvo = normalizar(nome);
  await snapshot(tabId, conversation);
  const itens = [...stateFor(conversation).previous.values()].filter(item => item.ref);
  const achado = itens.find(item => normalizar(item.name) === alvo)
    || itens.find(item => normalizar(item.name).includes(alvo));
  if (!achado) throw new Error(`nenhum elemento com o nome «${nome}» nesta página; leia a página e use o \`ref\``);
  return achado.ref;
}

function paraOModelo(item) {
  const limite = item.role === 'StaticText' ? LIMITE_DO_TEXTO : LIMITE_DO_NOME;
  const saida = { role: item.role, name: item.name.length > limite ? `${item.name.slice(0, limite)}…` : item.name };
  if (item.ref) saida.ref = item.ref;
  return saida;
}

async function snapshot(tabId, conversation, complete = false) {
  const state = stateFor(conversation);
  const info = await pageInfo(tabId);
  // Refs e diff recomeçam só com documento novo (o frame principal navegou):
  // o site trocar o endereço sem trocar de página não invalida o que o agente leu.
  const trocou = mainFrames.has(tabId) ? state.novoDocumento : info.url !== state.lastUrl;
  state.lastUrl = info.url;
  state.novoDocumento = false;
  if (trocou) { state.byBackend.clear(); state.byRef.clear(); state.previous = null; }
  const response = await command(tabId, 'Accessibility.getFullAXTree');
  const tree = [];
  const nomes = new Map();
  for (const node of emOrdem(response.nodes || [])) {
    if (node.ignored) continue;
    const role = String(node.role?.value || '');
    const name = String(node.name?.value || '').trim();
    if (!ROLES.has(role) || (!name && role !== 'row' && role !== 'cell')) continue;
    nomes.set(String(node.nodeId || ''), name);
    const anterior = tree[tree.length - 1];
    // O texto de um link ou botão vem logo depois dele na leitura: é repetição.
    if (role === 'StaticText' && ((node.parentId && nomes.get(String(node.parentId)) === name)
        || (anterior && anterior.name === name))) continue;
    if (role === 'StaticText' && anterior && anterior.role === 'StaticText'
        && anterior.name.length + name.length < LIMITE_DA_FRASE) {
      anterior.name = juntar(anterior.name, name);
      continue;
    }
    const item = { role, name, node: String(node.nodeId || '') };
    if (node.parentId) item.parent = String(node.parentId);
    if (INTERACTIVE.has(role) && node.backendDOMNodeId) {
      const backend = Number(node.backendDOMNodeId);
      let ref = state.byBackend.get(backend);
      if (!ref) {
        ref = `e${state.nextRef++}`;
        state.byBackend.set(backend, ref);
        state.byRef.set(ref, backend);
      }
      item.ref = ref;
    }
    tree.push(item);
    if (tree.length >= 600) break;
  }
  const current = new Map(tree.map(item => [item.ref || item.node || `${item.role}:${item.name}`, item]));
  let data;
  if (!state.previous || complete) {
    const limite = complete ? tree.length : LIMITE_DA_LEITURA;
    data = { modo: 'completo', arvore: tree.slice(0, limite).map(paraOModelo) };
    if (tree.length > limite) {
      data.cortados = tree.length - limite;
      data.nota = `mais ${tree.length - limite} itens abaixo: role a página, ou peça \`snapshot\` com \`completo: true\``;
    }
  } else {
    const prev = state.previous;
    data = {
      modo: 'diff',
      entrou: [...current].filter(([key]) => !prev.has(key)).map(([, item]) => paraOModelo(item)),
      saiu: [...prev].filter(([key]) => !current.has(key)).map(([, item]) => paraOModelo(item)),
      mudou: [...current].filter(([key, item]) => prev.has(key) && JSON.stringify(prev.get(key)) !== JSON.stringify(item))
        .map(([key, item]) => ({ antes: paraOModelo(prev.get(key)), depois: paraOModelo(item) })),
    };
  }
  state.previous = current;
  return { fonte: 'chrome_tab_accessibility', dados: data, epoch: state.epoch,
    confianca: 1, interativa: tree.some(item => item.ref),
    metadados: { ...info, navegacao_recusada: state.blocked || null, screenshot: false } };
}

async function pointFor(tabId, conversation, ref) {
  const backendNodeId = stateFor(conversation).byRef.get(ref);
  if (!backendNodeId) throw new Error(`ref desconhecida: ${ref}; leia a página novamente`);
  try { await command(tabId, 'DOM.scrollIntoViewIfNeeded', { backendNodeId }); } catch { /* pode já estar visível */ }
  const model = await command(tabId, 'DOM.getBoxModel', { backendNodeId });
  const quad = model.model?.content || model.model?.border;
  if (!quad || quad.length < 8) throw new Error(`elemento ${ref} não está visível`);
  const xs = [quad[0], quad[2], quad[4], quad[6]];
  const ys = [quad[1], quad[3], quad[5], quad[7]];
  return { x: (xs[0] + xs[1] + xs[2] + xs[3]) / 4,
    y: (ys[0] + ys[1] + ys[2] + ys[3]) / 4,
    box: { x: Math.min(...xs), y: Math.min(...ys),
      width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) } };
}

// Opção de <select> não tem caixa na tela quando a lista está fechada: clicar
// nela quebra. Escolher no controle, com os eventos que o site espera, é o
// que a pessoa faz ao abrir a lista e clicar. null quando o alvo não é lista.
async function escolherNaLista(tabId, conversation, ref, texto) {
  const backendNodeId = stateFor(conversation).byRef.get(ref);
  if (!backendNodeId) return null;
  const { object } = await command(tabId, 'DOM.resolveNode', { backendNodeId });
  if (!object?.objectId) return null;
  const resposta = await command(tabId, 'Runtime.callFunctionOn', {
    objectId: object.objectId, returnByValue: true, arguments: [{ value: texto ?? '' }],
    functionDeclaration: `function (texto) {
  const lista = this.tagName === 'OPTION' ? this.closest('select') : (this.tagName === 'SELECT' ? this : null);
  if (!lista) return null;
  const alvo = this.tagName === 'OPTION' ? this : Array.from(lista.options).find(o =>
    o.text.trim().toLowerCase() === String(texto || '').trim().toLowerCase() || o.value === texto);
  if (!alvo) return 'sem opção «' + texto + '» na lista; as que existem: ' + Array.from(lista.options).map(o => o.text.trim()).join(', ');
  lista.value = alvo.value;
  lista.dispatchEvent(new Event('input', { bubbles: true }));
  lista.dispatchEvent(new Event('change', { bubbles: true }));
  return '';
}`,
  });
  const erro = resposta.result?.value;
  if (erro === null || erro === undefined) return null;
  if (erro) throw new Error(erro);
  return true;
}

async function clickPoint(tabId, point) {
  const { x, y } = point;
  await command(tabId, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
  await command(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
  await command(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
}

async function execute(conversation, action) {
  const tabId = await ownTab(conversation);
  const agente = agentes[conversation] || 'Agente';
  await attach(tabId, agente);
  try { await evaluate(tabId, `window.__tato?.usar(${JSON.stringify(agente)})`); }
  catch { /* A pagina pode estar navegando; a acao continua. */ }
  const state = stateFor(conversation);
  const type = String(action?.acao || '');
  const args = action?.argumentos || {};
  if (type === 'snapshot') {
    const mode = String(args.representacao || 'accessibility');
    if (mode === 'dom') {
      const info = await pageInfo(tabId);
      const data = await evaluate(tabId, 'document.documentElement.outerHTML');
      const clean = String(data || '').replace(/<tato-sobreposicao\b[^>]*>[\s\S]*?<\/tato-sobreposicao>/g, '');
      return { fonte: 'chrome_tab_dom', dados: clean.slice(0, 100000), epoch: state.epoch,
        confianca: 1, interativa: false, metadados: { ...info, screenshot: false } };
    }
    if (mode === 'jsonld') {
      const info = await pageInfo(tabId);
      const data = await evaluate(tabId, 'Array.from(document.querySelectorAll(\'script[type="application/ld+json"]\'), x => x.textContent)');
      return { fonte: 'chrome_tab_jsonld', dados: data || [], epoch: state.epoch,
        confianca: 1, interativa: false, metadados: { ...info, screenshot: false } };
    }
    return snapshot(tabId, conversation, Boolean(args.completo));
  }
  if (type === 'imagens') {
    const info = await pageInfo(tabId);
    await toggleOverlay(tabId, 'esconder');
    let image;
    try {
      image = await command(tabId, 'Page.captureScreenshot', { format: 'png', captureBeyondViewport: Boolean(args.pagina_inteira) });
    } finally {
      await toggleOverlay(tabId, 'mostrar');
    }
    return { fonte: 'chrome_tab_screenshot', dados: image.data, epoch: state.epoch,
      confianca: 1, interativa: false, metadados: { ...info, screenshot: true } };
  }
  if (type === 'console') return { fonte: 'chrome_tab_console', dados: [...state.console], epoch: state.epoch,
    confianca: 1, interativa: false, metadados: { screenshot: false } };

  state.blocked = '';
  if (type === 'navegar') {
    const url = String(args.url || '');
    if (!/^https?:\/\//.test(url)) throw new Error('URL inválida');
    await showOverlay(tabId, 36, 48, 'navegar');
    await command(tabId, 'Page.navigate', { url });
    state.byBackend.clear(); state.byRef.clear(); state.previous = null;
  } else if (type === 'clicar' || type === 'digitar') {
    // `digitar` sem ref escreve no campo que já tem o foco (o que acabou de ser clicado).
    if (!args.ref && args.nome) args.ref = await refPorNome(tabId, conversation, String(args.nome));
    const noFoco = type === 'digitar' && !args.ref;
    if (type === 'clicar' && !args.ref) {
      throw new Error('clicar precisa de `ref` (da última leitura) ou `nome` (o texto do elemento)');
    }
    const naLista = !noFoco && await escolherNaLista(tabId, conversation, String(args.ref || ''),
      type === 'digitar' ? String(args.texto || '') : '');
    if (naLista) {
      // A opção fechada não tem caixa; a lista em si, sim.
      try {
        const ponto = await pointFor(tabId, conversation, String(args.ref || ''));
        await showOverlay(tabId, ponto.x, ponto.y, 'escolher na lista', false, ponto.box);
      } catch { /* sem caixa: a escolha vale do mesmo jeito */ }
    } else if (noFoco) {
      // O balão vai no campo que tem o foco; sem campo editável em foco, o texto iria para lugar nenhum.
      const foco = await evaluate(tabId, `(() => {
        const e = document.activeElement;
        const editavel = e && (e.isContentEditable || /^(INPUT|TEXTAREA)$/.test(e.tagName));
        if (!editavel) return null;
        const r = e.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                 box: { x: r.left, y: r.top, width: r.width, height: r.height } };
      })()`);
      if (!foco) throw new Error('nenhum campo de texto com o foco: clique no campo antes, ou mande o `ref` dele');
      await showOverlay(tabId, foco.x, foco.y, 'digitar', false, foco.box);
    } else {
      const point = await pointFor(tabId, conversation, String(args.ref || ''));
      await showOverlay(tabId, point.x, point.y, type === 'clicar' ? 'clicar' : 'digitar',
        type === 'clicar', type === 'digitar' ? point.box : null);
      await new Promise(resolve => setTimeout(resolve, 450));
      await clickPoint(tabId, point);
    }
    if (type === 'digitar' && !naLista) {
      if (!noFoco) {
        await command(tabId, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2 });
        await command(tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2 });
      }
      await command(tabId, 'Input.insertText', { text: String(args.texto || '') });
      if (args.enviar) {
        // Enter depois do texto: é assim que uma busca sai.
        await command(tabId, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' });
        await command(tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
      }
    }
  } else if (type === 'rolar') {
    await showOverlay(tabId, 450, 350, 'rolar');
    await command(tabId, 'Input.dispatchMouseEvent', { type: 'mouseWheel', x: 450, y: 350,
      deltaX: Number(args.delta_x || 0), deltaY: Number(args.delta_y || args.y || 600) });
  } else throw new Error(`ação não implementada: ${type}`);

  state.epoch += 1;
  await esperarPagina(state);
  if (type === 'navegar') await showOverlay(tabId, 36, 48, 'lendo pagina');
  const lida = await snapshot(tabId, conversation);
  // Daqui até a próxima ação quem trabalha é o modelo: a página diz isso.
  await toggleOverlay(tabId, 'pensar');
  return lida;
}

// A navegação que o gesto dispara começa logo depois dele, e o documento
// velho ainda responde: ler antes do fim do carregamento dava árvore vazia.
async function esperarPagina(state) {
  await new Promise(resolve => setTimeout(resolve, 350));
  const prazo = Date.now() + 8000;
  while (state.carregando && Date.now() < prazo) await new Promise(resolve => setTimeout(resolve, 150));
}

async function handleCommand(message) {
  const id = String(message.id || '');
  const conversation = String(message.conversation || '');
  if (conversation && message.agente) agentes[conversation] = nomeDoAgente(message.agente);
  try {
    let result;
    if (message.command === 'execute') result = await execute(conversation, message.arguments?.action);
    else if (message.command === 'show') {
      await loaded;
      const record = owned[conversation];
      if (!record || record.closed) result = { shown: false };
      else {
        const tab = await chrome.tabs.update(record.tabId, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        result = { shown: true };
      }
    } else if (message.command === 'soltar') {
      // Fim do turno: a borda e a seta apagam. Aba sem sessão de depuração não tem o que apagar.
      await loaded;
      const record = owned[conversation];
      if (record && !record.closed && attached.has(record.tabId)) await toggleOverlay(record.tabId, 'soltar');
      result = { released: true };
    } else if (message.command === 'close') {
      await loaded;
      const record = owned[conversation];
      if (record && !record.closed) await chrome.tabs.remove(record.tabId);
      result = { closed: Boolean(record && !record.closed) };
    } else if (message.command === 'reset') {
      await loaded;
      if (owned[conversation] && !owned[conversation].closed) throw new Error('A aba anterior ainda está aberta');
      delete owned[conversation];
      await ownTab(conversation);
      result = { reopened: true };
    } else throw new Error('comando desconhecido');
    send({ kind: 'result', id, ok: true, result });
  } catch (error) {
    send({ kind: 'result', id, ok: false, error: String(error?.message || error) });
  }
}

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.kind === 'status') {
    respond({ connected: paired && socket?.readyState === WebSocket.OPEN });
    return false;
  }
  if (message?.kind === 'connect') {
    // Sem código: o servidor reconhece o ID fixo desta extensão.
    void (async () => connect(await savedToken() || ''))()
      .then(() => respond({ ok: true }))
      .catch(error => respond({ ok: false, error: String(error.message || error) }));
    return true;
  }
  return false;
});

chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== 'tato-reconnect' || paired && socket?.readyState === WebSocket.OPEN) return;
  // Sem token salvo, pede sem código: o servidor reconhece o ID fixo desta extensão.
  try { await connect(await savedToken() || ''); } catch { /* servidor pode estar fechado */ }
});

void chrome.alarms.create('tato-reconnect', { periodInMinutes: 1 });
void (async () => {
  try { await connect(await savedToken() || ''); } catch { /* será retomado pelo alarm */ }
})();
