const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function ambiente(windowState = 'normal') {
  const calls = { created: [], focused: [], removed: [], grouped: [], commands: [] };
  const tabs = new Map();
  const storage = { session: {}, local: {} };
  let onRemoved;
  const chrome = {
    storage: {
      session: {
        get: async key => ({ [key]: storage.session[key] }),
        set: async value => Object.assign(storage.session, value),
      },
      local: { get: async key => ({ [key]: storage.local[key] }), set: async value => Object.assign(storage.local, value) },
    },
    tabs: {
      create: async options => {
        const tab = { id: calls.created.length + 1, windowId: 7 };
        tabs.set(tab.id, tab);
        calls.created.push(options);
        return tab;
      },
      get: async id => {
        if (!tabs.has(id)) throw Error('tab gone');
        return tabs.get(id);
      },
      group: async options => { calls.grouped.push(options); return 11; },
      remove: async id => { tabs.delete(id); calls.removed.push(id); onRemoved?.(id); },
    },
    windows: {
      get: async () => ({ state: windowState }),
      update: async (id, options) => { calls.focused.push({ id, options }); },
    },
    tabGroups: { update: async () => {} },
    debugger: {
      attach: async () => {}, detach: async () => {},
      sendCommand: async ({ tabId }, name, args) => {
        calls.commands.push({ tabId, name, args });
        if (name === 'Runtime.evaluate') {
          return { result: { value: args.expression.startsWith('({url:')
            ? { url: 'https://example.org/', titulo: 'Teste' } : true } };
        }
        if (name === 'Accessibility.getFullAXTree') return { nodes: [] };
        if (name === 'Page.getFrameTree') return { frameTree: { frame: { id: 'principal' } } };
        if (name === 'Page.captureScreenshot') return { data: 'imagem-de-teste' };
        return {};
      },
      onEvent: { addListener: callback => { calls.onEvent = callback; } }, onDetach: { addListener: () => {} },
    },
    runtime: { getURL: name => `chrome-extension://tato/${name}`, onMessage: { addListener: () => {} } },
    alarms: { onAlarm: { addListener: () => {} }, create: async () => {} },
  };
  chrome.tabs.onRemoved = { addListener: callback => { onRemoved = callback; } };
  const context = vm.createContext({ chrome, console, WebSocket: { OPEN: 1 },
    fetch: async () => ({ ok: true, text: async () => 'window.__tato = {};' }),
    setTimeout, clearTimeout, setInterval, clearInterval });
  const source = fs.readFileSync(path.join(__dirname, 'service-worker.js'), 'utf8');
  vm.runInContext(source, context);
  return { context, calls, tabs, storage };
}

test('aba nova traz Chrome para frente; continuar nao rouba foco', async () => {
  const { context, calls } = ambiente();
  const ownTab = vm.runInContext('ownTab', context);
  assert.equal(await ownTab('conversa-a'), 1);
  assert.equal(await ownTab('conversa-a'), 1);
  assert.equal(calls.created.length, 1);
  assert.equal(calls.focused.length, 1);
  assert.equal(calls.focused[0].id, 7);
  assert.equal(calls.focused[0].options.focused, true);
  assert.equal(calls.grouped.length, 1);
});

test('aba propria fechada nao adota outra aba', async () => {
  const { context, calls, tabs } = ambiente();
  const ownTab = vm.runInContext('ownTab', context);
  await ownTab('conversa-a');
  tabs.delete(1);
  await assert.rejects(ownTab('conversa-a'), /aba do agente foi fechada/i);
  await assert.rejects(ownTab('conversa-a'), /aba do agente foi fechada/i);
  assert.equal(calls.created.length, 1);
  assert.equal(calls.focused.length, 1);
});

test('Chrome minimizado volta a tela quando abre nova aba', async () => {
  const { context, calls } = ambiente('minimized');
  await vm.runInContext('ownTab', context)('conversa-b');
  assert.equal(calls.focused[0].options.state, 'normal');
  assert.equal(calls.focused[0].options.focused, true);
});

