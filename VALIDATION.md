# Validação do MVP — 14/10/2026

- Acesso de leitura ao repositório confirmado; branch `main`, revisão inicial `d109ff4686f676872ab901bea3a716fa05a87f18`.
- Nove testes automatizados passaram após atualizar as dependências: login/rotação/logout e cabeçalhos; CSRF/origem/host/bloqueio; expiração de sessão; valores monetários/duplicidade/escape de HTML; vários MLBs por SKU e proteção do histórico; OAuth ligado à sessão/de uso único/criptografia; expiração de state e vendedor incorreto; renovação de token/propriedade do anúncio; recusa de SQLite em produção.
- `pip-audit -r requirements.txt`: nenhuma vulnerabilidade conhecida encontrada na consulta realizada. Isso é uma checagem de dependências, não uma auditoria completa do sistema.
- Modelos e páginas foram exercitados pelo cliente de testes Flask. PostgreSQL real, locks concorrentes, runtime Linux/Gunicorn, layout em navegador e API real do Mercado Livre ainda precisam de validação no ambiente de deploy.
- Nenhum token ou segredo real foi usado. Os dados nos testes são artificiais. Credenciais geradas ou configuradas depois devem ficar exclusivamente no ambiente de execução.
- Publicação no GitHub autorizada pelo proprietário em 14/10/2026. Nenhum deploy foi realizado durante esta validação. Serviço Render ainda não criado pelo usuário; URL pública e redirect URI reais pendentes.
