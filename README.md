# Loja Virtual — CRUD de Produtos (Web + CLI)

Aplicação CRUD para gerenciamento de produtos de uma loja virtual com interface **Web** (`index.html`) e **CLI** (`cli.py`) consumindo a mesma **API REST** (`api.py`) e banco **PostgreSQL** (`db.py`), executada integralmente via **Docker Compose**.

---

## Issue #1 — Estrutura Web, Autenticação e Logs

### 1. Como iniciar a aplicação (Web, API e Banco)

Certifique-se de ter apenas **Docker** e **Docker Compose** instalados no host. Execute na raiz do projeto:

```bash
docker compose up -d --build
```

- **Interface Web:** acesse `http://localhost:8000/` no navegador.
- **Healthcheck Web:** `GET http://localhost:8000/api/web/health` (retorna HTTP 200 `{"status":"ok","client":"web"}`).

---

### 2. Configuração de Autenticação e Usuário Inicial

Na inicialização (`db.init_db`), o sistema cria automaticamente as tabelas `users` e `products` e configura o usuário inicial. A senha nunca é salva nem registrada em texto puro (é armazenada com hash SHA-256 + salt).

As credenciais iniciais podem ser configuradas por variáveis de ambiente no `docker-compose.yml` (valores padrão de desenvolvimento abaixo):

| Variável | Valor Padrão (Dev) | Descrição |
| :--- | :--- | :--- |
| `INITIAL_ADMIN_USER` | `admin` | Usuário inicial criado no banco |
| `INITIAL_ADMIN_PASSWORD` | `admin123` | Senha inicial (convertida em hash SHA-256 no banco) |
| `PASSWORD_SALT` | `loja-virtual-salt` | Salt aplicado ao hash de senha |
| `SECRET_KEY` | `loja-virtual-secret-key` | Chave HMAC-SHA256 para assinatura de tokens |

Para autenticar na Web:
1. Abra `http://localhost:8000/`.
2. Informe o usuário e a senha configurados e clique em **Entrar** (ou envie `POST /api/web/login` com `{"username": "...", "password": "..."}`).
3. Credenciais válidas retornam **HTTP 200** com token assinado; credenciais ausentes ou inválidas retornam **HTTP 401**.

---

### 3. Como consultar os Logs Estruturados (JSON)

Todas as operações emitem uma linha JSON no `stdout` da camada `api` e, quando acessam o banco, uma linha JSON na camada `db` compartilhando o mesmo `request_id`.

Para visualizar os logs em tempo real:

```bash
docker compose logs -f api
```

Exemplo de saída correlacionada de login:

```json
{"timestamp":"2026-11-12T19:30:00Z","level":"INFO","layer":"db","client":"web","operation":"login","status":"success","request_id":"req-a1b2c3d4","message":"Usuario Web autenticado no banco"}
{"timestamp":"2026-11-12T19:30:00Z","level":"INFO","layer":"api","client":"web","operation":"login","status":"success","request_id":"req-a1b2c3d4","message":"Login Web realizado com sucesso"}
```

---

### 4. Como executar os Testes Unitários e verificar 100% de Cobertura

Os testes unitários e a medição de cobertura de linhas rodam dentro do contêiner via Docker Compose:

```bash
docker compose run --rm api pytest --cov=api --cov=db --cov-report=term-missing --cov-fail-under=100
```

---

## Issue #2 — Cadastro e edição de produtos na Web

Depois de autenticar na página inicial, use o formulário **Cadastrar produto**. Nome, categoria, preço e estoque são obrigatórios; a descrição é opcional. O navegador valida os campos antes do envio, e a API também rejeita dados inválidos.

Para cadastrar, preencha os campos e clique em **Cadastrar produto**. A confirmação informa o ID criado. Para editar, informe o ID em **ID do produto para edição**, preencha os dados atualizados e clique em **Usar ID para editar**; depois, envie o formulário em **Salvar alterações**. O formulário exige os dados do produto novamente, pois a listagem e o preenchimento automático serão entregues na Issue #3.

