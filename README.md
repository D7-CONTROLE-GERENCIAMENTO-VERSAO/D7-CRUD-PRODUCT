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
