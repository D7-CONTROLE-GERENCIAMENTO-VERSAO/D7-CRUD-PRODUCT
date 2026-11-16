import argparse
from decimal import Decimal, InvalidOperation
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_API_URL = os.getenv("API_BASE_URL", "http://api:8000")
VALID_CATEGORIES = ("Eletronicos", "Perifericos", "Moveis", "Vestuario", "Outros")


def resolve_token(cli_token: str | None = None) -> str:
    if cli_token is not None and cli_token.strip():
        return cli_token.strip()
    return os.getenv("CLI_TOKEN", "").strip()


def validate_product_payload(
    name: str,
    category: str,
    price: str,
    stock: int,
    description: str = "",
    product_id: int | None = None,
) -> dict:
    if product_id is not None and product_id < 1:
        raise ValueError("O ID do produto deve ser um inteiro positivo")

    normalized_name = name.strip()
    if len(normalized_name) < 2 or len(normalized_name) > 120:
        raise ValueError("O nome deve ter entre 2 e 120 caracteres")

    if category not in VALID_CATEGORIES:
        raise ValueError(f"Categoria invalida: {category}")

    try:
        dec_price = Decimal(str(price))
    except InvalidOperation as exc:
        raise ValueError("Preco invalido") from exc

    if (
        not dec_price.is_finite()
        or dec_price <= 0
        or dec_price > Decimal("99999999.99")
        or dec_price.as_tuple().exponent < -2
    ):
        raise ValueError("O preco deve ser maior que zero com ate 2 casas decimais")

    if stock < 0 or stock > 2_147_483_647:
        raise ValueError("O estoque deve ser um inteiro nao negativo")

    return {
        "name": normalized_name,
        "category": category,
        "price": f"{dec_price:.2f}",
        "stock": stock,
        "description": description,
    }


def request_api(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
            return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = {"detail": raw or exc.reason}
        return exc.code, detail


def _add_product_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--name", required=True, help="Nome do produto")
    subparser.add_argument("--category", required=True, help="Categoria do produto")
    subparser.add_argument("--price", required=True, help="Preco unitario (> 0)")
    subparser.add_argument("--stock", required=True, type=int, help="Estoque (>= 0)")
    subparser.add_argument(
        "--description", default="", help="Descricao complementar (opcional)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="CLI oficial do CRUD de Produtos (Loja Virtual)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_API_URL,
        help="URL base da API (padrao: $API_BASE_URL ou http://api:8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token CLI (ou defina a variavel de ambiente CLI_TOKEN)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health_parser = subparsers.add_parser(
        "health",
        help="Verifica o healthcheck exclusivo da CLI (/api/cli/health)",
    )
    health_parser.add_argument(
        "--auth",
        action="store_true",
        help="Envia o Bearer token para validar autenticacao opcional no healthcheck",
    )

    token_parser = subparsers.add_parser(
        "token",
        help="Autentica na API (/api/cli/token) e emite um Bearer token para a CLI",
    )
    token_parser.add_argument("--username", required=True, help="Usuario de acesso")
    token_parser.add_argument("--password", required=True, help="Senha de acesso")

    create_parser = subparsers.add_parser(
        "create",
        help="Cadastra um novo produto via API CLI (POST /api/cli/records)",
    )
    _add_product_arguments(create_parser)

    update_parser = subparsers.add_parser(
        "update",
        help="Atualiza um produto existente via API CLI (PUT /api/cli/records/{id})",
    )
    update_parser.add_argument(
        "--id", dest="product_id", required=True, type=int, help="ID do produto"
    )
    _add_product_arguments(update_parser)

    list_parser = subparsers.add_parser(
        "list",
        help="Lista produtos paginados via API CLI (GET /api/cli/records)",
    )
    list_parser.add_argument("--page", type=int, default=1, help="Numero da pagina")
    list_parser.add_argument(
        "--sort-by",
        choices=("id", "name", "category", "price", "stock"),
        default="id",
        help="Campo de ordenacao",
    )
    list_parser.add_argument(
        "--order",
        choices=("asc", "desc"),
        default="asc",
        help="Direcao da ordenacao",
    )

    delete_parser = subparsers.add_parser(
        "delete",
        help="Exclui um produto via API CLI (DELETE /api/cli/records/{id})",
    )
    delete_parser.add_argument(
        "--id", dest="product_id", required=True, type=int, help="ID do produto"
    )

    return parser


def _require_token(raw_token: str | None) -> str | None:
    token = resolve_token(raw_token)
    if not token:
        print(
            json.dumps(
                {"status": "error", "detail": "Informe --token ou defina CLI_TOKEN"}
            ),
            file=sys.stderr,
        )
        return None
    return token


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "health":
        token = None
        if args.auth:
            token = _require_token(args.token)
            if token is None:
                return 1
        status, body = request_api(args.base_url, "GET", "/api/cli/health", token=token)
    elif args.command == "token":
        status, body = request_api(
            args.base_url,
            "POST",
            "/api/cli/token",
            payload={"username": args.username, "password": args.password},
        )
    else:
        token = _require_token(args.token)
        if token is None:
            return 1

        if args.command == "list":
            query = urllib.parse.urlencode(
                {
                    "page": args.page,
                    "sort_by": args.sort_by,
                    "order": args.order,
                }
            )
            status, body = request_api(
                args.base_url,
                "GET",
                f"/api/cli/records?{query}",
                token=token,
            )
        elif args.command == "delete":
            status, body = request_api(
                args.base_url,
                "DELETE",
                f"/api/cli/records/{args.product_id}",
                token=token,
            )
        else:
            product_id = getattr(args, "product_id", None)
            try:
                payload = validate_product_payload(
                    name=args.name,
                    category=args.category,
                    price=args.price,
                    stock=args.stock,
                    description=args.description,
                    product_id=product_id,
                )
            except ValueError as exc:
                print(
                    json.dumps(
                        {"status": "error", "detail": str(exc)}, ensure_ascii=False
                    ),
                    file=sys.stderr,
                )
                return 1

            if args.command == "create":
                status, body = request_api(
                    args.base_url,
                    "POST",
                    "/api/cli/records",
                    payload=payload,
                    token=token,
                )
            else:
                status, body = request_api(
                    args.base_url,
                    "PUT",
                    f"/api/cli/records/{product_id}",
                    payload=payload,
                    token=token,
                )

    print(json.dumps(body, ensure_ascii=False))
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
