// Mostra borda, seta e descrição da ação na aba controlada.
// O visual é o mesmo para qualquer cliente.
// Shadow DOM e aria-hidden retiram a camada da leitura de acessibilidade.
// pointer-events permite clicar na página abaixo da camada.
// A borda depende do sinal de vida; a seta e o balão somem após a ação.
(() => {
  if (window.__tato) return;
  const APAGAR_MS = 8000;
  const VIVO_MS = 60000;
  let raiz = null, acao = null, seta = null, balao = null, onda = null, campo = null, borda = null, host = null,
    timer = null, vigia = null, ultimoVivo = 0;

  const CSS = `
    :host { all: initial; }
    .tudo { position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;
            font-family: system-ui, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
    .acao { opacity: 0; transition: opacity 300ms ease; }
    .acao.ligada { opacity: 1; }
    .borda { position: fixed; inset: 0; opacity: 0; transition: opacity 300ms ease;
             border: 3px solid transparent; animation: pulsoTato 2.4s ease-in-out infinite;
             border-image: linear-gradient(90deg, #36e2f5, #2f5bff, #8b3dff, #e24bd8, #ff7a59) 1; }
    .borda.viva { opacity: 1; }
    @keyframes pulsoTato {
      0%,100% { box-shadow: inset 0 0 18px rgba(139,61,255,.35); }
      50% { box-shadow: inset 0 0 30px rgba(139,61,255,.6); }
    }
    .seta { position: fixed; left: 0; top: 0; width: 0; height: 0;
            transition: transform 450ms cubic-bezier(.45,0,.2,1); }
    .seta svg { position: absolute; left: -4px; top: -4px;
      filter: drop-shadow(0 0 4px rgba(139,61,255,.55)) drop-shadow(0 3px 4px rgba(0,0,0,.3)); }
    .balao { position: absolute; left: 28px; top: 26px; white-space: nowrap; max-width: 360px;
             overflow: hidden; text-overflow: ellipsis; font-size: 12px; font-weight: 600;
             color: #fafaf9; background: #18181b; border-radius: 6px; padding: 4px 8px;
             box-shadow: 0 0 0 1px #8b3dff, 0 2px 6px rgba(0,0,0,.2); }
    .onda { position: absolute; left: -16px; top: -16px; width: 32px; height: 32px;
            border-radius: 50%; border: 2px solid #8b3dff; opacity: 0; }
    .onda.viva { animation: clique 700ms ease-out 2; }
    @keyframes clique { 0% { transform: scale(.4); opacity: .9; } 100% { transform: scale(1.8); opacity: 0; } }
    .campo { position: fixed; display: none; border-radius: 4px;
             box-shadow: 0 0 0 2px #8b3dff, 0 0 0 5px rgba(139,61,255,.3); }
    .campo.ativo { display: block; }
    /* Quem pediu menos movimento ao sistema: a seta pula, a borda fica
       parada e o clique nao faz onda. O que ela diz continua igual. */
    @media (prefers-reduced-motion: reduce) {
      .acao, .borda, .seta { transition: none; }
      .borda { animation: none; box-shadow: inset 0 0 18px rgba(139,61,255,.35); }
      .onda.viva { animation: none; }
    }
  `;
  // Seta do agente, distinta do cursor do sistema.
  // Montadas no DOM, nao por innerHTML: sites com Trusted Types recusariam.
  function desenharSeta() {
    const NS = 'http://www.w3.org/2000/svg';
    const el = (tag, attrs) => {
      const no = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) no.setAttribute(k, v);
      return no;
    };
    const tato = el('svg', { width: '30', height: '30', viewBox: '0 0 24 24' });
    const degrade = el('linearGradient', { id: 'tato-degrade', x1: '0', y1: '0', x2: '1', y2: '0' });
    for (const [pos, cor] of [['0', '#2cd4f5'], ['.35', '#1f6bff'], ['.55', '#2a3cf0'], ['.8', '#8b3dff'], ['1', '#e04be8']]) {
      degrade.append(el('stop', { offset: pos, 'stop-color': cor }));
    }
    const defs = el('defs', {});
    defs.append(degrade);
    tato.append(defs, el('path', { d: 'M4.8 4.3L20.6 14.2L11.8 14.6L8.8 20.6z', fill: 'url(#tato-degrade)',
                                   stroke: 'url(#tato-degrade)', 'stroke-width': '3.8', 'stroke-linejoin': 'round' }));
    return tato;
  }

  // Folha construida: CSP que recusa <style> inline nao a alcanca.
  function aplicarEstilo(sombra) {
    try {
      const folha = new CSSStyleSheet();
      folha.replaceSync(CSS);
      sombra.adoptedStyleSheets = [folha];
    } catch (e) {
      const estilo = document.createElement('style');
      estilo.textContent = CSS;
      sombra.append(estilo);
    }
  }

  function montar() {
    if (host && host.isConnected) return;
    host = document.createElement('tato-sobreposicao');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
    const sombra = host.attachShadow({ mode: 'closed' });
    aplicarEstilo(sombra);
    raiz = document.createElement('div');
    raiz.className = 'tudo';
    acao = document.createElement('div');
    acao.className = 'acao';
    borda = document.createElement('div');
    borda.className = 'borda';
    campo = document.createElement('div');
    campo.className = 'campo';
    seta = document.createElement('div');
    seta.className = 'seta';
    onda = document.createElement('span');
    onda.className = 'onda';
    balao = document.createElement('span');
    balao.className = 'balao';
    seta.append(onda, desenharSeta(), balao);
    acao.append(campo, seta);
    raiz.append(borda, acao);
    sombra.append(raiz);
    document.documentElement.appendChild(host);
  }

  function vivo() {
    montar();
    ultimoVivo = Date.now();
    borda.classList.add('viva');
    if (!vigia) vigia = setInterval(() => {
      if (Date.now() - ultimoVivo > VIVO_MS && borda) borda.classList.remove('viva');
    }, 5000);
  }

  function acender() {
    acao.classList.add('ligada');
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (acao) acao.classList.remove('ligada');
      if (campo) campo.classList.remove('ativo');
    }, APAGAR_MS);
  }

  window.__tato = {
    // Guarda o nome do cliente conectado, para o diagnóstico.
    usar(nome) {
      window.__tatoAgente = String(nome || '');
      return true;
    },
    // `caixa` ({x, y, width, height}) so vem no digitar: o campo ganha
    // contorno enquanto o texto entra. Qualquer outra acao o apaga.
    apontar(x, y, texto, clique, caixa) {
      vivo();
      if (caixa) {
        Object.assign(campo.style, { left: `${Math.round(caixa.x)}px`, top: `${Math.round(caixa.y)}px`,
                                     width: `${Math.round(caixa.width)}px`, height: `${Math.round(caixa.height)}px` });
        campo.classList.add('ativo');
      } else {
        campo.classList.remove('ativo');
      }
      seta.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
      balao.textContent = String(texto || '');
      onda.classList.remove('viva');
      if (clique) { void onda.offsetWidth; onda.classList.add('viva'); }
      acender();
      return true;
    },
    // O print mostra a pagina, nao a sobreposicao: some antes e volta depois.
    esconder() { if (host) host.style.display = 'none'; },
    mostrar() { if (host) host.style.display = ''; },
    // O sinal de vida da extensao: a aba continua do agente.
    vivo,
    // A acao acabou e o modelo decide a seguinte: a seta fica onde estava e o
    // balao diz isso, ate a proxima acao ou o fim do turno.
    pensar() {
      if (!acao) return;
      vivo();
      balao.textContent = 'pensando no próximo passo…';
      onda.classList.remove('viva');
      acao.classList.add('ligada');
      clearTimeout(timer);
    },
    // Fim do turno: a seta e o balao saem; a borda fica, a aba continua dele.
    soltar() {
      if (acao) acao.classList.remove('ligada');
      if (campo) campo.classList.remove('ativo');
      clearTimeout(timer);
    },
    // Fim da sessao: sai tudo.
    encerrar() {
      this.soltar();
      if (borda) borda.classList.remove('viva');
      ultimoVivo = 0;
    },
    // Para o teste e o diagnostico: a sombra e fechada de proposito.
    estado() {
      if (!raiz) return null;
      const ativo = campo.classList.contains('ativo');
      return { ligada: acao.classList.contains('ligada'), borda: borda.classList.contains('viva'),
               texto: balao.textContent, agente: window.__tatoAgente || '',
               posicao: seta.style.transform, visivel: host.style.display !== 'none',
               campo: ativo ? { left: campo.style.left, top: campo.style.top,
                                width: campo.style.width, height: campo.style.height } : null,
               animacaoDaBorda: getComputedStyle(borda).animationName,
               transicaoDaSeta: getComputedStyle(seta).transitionDuration };
    },
  };

  // Pagina nova numa aba do agente: a borda ja nasce acesa.
  if (document.documentElement) vivo();
  else document.addEventListener('DOMContentLoaded', vivo, { once: true });
})();
