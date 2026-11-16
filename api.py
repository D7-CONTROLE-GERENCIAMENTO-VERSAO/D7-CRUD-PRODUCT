from contextlib import asynccontextmanager
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
from typing import Annotated, Literal
import uuid

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, StringConstraints

import db

SortField = Literal["id", "name", "category", "price", "stock"]
SortOrder = Literal["asc", "desc"]
ProductCategory = Literal[
    "Eletronicos", "Perifericos", "Moveis", "Vestuario", "Outros"
]


class LoginPayload(BaseModel):
    username: str
    password: str


class ProductPayload(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)
    ]
    category: ProductCategory
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    stock: int = Field(ge=0, le=2_147_483_647)
    description: str = ""


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:8]}"


def log_api(
    client: str,
    operation: str,
    status: str,
    request_id: str,
    message: str,
    level: str = "INFO",
) -> dict:
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": level,
        "layer": "api",
        "client": client,
        "operation": operation,
        "status": status,
        "request_id": request_id,
        "message": message,
    }
    print(json.dumps(entry, ensure_ascii=False), flush=True)
    return entry


def _sign_value(value: str) -> str:
    secret = os.getenv("SECRET_KEY", "loja-virtual-secret-key").encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_web_token(username: str) -> str:
    signature = _sign_value(f"web:{username}")
    return f"{username}.{signature}"


def _verify_signed_token(
    client: str,
    authorization: str | None,
    request_id: str,
    detail: str,
) -> str:
    label = client.upper()
    if not authorization or not authorization.startswith("Bearer "):
        log_api(
            client=client,
            operation="auth",
            status="error",
            request_id=request_id,
            message=f"Token {label} ausente ou malformado",
            level="WARN",
        )
        raise HTTPException(status_code=401, detail=detail)

    raw_token = authorization.removeprefix("Bearer ").strip()
    if "." not in raw_token:
        log_api(
            client=client,
            operation="auth",
            status="error",
            request_id=request_id,
            message=f"Formato de token {label} invalido",
            level="WARN",
        )
        raise HTTPException(status_code=401, detail=detail)

    username, signature = raw_token.split(".", 1)
    expected_sig = _sign_value(f"{client}:{username}")
    if not username or not hmac.compare_digest(signature, expected_sig):
        log_api(
            client=client,
            operation="auth",
            status="error",
            request_id=request_id,
            message=f"Assinatura de token {label} invalida",
            level="WARN",
        )
        raise HTTPException(status_code=401, detail=detail)

    return username


def verify_web_token(authorization: str | None, request_id: str = "req-auth") -> str:
    return _verify_signed_token("web", authorization, request_id, "Nao autorizado")


def create_cli_token(username: str) -> str:
    signature = _sign_value(f"cli:{username}")
    return f"{username}.{signature}"


