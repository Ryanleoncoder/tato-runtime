const status = document.getElementById('status');
const tentar = document.getElementById('tentar');

function mostrar(conectado, texto) {
  status.textContent = texto || (conectado ? 'Conectado ao Tato.' : 'Procurando o servidor local…');
  tentar.hidden = conectado;
}

async function refresh() {
  const answer = await chrome.runtime.sendMessage({ kind: 'status' });
  mostrar(Boolean(answer.connected));
}

tentar.addEventListener('click', async () => {
  mostrar(false, 'Conectando…');
  const answer = await chrome.runtime.sendMessage({ kind: 'connect' });
  mostrar(Boolean(answer.ok), answer.ok ? '' : 'O servidor local não respondeu. Confira se ele está aberto.');
});

void refresh();