test('a moldura so e injetada na aba criada pelo agente e nao entra no print', async () => {
  const { context, calls } = ambiente();
  const execute = vm.runInContext('execute', context);
  await execute('conversa-c', { acao: 'navegar', argumentos: { url: 'https://example.org/' } });
  const injections = calls.commands.filter(call => call.name === 'Page.addScriptToEvaluateOnNewDocument');
  assert.equal(injections.length, 2);
  assert.equal(injections[0].args.source, 'window.__tatoAgente = "Agente";', 'quem usa vem antes');
  assert.equal(injections[1].tabId, 1);
  assert.equal(injections[1].args.source, 'window.__tato = {};');
  assert.equal(calls.commands.some(call => call.tabId !== 1), false);

  calls.commands.length = 0;
  const image = await execute('conversa-c', { acao: 'imagens', argumentos: {} });
  assert.equal(image.dados, 'imagem-de-teste');
  const expressions = calls.commands.filter(call => call.name === 'Runtime.evaluate').map(call => call.args.expression);
  const hidden = expressions.findIndex(expression => expression.includes('esconder()'));
  const shown = expressions.findIndex(expression => expression.includes('mostrar()'));
  const captured = calls.commands.findIndex(call => call.name === 'Page.captureScreenshot');
  assert.ok(hidden >= 0 && shown > hidden && captured >= 0);
  assert.ok(calls.commands.findIndex(call => call.args?.expression?.includes('esconder()')) < captured);
  assert.ok(calls.commands.findIndex(call => call.args?.expression?.includes('mostrar()')) > captured);
});

test('o grupo de abas leva o nome de quem usa, sempre em roxo', async () => {
  const { context } = ambiente();
  const grupos = [];
  vm.runInContext('chrome', context).tabGroups.update = async (_id, options) => { grupos.push(options); };
  const handleCommand = vm.runInContext('handleCommand', context);
  await handleCommand({ id: '1', conversation: 'conversa-claude', command: 'reset', agente: 'Claude Code' });
  await handleCommand({ id: '2', conversation: 'conversa-codex', command: 'reset', agente: 'Codex' });
  assert.equal(JSON.stringify(grupos), JSON.stringify([{ title: 'Claude Code conversa', color: 'purple' },
                                                       { title: 'Codex conversa', color: 'purple' }]));
});

test('depois do gesto, a leitura espera a pagina nova terminar de carregar', async () => {
  const { context, calls } = ambiente();
  const execute = vm.runInContext('execute', context);
  const chrome = vm.runInContext('chrome', context);
  const original = chrome.debugger.sendCommand;
  let parou = 0;
  chrome.debugger.sendCommand = async (alvo, name, args) => {
    if (name === 'Page.navigate') {
      calls.onEvent({ tabId: alvo.tabId }, 'Page.frameStartedLoading', { frameId: 'principal' });
      setTimeout(() => {
        parou = Date.now();
        calls.onEvent({ tabId: alvo.tabId }, 'Page.frameStoppedLoading', { frameId: 'principal' });
      }, 900);
    }
    return original(alvo, name, args);
  };
  await execute('conversa-carga', { acao: 'navegar', argumentos: { url: 'https://example.org/' } });
  assert.ok(parou > 0 && Date.now() >= parou, 'a leitura saiu depois do fim do carregamento');
});

test('a leitura vai enxuta: sem texto repetido do link, sem numeros internos e com teto', async () => {
  const { context } = ambiente();
  const chrome = vm.runInContext('chrome', context);
  const original = chrome.debugger.sendCommand;
  const nos = [{ nodeId: 1, role: { value: 'link' }, name: { value: 'Domains' }, backendDOMNodeId: 10 },
               { nodeId: 2, parentId: 1, role: { value: 'StaticText' }, name: { value: 'Domains' } }];
  for (let i = 0; i < 300; i += 1) nos.push({ nodeId: 100 + i, role: { value: 'heading' }, name: { value: `titulo ${i}` } });
  chrome.debugger.sendCommand = async (alvo, name, args) => (
    name === 'Accessibility.getFullAXTree' ? { nodes: nos } : original(alvo, name, args));
  const execute = vm.runInContext('execute', context);
  const lida = await execute('conversa-enxuta', { acao: 'snapshot', argumentos: {} });
  assert.equal(lida.dados.arvore.length, 250);
  assert.equal(JSON.stringify(lida.dados.arvore[0]), JSON.stringify({ role: 'link', name: 'Domains', ref: 'e1' }));
  assert.equal(lida.dados.arvore.filter(item => item.name === 'Domains').length, 1, 'o texto repetido saiu');
  assert.equal(lida.dados.cortados, 51);
  const tudo = await execute('conversa-enxuta', { acao: 'snapshot', argumentos: { completo: true } });
  assert.equal(tudo.dados.arvore.length, 301);
});