def verify_cli_token(
    authorization: str | None, request_id: str = "req-cli-auth"
) -> str:
    return _verify_signed_token(
        "cli", authorization, request_id, "Token CLI ausente ou invalido"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db(request_id=new_request_id())
    yield


app = FastAPI(title="Loja Virtual — CRUD Produtos", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def serve_index():
    html_path = Path(__file__).with_name("index.html")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/web/health")
def web_health():
    req_id = new_request_id()
    log_api(
        client="web",
        operation="health",
        status="success",
        request_id=req_id,
        message="Healthcheck Web OK",
    )
    return {"status": "ok", "client": "web"}


@app.post("/api/web/login")
def web_login(payload: LoginPayload):
    req_id = new_request_id()
    authenticated = db.web_authenticate_user(
        payload.username, payload.password, request_id=req_id
    )
    if not authenticated:
        log_api(
            client="web",
            operation="login",
            status="error",
            request_id=req_id,
            message="Credenciais Web invalidas",
            level="WARN",
        )
        raise HTTPException(status_code=401, detail="Credenciais invalidas")

    token = create_web_token(payload.username)
    log_api(
        client="web",
        operation="login",
        status="success",
        request_id=req_id,
        message="Login Web realizado com sucesso",
    )
    return {"status": "ok", "token": token, "username": payload.username}


@app.post("/api/web/records", status_code=201)
def web_create_record(
    payload: ProductPayload,
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    product = db.web_create_product(
        name=payload.name,
        category=payload.category,
        price=payload.price,
        stock=payload.stock,
        description=payload.description,
        request_id=req_id,
    )
    log_api(
        client="web",
        operation="create",
        status="success",
        request_id=req_id,
        message="Produto criado",
    )
    return product


@app.put("/api/web/records/{product_id}")
def web_update_record(
    product_id: int,
    payload: ProductPayload,
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    product = db.web_update_product(
        product_id=product_id,
        name=payload.name,
        category=payload.category,
        price=payload.price,
        stock=payload.stock,
        description=payload.description,
        request_id=req_id,
    )
    if product is None:
        log_api(
            client="web",
            operation="update",
            status="error",
            request_id=req_id,
            message="Produto nao encontrado",
            level="WARN",
        )
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    log_api(
        client="web",
        operation="update",
        status="success",
        request_id=req_id,
        message="Produto atualizado",
    )
    return product


@app.get("/api/web/records")
def web_list_records(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=5, ge=5, le=5),
    sort_by: SortField = Query(default="id"),
    order: SortOrder = Query(default="asc"),
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    result = db.web_list_products(
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        order=order,
        request_id=req_id,
    )
    log_api(
        client="web",
        operation="list",
        status="success",
        request_id=req_id,
        message="Produtos listados",
    )
    return {
        "status": "ok",
        "items": result["items"],
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "order": order,
    }


@app.get("/api/web/records/search")
def web_search_records(
    search: str | None = Query(default=None, max_length=120),
    category: ProductCategory | None = Query(default=None),
    min_price: Decimal | None = Query(default=None, ge=0),
    max_price: Decimal | None = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=5, ge=5, le=5),
    sort_by: SortField = Query(default="id"),
    order: SortOrder = Query(default="asc"),
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=422,
            detail="O preco minimo nao pode ser maior que o preco maximo",
        )

    result = db.web_search_products(
        search=search,
        category=category,
        min_price=min_price,
        max_price=max_price,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        order=order,
        request_id=req_id,
    )
    log_api(
        client="web",
        operation="search",
        status="success",
        request_id=req_id,
        message="Produtos buscados",
    )
    return {
        "status": "ok",
        "items": result["items"],
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "order": order,
    }


@app.get("/api/web/records/export")
def web_export_records(
    search: str | None = Query(default=None, max_length=120),
    category: ProductCategory | None = Query(default=None),
    min_price: Decimal | None = Query(default=None, ge=0),
    max_price: Decimal | None = Query(default=None, ge=0),
    sort_by: SortField = Query(default="id"),
    order: SortOrder = Query(default="asc"),
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=422,
            detail="O preco minimo nao pode ser maior que o preco maximo",
        )

    items = db.web_export_products(
        search=search,
        category=category,
        min_price=min_price,
        max_price=max_price,
        sort_by=sort_by,
        order=order,
        request_id=req_id,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["id", "name", "category", "price", "stock", "description"])
    for item in items:
        writer.writerow(
            [
                item["id"],
                item["name"],
                item["category"],
                f"{item['price']:.2f}",
                item["stock"],
                item["description"],
            ]
        )

    log_api(
        client="web",
        operation="export",
        status="success",
        request_id=req_id,
        message="Exportacao CSV Web concluida",
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="produtos.csv"'},
    )


@app.delete("/api/web/records/{product_id}")
def web_delete_record(
    product_id: int,
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    deleted = db.web_delete_product(product_id=product_id, request_id=req_id)
    if not deleted:
        log_api(
            client="web",
            operation="delete",
            status="error",
            request_id=req_id,
            message="Produto nao encontrado",
            level="WARN",
        )
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    log_api(
        client="web",
        operation="delete",
        status="success",
        request_id=req_id,
        message="Produto removido",
    )
    return {"status": "ok", "deleted_id": product_id}


@app.get("/api/web/dashboard")
def web_get_dashboard(
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_web_token(authorization, request_id=req_id)
    dashboard = db.web_get_dashboard(request_id=req_id)
    log_api(
        client="web",
        operation="dashboard",
        status="success",
        request_id=req_id,
        message="Dashboard Web carregado",
    )
    return {"status": "ok", **dashboard}


@app.get("/api/cli/health")
def cli_health(
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    if authorization is not None:
        verify_cli_token(authorization, request_id=req_id)
    log_api(
        client="cli",
        operation="health",
        status="success",
        request_id=req_id,
        message="Healthcheck CLI OK",
    )
    return {"status": "ok", "client": "cli"}


@app.post("/api/cli/token")
def cli_issue_token(payload: LoginPayload):
    req_id = new_request_id()
    authenticated = db.cli_authenticate_user(
        payload.username, payload.password, request_id=req_id
    )
    if not authenticated:
        log_api(
            client="cli",
            operation="token",
            status="error",
            request_id=req_id,
            message="Credenciais CLI invalidas",
            level="WARN",
        )
        raise HTTPException(status_code=401, detail="Credenciais invalidas")

    token = create_cli_token(payload.username)
    log_api(
        client="cli",
        operation="token",
        status="success",
        request_id=req_id,
        message="Token CLI emitido com sucesso",
    )
    return {
        "status": "ok",
        "token": token,
        "token_type": "Bearer",
        "username": payload.username,
    }


@app.post("/api/cli/records", status_code=201)
def cli_create_record(
    payload: ProductPayload,
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_cli_token(authorization, request_id=req_id)
    product = db.cli_create_product(
        name=payload.name,
        category=payload.category,
        price=payload.price,
        stock=payload.stock,
        description=payload.description,
        request_id=req_id,
    )
    log_api(
        client="cli",
        operation="create",
        status="success",
        request_id=req_id,
        message="Produto criado via CLI",
    )
    return product


@app.put("/api/cli/records/{product_id}")
def cli_update_record(
    product_id: int,
    payload: ProductPayload,
    authorization: str | None = Header(default=None),
):
    req_id = new_request_id()
    verify_cli_token(authorization, request_id=req_id)
    product = db.cli_update_product(
        product_id=product_id,
        name=payload.name,
        category=payload.category,
        price=payload.price,
        stock=payload.stock,
        description=payload.description,
        request_id=req_id,
    )
    if product is None:
        log_api(
            client="cli",
            operation="update",
            status="error",
            request_id=req_id,
            message="Produto nao encontrado na CLI",
            level="WARN",
        )
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    log_api(
        client="cli",
        operation="update",
        status="success",
        request_id=req_id,
        message="Produto atualizado via CLI",
    )
    return product
