from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import psycopg2


def log_db(
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
        "layer": "db",
        "client": client,
        "operation": operation,
        "status": status,
        "request_id": request_id,
        "message": message,
    }
    print(json.dumps(entry, ensure_ascii=False), flush=True)
    return entry


def hash_password(password: str) -> str:
    salt = os.getenv("PASSWORD_SALT", "loja-virtual-salt")
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def get_connection():
    dsn = os.getenv(
        "DATABASE_URL", "postgresql://postgres:postgres@db:5432/products_db"
    )
    return psycopg2.connect(dsn)


def init_db(request_id: str = "req-init") -> None:
    admin_user = os.getenv("INITIAL_ADMIN_USER", "admin")
    admin_password = os.getenv("INITIAL_ADMIN_PASSWORD", "admin123")
    password_hash = hash_password(admin_password)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(128) NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    price NUMERIC(10, 2) NOT NULL CHECK (price > 0),
                    stock INTEGER NOT NULL CHECK (stock >= 0),
                    description TEXT NOT NULL DEFAULT ''
                );
                """
            )
            cur.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, %s)
                ON CONFLICT (username) DO UPDATE
                SET password_hash = EXCLUDED.password_hash;
                """,
                (admin_user, password_hash),
            )
        conn.commit()
    finally:
        conn.close()

    log_db(
        client="web",
        operation="init_db",
        status="success",
        request_id=request_id,
        message="Tabelas e usuario inicial configurados",
    )