function comPagina(context, nos, extra = {}) {
  const chrome = vm.runInContext('chrome', context);
  const original = chrome.debugger.sendCommand;
  const chamadas = [];
  chrome.debugger.sendCommand = async (alvo, name, args) => {
    chamadas.push(name);
    if (name === 'Accessibility.getFullAXTree') return { nodes: nos };
    if (name in extra) return extra[name](args);
    return original(alvo, name, args);
  };
  return chamadas;
}

test('a arvore sai na ordem da pagina e os pedacos de texto viram uma frase', async () => {
  const { context } = ambiente();
  comPagina(context, [
    { nodeId: 2, parentId: 1, role: { value: 'heading' }, name: { value: 'Rodape' } },
    { nodeId: 1, ignored: true, role: { value: 'RootWebArea' }, childIds: [5, 7, 2] },
    { nodeId: 5, parentId: 1, role: { value: 'heading' }, name: { value: 'Topo' } },
    { nodeId: 7, parentId: 1, ignored: true, role: { value: 'paragraph' }, childIds: [6, 8, 9] },
    { nodeId: 6, parentId: 7, role: { value: 'StaticText' }, name: { value: 'o sentido do' } },
    { nodeId: 8, parentId: 7, role: { value: 'StaticText' }, name: { value: 'tato' } },
    { nodeId: 9, parentId: 7, role: { value: 'StaticText' }, name: { value: ', o mais antigo' } },
  ]);
  const lida = await vm.runInContext('execute', context)('conversa-ordem', { acao: 'snapshot', argumentos: {} });
  assert.equal(JSON.stringify(lida.dados.arvore.map(item => item.name)),
    JSON.stringify(['Topo', 'o sentido do tato, o mais antigo', 'Rodape']));
});

test('clicar por nome acha o elemento sem ref', async () => {
  const { context } = ambiente();
  const chamadas = comPagina(context, [
    { nodeId: 1, role: { value: 'link' }, name: { value: 'Learn more' }, backendDOMNodeId: 40 },
  ], { 'DOM.getBoxModel': () => ({ model: { content: [0, 0, 10, 0, 10, 10, 0, 10] } }) });
  await vm.runInContext('execute', context)('conversa-nome',
    { acao: 'clicar', argumentos: { nome: 'learn more' }, consequencia: 'nenhuma' });
  assert.ok(chamadas.includes('Input.dispatchMouseEvent'));
});

test('o site trocar so o endereco nao zera as refs', async () => {
  const { context } = ambiente();
  let url = 'https://example.org/busca';
  comPagina(context, [{ nodeId: 1, role: { value: 'link' }, name: { value: 'Proxima' }, backendDOMNodeId: 40 }], {
    'Runtime.evaluate': args => ({ result: { value: args.expression.startsWith('({url:')
      ? { url, titulo: 'Teste' } : true } }),
  });
  const execute = vm.runInContext('execute', context);
  await execute('conversa-url', { acao: 'navegar', argumentos: { url: 'https://example.org/busca' } });
  url = 'https://example.org/busca?pagina=1';
  const depois = await execute('conversa-url', { acao: 'snapshot', argumentos: {} });
  assert.equal(depois.dados.modo, 'diff', 'mesma pagina: vem so o que mudou');
});

test('texto do link depois dele e datas picadas saem limpos', async () => {
  const { context } = ambiente();
  comPagina(context, [
    { nodeId: 1, ignored: true, role: { value: 'RootWebArea' }, childIds: [2, 4, 5, 6, 7] },
    { nodeId: 2, parentId: 1, role: { value: 'link' }, name: { value: 'Pagina principal' }, backendDOMNodeId: 9, childIds: [3] },
    { nodeId: 3, parentId: 2, ignored: true, role: { value: 'generic' }, childIds: [8] },
    { nodeId: 8, parentId: 3, role: { value: 'StaticText' }, name: { value: 'Pagina principal' } },
    { nodeId: 4, parentId: 1, role: { value: 'StaticText' }, name: { value: '27' } },
    { nodeId: 5, parentId: 1, role: { value: 'StaticText' }, name: { value: 'de' } },
    { nodeId: 6, parentId: 1, role: { value: 'StaticText' }, name: { value: 'setembro' } },
    { nodeId: 7, parentId: 1, role: { value: 'heading' }, name: { value: 'Fim' } },
  ]);
  const lida = await vm.runInContext('execute', context)('conversa-limpa', { acao: 'snapshot', argumentos: {} });
  assert.equal(JSON.stringify(lida.dados.arvore.map(item => item.name)),
    JSON.stringify(['Pagina principal', '27 de setembro', 'Fim']));
});