As operações usam `POST /api/web/records` e `PUT /api/web/records/{id}`. Ambas exigem o token Web recebido no login; IDs inexistentes retornam HTTP 404. Os eventos de API e banco são enviados ao stdout com o mesmo `request_id`; consulte-os com:

```bash
docker compose logs -f api
```

Para executar os testes e confirmar 100% de cobertura de linhas em `api.py` e `db.py`:

```bash
docker compose run --rm api pytest --cov=api --cov=db --cov-report=term-missing --cov-fail-under=100
```

---

## Issue #3 — Listagem, Paginação, Ordenação e Exclusão Web de Produtos

### 1. Tabela, Paginação, Ordenação e Exclusão na Interface Web

Ao acessar a interface Web já autenticado ou logo após o login, o **Painel de Produtos** carrega automaticamente a tabela **Produtos Cadastrados** e a mantém sincronizada após cada cadastro, edição ou exclusão:

- **Colunas exibidas:** `ID` (`id`), `Nome` (`name`), `Categoria` (`category`), `Preço` (`price`), `Estoque` (`stock`) e `Ações` (**Selecionar**).
- **Paginação fixa:** exibe **5 itens por página** (`page` a partir de `1` e `page_size=5` fixo), com botões **Anterior** e **Próxima** e indicador da página atual e total de registros.
- **Ordenação:** selecione o campo em **Ordenar por** (`id`, `name`, `category`, `price`, `stock`) e a direção (`asc` ou `desc`), ou clique diretamente nos cabeçalhos ordenáveis da tabela.
- **Seleção e exclusão imediata:** clicar em uma linha da tabela (ou no botão **Selecionar**) destaca o item e preenche os campos do formulário `#product-form` (incluindo descrição) para edição ou exclusão via botão **Excluir selecionado**. Caso a última linha de uma página posterior à primeira seja removida, a interface recua automaticamente para a página anterior.

### 2. Endpoints de Listagem e Exclusão Web

Todas as rotas exigem o cabeçalho `Authorization: Bearer <token>` obtido em `POST /api/web/login`:

- **`GET /api/web/records`**
  - **Parâmetros de query:**
    - `page` (inteiro `>= 1`, padrão `1`)
    - `page_size` (inteiro fixo `= 5`, padrão `5`)
    - `sort_by` (`id`, `name`, `category`, `price` ou `stock`, padrão `id`)
    - `order` (`asc` ou `desc`, padrão `asc`)
  - **Respostas HTTP:**
    - **200:** retorna `{"status": "ok", "items": [...], "total": <int>, "page": <int>, "page_size": 5, "sort_by": "...", "order": "..."}`.
    - **401:** token ausente, malformado ou inválido.
    - **422:** parâmetros de paginação ou ordenação inválidos.

- **`DELETE /api/web/records/{id}`**
  - **Parâmetro de rota:** `id` (inteiro do produto).
  - **Respostas HTTP:**
    - **200:** produto removido imediatamente no banco, retornando `{"status": "ok", "deleted_id": <id>}`.
    - **401:** token ausente, malformado ou inválido.
    - **404:** produto não encontrado.
    - **422:** identificador de rota inválido.

### 3. Logs Correlacionados e Testes com 100% de Cobertura

As requisições de listagem e exclusão emitem logs JSON correlacionados pelo mesmo `request_id` nas camadas `api` e `db`. Consulte os logs em tempo real com:

```bash
docker compose logs -f api
```

Execute a suíte de testes unitários verificando 100% de cobertura em `api.py` e `db.py` com:

```bash
docker compose run --rm api pytest --cov=api --cov=db --cov-report=term-missing --cov-fail-under=100
```

---

## Issue #4 — Busca avançada Web de produtos

No **Painel de Produtos**, use os filtros **Buscar por nome**, **Categoria**, **Preço mínimo** e **Preço máximo** em conjunto ou separadamente. A busca é enviada de forma assíncrona e atualiza a tabela sem recarregar a página. O botão **Limpar filtros** restaura a listagem completa.

