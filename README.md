# Auditoria de Fretes — Linha Evolux

MVP web privado para cadastro de produtos, anúncios, modelos e auditoria de custos de frete. Python 3.12, Flask, PostgreSQL e HTML responsivo. O repositório era composto apenas por este README antes da implementação.

Ambiente de demonstração no Render: [Auditoria de Fretes](https://auditoria-fretes-evolux.onrender.com). Callback OAuth real: `https://auditoria-fretes-evolux.onrender.com/oauth/callback`. Login e configuração ficam privados; nenhuma credencial deve ser publicada neste repositório.

## O que está disponível

- Login de um administrador por senha com hash scrypt; sessões opacas no banco, expiração de 1 hora, rotação no login e logout. Cinco falhas bloqueiam o login por 15 minutos.
- Formulários protegidos por CSRF, cookies HttpOnly/SameSite e Secure em produção, validação de origem/host e cabeçalhos de segurança.
- Produto identificado por SKU e anúncio identificado por MLB, com vários MLBs para o mesmo SKU. Cadastros repetidos atualizam produto, modelo ou anúncio. Um anúncio com fretes não pode mudar de SKU.
- Biblioteca interna de modelos (nome, título, descrição), vinculada aos anúncios. Não publica nem altera anúncios no Mercado Livre.
- Dimensões reais da embalagem em cm e peso em kg por produto; consulta das dimensões de embalagem retornadas pela API por anúncio, sem confundir com dimensões do produto. Revisão dimensional manual.
- Registro de frete esperado e cobrado por referência única de pedido/envio, data e origem da evidência. Diferença em centavos = cobrado − esperado; painel com total cobrado, excesso e divergências. Valores negativos indicam abaixo do esperado.
- OAuth Authorization Code com PKCE S256 e state aleatório, ligado à sessão, válido por 10 minutos e de uso único. Verificação do vendedor em `/users/me`. Tokens e verifier criptografados com Fernet no banco, nunca enviados ao navegador.
- Renovação de token sob bloqueio de linha PostgreSQL; credenciais rotacionadas salvas antes de consultar o anúncio. Consulta autenticada de `/items/{MLB}` com checagem de propriedade. Desconexão local remove credenciais.
- Importação de todos os anúncios da conta por scan/scroll, inclusive contas com mais de 1.000 anúncios; consulta de detalhes em lotes e cotação atual de envio por MLB. Progresso em segundo plano, cancelamento e resultados paginados. A importação não altera anúncios nem sobrescreve medidas internas.
- SKU da API (`SELLER_SKU`) separado de MLB e de `seller_custom_field`; SKUs de variações preservados. Campos ausentes ficam explicitamente pendentes. A cotação é obtida por `/users/{id}/shipping_options/free`, em BRL; um zero real é preservado, ausência não vira zero.
- Comparação estimada entre a cotação atual do anúncio e a cotação atual com a embalagem interna de referência do SKU. Sem embalagem interna, não se inventa um frete esperado. Nenhuma estimativa é registrada como débito de venda.
- Seção **Minhas medidas por SKU** em `/catalog/measures`: o operador preenche largura, altura e profundidade em cm e peso bruto em kg para cada SKU importado. Os campos começam vazios e não recebem dimensões da API automaticamente. As medidas salvas são preservadas nas importações e podem ser corrigidas.
- **Comparação de fretes atuais** em `/catalog/comparison`: custo atual informado pela API para cada anúncio versus frete esperado recalculado pela API oficial com as medidas do operador, preço, modalidade e logística atuais. Cada SKU de variação tem seu próprio resultado, mesmo quando compartilha MLB com outros SKUs. Editar medidas invalida o cálculo anterior até atualizar. Ausência de medidas ou falha de cotação não vira zero.
- Medidas retornadas pelo Mercado Livre continuam disponíveis somente para consulta em `/catalog/api-measures`. Não substituem as medidas de referência cadastradas pelo operador. Profundidade corresponde ao comprimento na API.

Não há reconciliação automática de extratos/pedidos/envios, publicação de anúncios ou múltiplos usuários neste MVP. Uma diferença estimada não comprova cobrança indevida: tarifas e descontos variam ao longo do tempo. Débitos de vendas continuam exigindo comprovante no histórico de fretes. Quando a API não retornar dimensões ou cotações, o painel informa a ausência. O cadastro de embalagem interna continua sendo responsabilidade do operador.

## Preparação local

Crie um ambiente Python 3.12 e instale `pip install -r requirements-dev.txt`. Execute `python tools/generate_config.py` para criar hash da senha e chave de criptografia. A senha é solicitada sem aparecer na tela; os valores gerados são segredos e não devem ser salvos no Git.

Configure as variáveis em seu terminal ou gerenciador de segredos (o app não carrega `.env` automaticamente). Para desenvolvimento local: `APP_ENV=development`, `PUBLIC_BASE_URL=http://localhost:5000`, `DATABASE_URL=sqlite:///local.db`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `TOKEN_ENCRYPTION_KEY`. OAuth pode ficar desabilitado deixando os três campos MELI vazios. Rode `flask --app app run --host 127.0.0.1 --port 5000`. SQLite é somente para desenvolvimento; produção rejeita SQLite.

Teste com `python -m pytest -q`. Os testes usam banco SQLite temporário e respostas de API simuladas, não credenciais reais. A verificação final PostgreSQL/OAuth depende do ambiente implantado. Confira dependências com `python -m pip_audit -r requirements.txt`.

## Deploy no Render

O serviço deve usar a branch que contém esta implementação. Antes de criar o serviço, confira se `app.py`, `requirements.txt` e `render.yaml` estão presentes no GitHub. Publique apenas os arquivos de código, testes, configuração e documentação; nunca `.env`, banco, tokens, logs ou a pasta `.venv`.

1. Crie um PostgreSQL no Render ou outro provedor com armazenamento persistente e conexão segura. Anote a URL privada se for no mesmo Render/região. Banco externo deve exigir TLS conforme o provedor (por exemplo `?sslmode=require`).
2. Após publicar os arquivos, escolha **New → Web Service → Public Git Repository** e informe `https://github.com/correacvarte-maker/auditoria-fretes-evolux`. Branch `main`; runtime Python; build `pip install -r requirements.txt`; start `gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT --access-logfile /dev/null app:app`; health check `/health`. A configuração equivalente está em `render.yaml`, caso prefira Blueprint.
3. Configure as variáveis abaixo. Use o nome real do serviço exibido no painel para preencher `PUBLIC_BASE_URL`. Se a URL ainda não foi atribuída, crie o serviço, copie a URL e ajuste a variável antes do deploy bem-sucedido. O app recusa configuração incompleta e não abre acesso público por padrão.
4. Copie a URL pública que o Render efetivamente atribuiu, por exemplo o formato `https://<nome-real>.onrender.com`. **O formato é exemplo, não uma URL já criada.** Atualize `PUBLIC_BASE_URL` com a origem exata, sem caminho ou barra final, e reimplante. Se mudar para domínio personalizado, atualize também a redirect URI.
5. Cadastre uma aplicação Mercado Livre (não Mercado Pago), habilite PKCE e configure a **redirect URI exata**: `<PUBLIC_BASE_URL>/oauth/callback`. Preencha `MELI_CLIENT_ID` e `MELI_CLIENT_SECRET` nos campos privados do Render. Não cole segredos em issue, README, chat ou barra de endereços do navegador.
6. Abra `/health`, depois o painel, faça login e clique em **Conectar conta**. Autorize no Mercado Livre e confirme no painel o nome e o ID da conta que deseja auditar. A confirmação expira em 10 minutos e depende da mesma sessão administrativa; tokens pendentes ficam criptografados no banco. A conta confirmada fica fixada para consultas e reconexões. Para trocar de vendedor, primeiro desconecte e depois autorize e confirme a nova conta. Se `MELI_SELLER_ID` estiver definido, o sistema exige aquele ID e conecta diretamente após conferir a identidade. Cadastre SKU e MLB da conta; consulte o anúncio. Registre um frete com seu comprovante e confira os totais.

| Variável | Valor / propósito |
| --- | --- |
| `APP_ENV` | `production` |
| `PUBLIC_BASE_URL` | Origem HTTPS real do serviço; utilizada no OAuth e na validação de host |
| `DATABASE_URL` | URL PostgreSQL persistente, segredo; `postgres://`, `postgresql://` e `postgresql+psycopg://` aceitos |
| `ADMIN_USERNAME` | Nome de login do administrador |
| `ADMIN_PASSWORD_HASH` | Hash scrypt gerado localmente, nunca a senha em texto |
| `TOKEN_ENCRYPTION_KEY` | Chave Fernet gerada localmente; preservar entre deploys |
| `MELI_CLIENT_ID` | ID da aplicação Mercado Livre |
| `MELI_CLIENT_SECRET` | Segredo da aplicação, somente no Render |
| `MELI_SELLER_ID` | Opcional: ID numérico pré-definido da conta permitida. Sem esta variável, o administrador confirma a conta identificada pelo OAuth antes da conexão |
| `PYTHON_VERSION` | `3.12.15`, definido no Blueprint; confirme disponibilidade no runtime do Render |

Os três campos MELI podem ficar vazios no primeiro deploy para obter a URL; conexão e consulta ficam desabilitadas. Não invente uma URL de callback e não use a URL do GitHub como redirect URI. Nenhuma credencial real é necessária para testes locais.

## Cuidados operacionais e limites do plano grátis

Render Free é adequado para demonstração: o web service pode dormir após 15 minutos de inatividade e o PostgreSQL gratuito expira após 30 dias. Arquivos locais são efêmeros; por isso este app exige PostgreSQL em produção. Para operação contínua, escolha armazenamento que não expire e um serviço adequado, confirme custos no painel e mantenha backups. O Blueprint cria somente o serviço web grátis, não contrata banco pago.

O app cria tabelas no primeiro início. Execute uma única instância e um worker Gunicorn para este MVP; futuras alterações de esquema precisam de migrações revisadas. A consulta de API usa timeout de 20 segundos. A importação usa uma tarefa em segundo plano no processo web, com progresso persistido no PostgreSQL, retries limitados para HTTP 429 e rotação de refresh sob bloqueio. Se o processo reiniciar ou ficar sem progresso por cinco minutos, inicie novamente; os resultados anteriores permanecem identificados por data e execução, sem exclusão automática de anúncios ausentes. Uma importação completa atualiza observações, não constitui um histórico financeiro. Falhas de refresh exigem reconectar. O bloqueio global de login protege o único administrador, mas um atacante pode causar bloqueios temporários; para operação com vários usuários, substitua por identidade gerenciada e controle de acesso.

Depois de conectar a conta, importe todos os anúncios. Abra **Minhas medidas por SKU**, procure o SKU, preencha as medidas corretas da embalagem e salve. Clique em **Atualizar e comparar** para consultar os custos atuais e recalcular os esperados. A página de progresso acompanha o trabalho; **Comparação de fretes atuais** mostra custo atual, suas medidas, frete esperado e diferença por SKU/MLB. SKU com várias variações ou anúncios permanece separado por resultado. A referência usa o cálculo atual do Mercado Livre, sem uma tabela estática de tarifas que possa ficar desatualizada. Não há consulta de débitos históricos de vendas neste fluxo.

Sem chave Fernet, tokens salvos não podem ser recuperados. Ao trocar a chave, primeiro desconecte a conta e depois reconecte; uma rotação geral precisa de migração de credenciais. Ao trocar a senha/hash, invalide também as sessões existentes no banco. Restrinja acesso ao painel Render e ao banco, mantenha backups protegidos e nunca habilite debug em produção. O comando de start desativa logs de acesso para não registrar query strings com códigos OAuth; mantenha essa opção ou configure redação desses parâmetros em qualquer proxy/monitoramento próprio. O provedor de hospedagem também pode registrar tráfego: revise retenção e acesso.

Desconectar remove tokens locais; revogar autorização na conta Mercado Livre encerra o acesso na plataforma. A API pode exigir permissões adicionais e retornar 403; não substitua por coleta de páginas nem por credenciais no cliente.

## Fontes oficiais

- [Mercado Livre: autenticação e autorização](https://developers.mercadolivre.com.br/autenticacao-e-autorizacao)
- [Mercado Livre: itens e buscas](https://developers.mercadolivre.com.br/pt_br/itens-e-buscas)
- [Mercado Livre: atributos](https://developers.mercadolivre.com.br/pt_br/api-docs-pt-br/atributos)
- [Mercado Livre: custos de envio e cotações](https://developers.mercadolivre.com.br/pt_br/produto-consulta-de-usuarios/custos-de-envio)
- [Mercado Livre: variações e SELLER_SKU](https://developers.mercadolivre.com.br/pt_br/variacoes)
- [Render: web services](https://render.com/docs/web-services)
- [Render: limites gratuitos e persistência](https://render.com/docs/free)

**Pendente para ativar a integração:** concluir o cadastro da aplicação Mercado Livre, configurar suas credenciais e o vendedor no Render e validar autorização e consulta com a conta correta. O serviço web e o banco foram criados em 14/10/2026; o banco gratuito expira em 13/11/2026.
# Importação em massa e tabela de referência (14/10/2026)

Em **Minhas medidas por SKU → Importar planilha de medidas em massa**, envie `.xlsx` (uma aba) ou `.csv` de até 2 MB / 10.000 linhas, com cabeçalho `sku;largura;altura;comprimento;peso bruto`. Medidas em cm; peso bruto em kg; SKU como texto para preservar zeros. Valores positivos, até 2 casas nas medidas e 3 no peso. Use somente SKUs importados da conta conectada. A prévia valida o arquivo inteiro e lista os erros sem alterar os cadastros. A confirmação substitui as medidas existentes e salva o lote numa transação. Prévia vinculada à sessão e ao vendedor, expira em 10 minutos.

O cálculo de referência escolhido pelo usuário é `peso_cubico = largura * altura * comprimento / 6000`, em kg; `peso_considerado = max(peso_cubico, peso_bruto)`. Sem arredondar antes da seleção da faixa, aplica-se a coluna **A partir de R$ 200** da [tabela oficial](https://www.mercadolivre.com.br/ajuda/40538), capturada em 14/10/2026, inclusive a anúncios de preço menor como referência fixa solicitada. Os limites superiores são inclusivos; acima de 150 kg usa-se a última linha. Os preços já incluem os descontos descritos na tabela. A coluna foi versionada em `freight_table.py` e não se atualiza sozinha: alterações publicadas pelo Mercado Livre precisam ser conferidas e incorporadas.

O painel `/catalog/comparison` recalcula imediatamente depois de salvar/importar medidas e mostra peso bruto, cúbico, considerado, faixa, tarifa esperada, frete atual do anúncio e diferença (`atual - esperado`). A consulta ao Mercado Livre atualiza apenas o lado atual. Não consulta pedidos/vendas para essa comparação. Uma diferença indica revisão da referência; categorias/logísticas especiais podem ter outras regras. Um anúncio com vários SKUs gera uma linha por SKU, reutilizando o custo consultado do anúncio; não se inventa uma tarifa atual específica de variação.

Esta regra de tabela substitui, no catálogo, a comparação anterior de cotação por API com medidas internas descrita abaixo. Os módulos legados de cadastro e fretes manuais continuam disponíveis.

