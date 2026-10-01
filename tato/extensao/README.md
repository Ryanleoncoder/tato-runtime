# Extensão do Tato para Chrome

A extensão opera no perfil de Chrome já aberto. Para cada conversa, cria uma
aba e a coloca num grupo identificado pelo nome do cliente. Ao criar ou reabrir
a aba, traz o Chrome para a frente. As ações seguintes usam a mesma aba sem
mudar o foco. Fechar a aba interrompe o acesso; reiniciar o servidor não a
fecha.

Uma borda, uma seta e um balão mostram as ações na aba criada, com as mesmas
cores para qualquer cliente. Essas camadas não aparecem em outras abas, não
interceptam cliques e são ocultadas durante a captura de imagem. O aviso de
depuração do Chrome permanece enquanto a aba está em uso.

## Instalação manual

1. Inicie o agente com o Tato registrado: o Tato abre a porta 47812 (`TATO_PORTA`).
2. No Chrome, abra `chrome://extensions`, ative o modo do desenvolvedor,
   escolha "Carregar sem compactação" e selecione esta pasta.
3. A extensão se conecta sozinha em até um minuto, sem código de pareamento:
   o servidor reconhece o ID fixo dela, definido pela `key` do `manifest.json`.

Se o servidor iniciar depois do Chrome, aguarde a próxima tentativa ou use
"Tentar agora" no ícone da extensão.

Sem a extensão conectada, o navegador aguarda: o agente não muda para outro
navegador sozinho.

## Limite de acesso e segurança

A permissão `debugger` é ampla no Chrome. O limite às abas criadas pela
extensão é aplicado pelo código, não pela permissão: ele só guarda IDs
retornados por `chrome.tabs.create`, não aceita IDs enviados pelo agente e não
assume outra aba quando a sua é fechada. A conexão WebSocket só aceita a
extensão pelo ID fixo e, depois do primeiro contato, por um token guardado
nela. O servidor valida URLs e redirecionamentos antes da navegação.

A integração ainda exige verificação manual em páginas com pop-ups, downloads
e autenticação. Revise a permissão antes de instalar. A extensão atende
Chrome/Chromium; outros navegadores não são suportados.