O endpoint protegido `GET /api/web/records/search` aceita:

- `search`: trecho do nome do produto, sem diferenciar maiúsculas de minúsculas.
- `category`: categoria exata (`Eletronicos`, `Perifericos`, `Moveis`, `Vestuario` ou `Outros`).
- `min_price` e `max_price`: limites inclusivos da faixa de preço; o mínimo não pode exceder o máximo.
- `page`, `page_size`, `sort_by` e `order`: paginação fixa de 5 itens e ordenação nos mesmos campos aceitos pela listagem Web.

Todos os filtros são opcionais e combinados com `AND`. A busca usa consulta SQL parametrizada em `db.web_search_products`, exige o token Web obtido no login e emite logs JSON correlacionados nas camadas `api` e `db`.

Para executar os testes e confirmar 100% de cobertura de linhas em `api.py` e `db.py`:

```bash
docker compose run --rm api pytest --cov=api --cov=db --cov-report=term-missing --cov-fail-under=100
```

---

## Issue #5 — Exportação CSV Web de Produtos

No **Painel de Produtos**, clique em **Exportar CSV** para baixar o arquivo `produtos.csv` com as colunas `id`, `name`, `category`, `price`, `stock` e `description`:

- Se houver filtros ativos na busca avançada (`search`, `category`, `min_price`, `max_price`), o CSV inclui **todos os registros correspondentes aos filtros**, sem se limitar aos 5 itens da página atual.
- Sem filtros ativos, o CSV exporta **todos os produtos cadastrados** no banco respeitando a ordenação selecionada (`sort_by` e `order`).

O endpoint protegido `GET /api/web/records/export` exige o token Web em `Authorization: Bearer <token>` (retorna HTTP 401 sem autenticação válida e HTTP 422 se `min_price > max_price`), executa `db.web_export_products` e registra logs JSON correlacionados por `request_id` (`operation: "export"`) nas camadas `api` e `db`.

Para consultar os logs e validar 100% de cobertura via Docker Compose:

```bash
docker compose logs -f api
docker compose run --rm api pytest --cov=api --cov=db --cov-report=term-missing --cov-fail-under=100
```

---

## Issue #6 — Dashboard Web de Indicadores

No **Painel de Produtos**, a seção **Dashboard de Indicadores** (`#dashboard-section`) exibe 3 gráficos em barras alimentados em tempo real pelos dados reais agregados no PostgreSQL (`db.web_get_dashboard`) por meio do endpoint protegido `GET /api/web/dashboard`:

1. **Produtos por Categoria (`COUNT(*)`):** exibe a quantidade total de produtos cadastrados em cada categoria (`products_by_category`) e o total geral de produtos (`totals.total_products`).
2. **Estoque Total por Categoria (`SUM(stock)`):** exibe a soma de unidades físicas em estoque agrupada por categoria (`stock_by_category`) e o estoque total consolidado (`totals.total_stock`).
3. **Valor Financeiro em Estoque por Categoria (`SUM(price * stock)`):** exibe o valor financeiro total imobilizado em estoque por categoria (`financial_by_category`) e o patrimônio total em estoque (`totals.total_financial_value`).

Os 3 indicadores são carregados automaticamente ao abrir a página autenticada e atualizados sempre que produtos são cadastrados, editados, excluídos ou recarregados (ou pelo botão **Atualizar indicadores**).

O endpoint `GET /api/web/dashboard` exige autenticação via `Authorization: Bearer <token>` (retornando HTTP 401 quando ausente ou inválido) e registra logs JSON correlacionados pelo mesmo `request_id` (`operation: "dashboard"`) nas camadas `api` e `db`.

Para consultar os logs e validar 100% de cobertura via Docker Compose:

```bash
docker compose logs -f api
docker compose run --rm api pytest --cov=api --cov=db --cov-report=term-missing --cov-fail-under=100
```