def web_authenticate_user(username: str, password: str, request_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM users WHERE username = %s;",
                (username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    expected_hash = hash_password(password)
    if row is not None and row[0] == expected_hash:
        log_db(
            client="web",
            operation="login",
            status="success",
            request_id=request_id,
            message="Usuario Web autenticado no banco",
        )
        return True

    log_db(
        client="web",
        operation="login",
        status="error",
        request_id=request_id,
        message="Falha de autenticacao Web no banco",
        level="WARN",
    )
    return False


def _product_from_row(row: tuple[int, str, str, Decimal, int, str]) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "category": row[2],
        "price": row[3],
        "stock": row[4],
        "description": row[5],
    }


def web_create_product(
    name: str,
    category: str,
    price: Decimal,
    stock: int,
    description: str,
    request_id: str,
) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO products (name, category, price, stock, description)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, name, category, price, stock, description;
                """,
                (name, category, price, stock, description),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    product = _product_from_row(row)
    log_db(
        client="web",
        operation="insert",
        status="success",
        request_id=request_id,
        message="Produto persistido",
    )
    return product


def web_update_product(
    product_id: int,
    name: str,
    category: str,
    price: Decimal,
    stock: int,
    description: str,
    request_id: str,
) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE products
                SET name = %s, category = %s, price = %s, stock = %s,
                    description = %s
                WHERE id = %s
                RETURNING id, name, category, price, stock, description;
                """,
                (name, category, price, stock, description, product_id),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if row is None:
        log_db(
            client="web",
            operation="update",
            status="error",
            request_id=request_id,
            message="Produto nao encontrado",
            level="WARN",
        )
        return None

    product = _product_from_row(row)
    log_db(
        client="web",
        operation="update",
        status="success",
        request_id=request_id,
        message="Produto atualizado",
    )
    return product


SORT_COLUMNS = {
    "id": "id",
    "name": "name",
    "category": "category",
    "price": "price",
    "stock": "stock",
}


def web_list_products(
    page: int,
    page_size: int,
    sort_by: str,
    order: str,
    request_id: str,
) -> dict:
    sort_column = SORT_COLUMNS[sort_by]
    sort_direction = "ASC" if order.lower() == "asc" else "DESC"
    offset = (page - 1) * page_size

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM products;")
            total = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT id, name, category, price, stock, description
                FROM products
                ORDER BY {sort_column} {sort_direction}, id ASC
                LIMIT %s OFFSET %s;
                """,
                (page_size, offset),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    items = [_product_from_row(row) for row in rows]
    log_db(
        client="web",
        operation="list",
        status="success",
        request_id=request_id,
        message="Produtos listados do banco",
    )
    return {"items": items, "total": total}


def _build_product_filters(
    search: str | None,
    category: str | None,
    min_price: Decimal | None,
    max_price: Decimal | None,
) -> tuple[str, list]:
    filters = []
    filter_params = []
    if search and search.strip():
        filters.append("name ILIKE %s")
        filter_params.append(f"%{search.strip()}%")
    if category:
        filters.append("category = %s")
        filter_params.append(category)
    if min_price is not None:
        filters.append("price >= %s")
        filter_params.append(min_price)
    if max_price is not None:
        filters.append("price <= %s")
        filter_params.append(max_price)

    where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
    return where_clause, filter_params


def web_search_products(
    search: str | None,
    category: str | None,
    min_price: Decimal | None,
    max_price: Decimal | None,
    page: int,
    page_size: int,
    sort_by: str,
    order: str,
    request_id: str,
) -> dict:
    where_clause, filter_params = _build_product_filters(
        search=search,
        category=category,
        min_price=min_price,
        max_price=max_price,
    )
    sort_column = SORT_COLUMNS[sort_by]
    sort_direction = "ASC" if order.lower() == "asc" else "DESC"
    offset = (page - 1) * page_size

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM products{where_clause};",
                tuple(filter_params),
            )
            total = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT id, name, category, price, stock, description
                FROM products{where_clause}
                ORDER BY {sort_column} {sort_direction}, id ASC
                LIMIT %s OFFSET %s;
                """,
                tuple(filter_params + [page_size, offset]),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    items = [_product_from_row(row) for row in rows]
    log_db(
        client="web",
        operation="search",
        status="success",
        request_id=request_id,
        message="Produtos buscados no banco",
    )
    return {"items": items, "total": total}


def web_export_products(
    search: str | None,
    category: str | None,
    min_price: Decimal | None,
    max_price: Decimal | None,
    sort_by: str,
    order: str,
    request_id: str,
) -> list[dict]:
    where_clause, filter_params = _build_product_filters(
        search=search,
        category=category,
        min_price=min_price,
        max_price=max_price,
    )
    sort_column = SORT_COLUMNS[sort_by]
    sort_direction = "ASC" if order.lower() == "asc" else "DESC"

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, category, price, stock, description
                FROM products{where_clause}
                ORDER BY {sort_column} {sort_direction}, id ASC;
                """,
                tuple(filter_params),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    items = [_product_from_row(row) for row in rows]
    log_db(
        client="web",
        operation="export",
        status="success",
        request_id=request_id,
        message="Produtos consultados para exportacao CSV",
    )
    return items


def web_delete_product(product_id: int, request_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM products
                WHERE id = %s
                RETURNING id;
                """,
                (product_id,),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if row is None:
        log_db(
            client="web",
            operation="delete",
            status="error",
            request_id=request_id,
            message="Produto nao encontrado",
            level="WARN",
        )
        return False

    log_db(
        client="web",
        operation="delete",
        status="success",
        request_id=request_id,
        message="Produto removido",
    )
    return True


def web_get_dashboard(request_id: str) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    category,
                    COUNT(*) AS products_count,
                    COALESCE(SUM(stock), 0) AS total_stock,
                    COALESCE(SUM(price * stock), 0) AS stock_value
                FROM products
                GROUP BY category
                ORDER BY category ASC;
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    products_by_category = [
        {"category": row[0], "count": int(row[1])} for row in rows
    ]
    stock_by_category = [
        {"category": row[0], "total_stock": int(row[2])} for row in rows
    ]
    financial_by_category = [
        {"category": row[0], "total_value": Decimal(str(row[3]))} for row in rows
    ]
    totals = {
        "total_products": sum(item["count"] for item in products_by_category),
        "total_stock": sum(item["total_stock"] for item in stock_by_category),
        "total_financial_value": sum(
            (item["total_value"] for item in financial_by_category),
            Decimal("0.00"),
        ),
    }

    log_db(
        client="web",
        operation="dashboard",
        status="success",
        request_id=request_id,
        message="Indicadores do dashboard consultados no banco",
    )
    return {
        "products_by_category": products_by_category,
        "stock_by_category": stock_by_category,
        "financial_by_category": financial_by_category,
        "totals": totals,
    }


def cli_authenticate_user(username: str, password: str, request_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM users WHERE username = %s;",
                (username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    expected_hash = hash_password(password)
    if row is not None and row[1] == expected_hash:
        log_db(
            client="cli",
            operation="token",
            status="success",
            request_id=request_id,
            message="Usuario CLI autenticado no banco",
        )
        return True

    log_db(
        client="cli",
        operation="token",
        status="error",
        request_id=request_id,
        message="Falha de autenticacao CLI no banco",
        level="WARN",
    )
    return False


def cli_create_product(
    name: str,
    category: str,
    price: Decimal,
    stock: int,
    description: str,
    request_id: str,
) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO products (name, category, price, stock, description)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, name, category, price, stock, description;
                """,
                (name, category, price, stock, description),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    product = _product_from_row(row)
    log_db(
        client="cli",
        operation="insert",
        status="success",
        request_id=request_id,
        message="Produto persistido via CLI",
    )
    return product


def cli_update_product(
    product_id: int,
    name: str,
    category: str,
    price: Decimal,
    stock: int,
    description: str,
    request_id: str,
) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE products
                SET name = %s, category = %s, price = %s, stock = %s,
                    description = %s
                WHERE id = %s
                RETURNING id, name, category, price, stock, description;
                """,
                (name, category, price, stock, description, product_id),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if row is None:
        log_db(
            client="cli",
            operation="update",
            status="error",
            request_id=request_id,
            message="Produto nao encontrado na CLI",
            level="WARN",
        )
        return None

    product = _product_from_row(row)
    log_db(
        client="cli",
        operation="update",
        status="success",
        request_id=request_id,
        message="Produto atualizado via CLI",
    )
    return product
