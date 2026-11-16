import io
import json
import runpy
import sys
import urllib.error
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

import api
import cli
import db


def _make_mock_conn(fetchone_return=None):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_return
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn, mock_cursor


def test_db_log_and_hash(capsys, monkeypatch):
    monkeypatch.setenv("PASSWORD_SALT", "test-salt")
    entry = db.log_db("web", "test_op", "success", "req-001", "Mensagem de teste")
    captured = capsys.readouterr().out.strip()
    parsed = json.loads(captured)
    assert parsed["layer"] == "db"
    assert parsed["client"] == "web"
    assert parsed["request_id"] == "req-001"
    assert entry["status"] == "success"

    h1 = db.hash_password("segredo")
    h2 = db.hash_password("segredo")
    assert h1 == h2
    assert "segredo" not in h1


def test_get_connection_calls_psycopg2(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    with patch("db.psycopg2.connect") as mock_connect:
        mock_connect.return_value = "dummy-conn"
        conn = db.get_connection()
        assert conn == "dummy-conn"
        mock_connect.assert_called_once_with("postgresql://u:p@localhost:5432/db")


def test_init_db_executes_schema_and_admin_seed(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_USER", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "admin123")
    mock_conn, mock_cursor = _make_mock_conn()
    with patch("db.get_connection", return_value=mock_conn):
        db.init_db(request_id="req-init-test")
    assert mock_cursor.execute.call_count == 3
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


def test_web_authenticate_user_success_and_failure(monkeypatch):
    monkeypatch.setenv("PASSWORD_SALT", "salt-1")
    valid_hash = db.hash_password("senha-correta")

    conn_ok, _ = _make_mock_conn(fetchone_return=(valid_hash,))
    with patch("db.get_connection", return_value=conn_ok):
        assert db.web_authenticate_user("admin", "senha-correta", "req-1") is True
    conn_ok.close.assert_called_once()

    conn_fail, _ = _make_mock_conn(fetchone_return=None)
    with patch("db.get_connection", return_value=conn_fail):
        assert db.web_authenticate_user("admin", "errada", "req-2") is False
    conn_fail.close.assert_called_once()


def test_web_create_product_persists_and_logs(capsys):
    row = (17, "Teclado", "Perifericos", Decimal("125.90"), 8, "Mecanico")
    mock_conn, mock_cursor = _make_mock_conn(fetchone_return=row)

    with patch("db.get_connection", return_value=mock_conn):
        product = db.web_create_product(
            "Teclado",
            "Perifericos",
            Decimal("125.90"),
            8,
            "Mecanico",
            "req-create",
        )

    assert product == {
        "id": 17,
        "name": "Teclado",
        "category": "Perifericos",
        "price": Decimal("125.90"),
        "stock": 8,
        "description": "Mecanico",
    }
    query, params = mock_cursor.execute.call_args.args
    assert "INSERT INTO products" in query
    assert params == ("Teclado", "Perifericos", Decimal("125.90"), 8, "Mecanico")
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()
    log_entry = json.loads(capsys.readouterr().out.strip())
    assert log_entry["layer"] == "db"
    assert log_entry["client"] == "web"
    assert log_entry["operation"] == "insert"
    assert log_entry["request_id"] == "req-create"


def test_web_update_product_success_and_not_found(capsys):
    updated_row = (17, "Teclado Pro", "Perifericos", Decimal("140.00"), 5, "Novo")
    mock_conn, mock_cursor = _make_mock_conn(fetchone_return=updated_row)
    with patch("db.get_connection", return_value=mock_conn):
        product = db.web_update_product(
            17,
            "Teclado Pro",
            "Perifericos",
            Decimal("140.00"),
            5,
            "Novo",
            "req-update",
        )
    assert product["id"] == 17
    assert product["name"] == "Teclado Pro"
    query, params = mock_cursor.execute.call_args.args
    assert "UPDATE products" in query
    assert params == (
        "Teclado Pro",
        "Perifericos",
        Decimal("140.00"),
        5,
        "Novo",
        17,
    )
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()

    missing_conn, _ = _make_mock_conn(fetchone_return=None)
    with patch("db.get_connection", return_value=missing_conn):
        assert (
            db.web_update_product(
                999,
                "Nao encontrado",
                "Outros",
                Decimal("1.00"),
                0,
                "",
                "req-missing",
            )
            is None
        )
    missing_conn.commit.assert_called_once()
    missing_conn.close.assert_called_once()
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert entries[0]["request_id"] == "req-update"
    assert entries[1]["status"] == "error"
    assert entries[1]["request_id"] == "req-missing"


def test_web_token_creation_and_verification(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    token = api.create_web_token("admin")
    assert api.verify_web_token(f"Bearer {token}", request_id="req-auth") == "admin"

    with pytest.raises(HTTPException) as exc1:
        api.verify_web_token(None)
    assert exc1.value.status_code == 401

    with pytest.raises(HTTPException) as exc2:
        api.verify_web_token("Bearer token-sem-ponto")
    assert exc2.value.status_code == 401

    with pytest.raises(HTTPException) as exc3:
        api.verify_web_token("Bearer admin.assinatura-invalida")
    assert exc3.value.status_code == 401


def test_api_endpoints_health_index_and_login():
    with patch("db.init_db") as mock_init:
        with TestClient(api.app) as client:
            res_index = client.get("/")
            assert res_index.status_code == 200
            assert "Loja Virtual" in res_index.text
            assert 'id="product-form"' in res_index.text
            assert 'id="product-description"' in res_index.text

            res_health = client.get("/api/web/health")
            assert res_health.status_code == 200
            assert res_health.json() == {"status": "ok", "client": "web"}

            with patch("db.web_authenticate_user", return_value=True):
                res_login_ok = client.post(
                    "/api/web/login",
                    json={"username": "admin", "password": "admin123"},
                )
                assert res_login_ok.status_code == 200
                body = res_login_ok.json()
                assert body["status"] == "ok"
                assert body["username"] == "admin"
                assert "token" in body

            with patch("db.web_authenticate_user", return_value=False):
                res_login_fail = client.post(
                    "/api/web/login",
                    json={"username": "admin", "password": "wrong"},
                )
                assert res_login_fail.status_code == 401
        mock_init.assert_called_once()


def test_api_product_create_update_and_missing_product(capsys):
    token = api.create_web_token("admin")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "name": "Teclado",
        "category": "Perifericos",
        "price": "125.90",
        "stock": 8,
        "description": "Mecanico",
    }
    created = {
        "id": 17,
        "name": "Teclado",
        "category": "Perifericos",
        "price": Decimal("125.90"),
        "stock": 8,
        "description": "Mecanico",
    }
    updated = {**created, "name": "Teclado Pro"}

    def create_product(**kwargs):
        db.log_db(
            "web",
            "insert",
            "success",
            kwargs["request_id"],
            "Produto persistido",
        )
        return created

    def update_product(**kwargs):
        db.log_db(
            "web",
            "update",
            "success",
            kwargs["request_id"],
            "Produto atualizado",
        )
        return updated

    with patch("db.init_db"):
        with TestClient(api.app) as client:
            with patch(
                "db.web_create_product", side_effect=create_product
            ) as create_mock:
                response = client.post(
                    "/api/web/records", json=payload, headers=headers
                )
            assert response.status_code == 201
            assert response.json()["id"] == 17
            create_mock.assert_called_once()
            create_request_id = create_mock.call_args.kwargs["request_id"]

            with patch(
                "db.web_update_product", side_effect=update_product
            ) as update_mock:
                response = client.put(
                    "/api/web/records/17",
                    json={**payload, "name": "Teclado Pro"},
                    headers=headers,
                )
            assert response.status_code == 200
            assert response.json()["name"] == "Teclado Pro"
            update_mock.assert_called_once()
            update_request_id = update_mock.call_args.kwargs["request_id"]

            with patch("db.web_update_product", return_value=None):
                response = client.put(
                    "/api/web/records/999", json=payload, headers=headers
                )
            assert response.status_code == 404
            assert response.json()["detail"] == "Produto nao encontrado"

    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    create_logs = [
        entry
        for entry in entries
        if entry["operation"] in ("create", "insert")
    ]
    update_logs = [
        entry
        for entry in entries
        if entry["operation"] == "update" and entry["status"] == "success"
    ]
    assert {entry["request_id"] for entry in create_logs} == {create_request_id}
    assert {entry["request_id"] for entry in update_logs} == {update_request_id}


def test_api_product_endpoints_require_authentication_and_validate_payload():
    valid_payload = {
        "name": "Teclado",
        "category": "Perifericos",
        "price": "1.00",
        "stock": 0,
    }
    invalid_payload = {
        **valid_payload,
        "name": "X",
        "category": "Categoria invalida",
        "price": "0",
        "stock": -1,
    }
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            response = client.post("/api/web/records", json=valid_payload)
            assert response.status_code == 401

            response = client.put("/api/web/records/1", json=valid_payload)
            assert response.status_code == 401

            token = api.create_web_token("admin")
            headers = {"Authorization": f"Bearer {token}"}
            response = client.post(
                "/api/web/records", json=invalid_payload, headers=headers
            )
            assert response.status_code == 422

            whitespace_name = {
                **valid_payload,
                "name": "  ",
            }
            response = client.post(
                "/api/web/records", json=whitespace_name, headers=headers
            )
            assert response.status_code == 422

            with patch(
                "db.web_create_product",
                return_value={
                    "id": 18,
                    **valid_payload,
                    "price": Decimal("1.00"),
                    "description": "",
                },
            ) as create_mock:
                response = client.post(
                    "/api/web/records", json=valid_payload, headers=headers
                )
            assert response.status_code == 201
            assert create_mock.call_args.kwargs["description"] == ""

            response = client.post(
                "/api/web/records",
                json={**valid_payload, "price": "1.001"},
                headers=headers,
            )
            assert response.status_code == 422
            response = client.post(
                "/api/web/records",
                json={**valid_payload, "stock": 2147483648},
                headers=headers,
            )
            assert response.status_code == 422


def test_db_web_list_products(capsys):
    conn_desc, cursor_desc = _make_mock_conn(fetchone_return=(2,))
    cursor_desc.fetchall.return_value = [
        (2, "Teclado", "Perifericos", Decimal("99.90"), 5, "Mecanico"),
        (1, "Mouse", "Perifericos", Decimal("49.90"), 10, ""),
    ]
    with patch("db.get_connection", return_value=conn_desc):
        result_desc = db.web_list_products(
            page=2,
            page_size=5,
            sort_by="price",
            order="desc",
            request_id="req-list-desc",
        )

    assert result_desc["total"] == 2
    assert len(result_desc["items"]) == 2
    assert result_desc["items"][0]["price"] == Decimal("99.90")
    assert cursor_desc.execute.call_count == 2
    assert "SELECT COUNT(*) FROM products" in cursor_desc.execute.call_args_list[0].args[0]
    list_sql = cursor_desc.execute.call_args_list[1].args[0]
    assert "ORDER BY price DESC, id ASC" in list_sql
    assert "LIMIT %s OFFSET %s" in list_sql
    assert cursor_desc.execute.call_args_list[1].args[1] == (5, 5)
    conn_desc.close.assert_called_once()

    conn_asc, cursor_asc = _make_mock_conn(fetchone_return=(1,))
    cursor_asc.fetchall.return_value = [
        (1, "Mouse", "Perifericos", Decimal("49.90"), 10, ""),
    ]
    with patch("db.get_connection", return_value=conn_asc):
        result_asc = db.web_list_products(
            page=1,
            page_size=5,
            sort_by="name",
            order="asc",
            request_id="req-list-asc",
        )

    assert result_asc["total"] == 1
    assert "ORDER BY name ASC, id ASC" in cursor_asc.execute.call_args_list[1].args[0]
    assert cursor_asc.execute.call_args_list[1].args[1] == (5, 0)
    conn_asc.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["operation"] for entry in logs] == ["list", "list"]
    assert [entry["status"] for entry in logs] == ["success", "success"]
    assert [entry["request_id"] for entry in logs] == ["req-list-desc", "req-list-asc"]


def test_db_web_search_products_combines_filters_and_paginates(capsys):
    row = (17, "Teclado", "Perifericos", Decimal("125.90"), 8, "Mecanico")
    conn, cursor = _make_mock_conn(fetchone_return=(3,))
    cursor.fetchall.return_value = [row]

    with patch("db.get_connection", return_value=conn):
        result = db.web_search_products(
            search="  tecla ",
            category="Perifericos",
            min_price=Decimal("100.00"),
            max_price=Decimal("200.00"),
            page=2,
            page_size=5,
            sort_by="price",
            order="desc",
            request_id="req-search",
        )

    count_sql, count_params = cursor.execute.call_args_list[0].args
    result_sql, result_params = cursor.execute.call_args_list[1].args
    assert "name ILIKE %s" in count_sql
    assert "category = %s" in count_sql
    assert "price >= %s" in count_sql
    assert "price <= %s" in count_sql
    assert count_params == ("%tecla%", "Perifericos", Decimal("100.00"), Decimal("200.00"))
    assert "ORDER BY price DESC, id ASC" in result_sql
    assert "LIMIT %s OFFSET %s" in result_sql
    assert result_params == (
        "%tecla%",
        "Perifericos",
        Decimal("100.00"),
        Decimal("200.00"),
        5,
        5,
    )
    assert result == {
        "items": [
            {
                "id": 17,
                "name": "Teclado",
                "category": "Perifericos",
                "price": Decimal("125.90"),
                "stock": 8,
                "description": "Mecanico",
            }
        ],
        "total": 3,
    }
    conn.close.assert_called_once()
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert logs[0]["operation"] == "search"
    assert logs[0]["request_id"] == "req-search"


def test_db_web_search_products_without_filters(capsys):
    conn, cursor = _make_mock_conn(fetchone_return=(0,))
    cursor.fetchall.return_value = []

    with patch("db.get_connection", return_value=conn):
        result = db.web_search_products(
            search=None,
            category=None,
            min_price=None,
            max_price=None,
            page=1,
            page_size=5,
            sort_by="id",
            order="asc",
            request_id="req-search-empty",
        )

    count_sql, count_params = cursor.execute.call_args_list[0].args
    result_sql, result_params = cursor.execute.call_args_list[1].args
    assert "WHERE" not in count_sql
    assert count_params == ()
    assert "ORDER BY id ASC, id ASC" in result_sql
    assert result_params == (5, 0)
    assert result == {"items": [], "total": 0}
    assert json.loads(capsys.readouterr().out)["operation"] == "search"


def test_db_web_export_products_with_and_without_filters(capsys):
    row = (17, "Teclado", "Perifericos", Decimal("125.90"), 8, "Mecanico")
    conn_filtered, cur_filtered = _make_mock_conn()
    cur_filtered.fetchall.return_value = [row]

    with patch("db.get_connection", return_value=conn_filtered):
        filtered_items = db.web_export_products(
            search="tecla",
            category="Perifericos",
            min_price=Decimal("100.00"),
            max_price=Decimal("200.00"),
            sort_by="price",
            order="desc",
            request_id="req-export-filtered",
        )

    sql_filtered, params_filtered = cur_filtered.execute.call_args.args
    assert "WHERE" in sql_filtered
    assert "LIMIT" not in sql_filtered
    assert "ORDER BY price DESC, id ASC" in sql_filtered
    assert params_filtered == (
        "%tecla%",
        "Perifericos",
        Decimal("100.00"),
        Decimal("200.00"),
    )
    assert len(filtered_items) == 1
    assert filtered_items[0]["name"] == "Teclado"
    conn_filtered.close.assert_called_once()

    conn_all, cur_all = _make_mock_conn()
    cur_all.fetchall.return_value = []
    with patch("db.get_connection", return_value=conn_all):
        all_items = db.web_export_products(
            search=None,
            category=None,
            min_price=None,
            max_price=None,
            sort_by="id",
            order="asc",
            request_id="req-export-all",
        )

    sql_all, params_all = cur_all.execute.call_args.args
    assert "WHERE" not in sql_all
    assert "LIMIT" not in sql_all
    assert params_all == ()
    assert all_items == []
    conn_all.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["operation"] for entry in logs] == ["export", "export"]
    assert [entry["request_id"] for entry in logs] == [
        "req-export-filtered",
        "req-export-all",
    ]


def test_db_web_delete_product(capsys):
    ok_conn, ok_cursor = _make_mock_conn(fetchone_return=(7,))
    with patch("db.get_connection", return_value=ok_conn):
        deleted = db.web_delete_product(7, "req-del-ok")

    assert deleted is True
    assert "DELETE FROM products" in ok_cursor.execute.call_args.args[0]
    assert ok_cursor.execute.call_args.args[1] == (7,)
    ok_conn.commit.assert_called_once()
    ok_conn.close.assert_called_once()

    missing_conn, _ = _make_mock_conn(fetchone_return=None)
    with patch("db.get_connection", return_value=missing_conn):
        not_deleted = db.web_delete_product(8, "req-del-missing")

    assert not_deleted is False
    missing_conn.commit.assert_called_once()
    missing_conn.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["operation"] for entry in logs] == ["delete", "delete"]
    assert [entry["status"] for entry in logs] == ["success", "error"]
    assert logs[1]["level"] == "WARN"
    assert [entry["request_id"] for entry in logs] == ["req-del-ok", "req-del-missing"]


def test_api_web_list_products_and_validation(capsys):
    sample_items = [
        {
            "id": 2,
            "name": "Mouse",
            "category": "Perifericos",
            "price": Decimal("59.90"),
            "stock": 10,
            "description": "Optico",
        }
    ]
    db_result = {"items": sample_items, "total": 6}
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/web/records")
            assert unauth.status_code == 401

            token = api.create_web_token("admin")
            headers = {"Authorization": f"Bearer {token}"}

            with patch("db.web_list_products", return_value=db_result) as list_mock:
                res_default = client.get("/api/web/records", headers=headers)
                res_custom = client.get(
                    "/api/web/records?page=2&page_size=5&sort_by=price&order=desc",
                    headers=headers,
                )

            bad_page = client.get("/api/web/records?page=0", headers=headers)
            bad_size = client.get("/api/web/records?page_size=10", headers=headers)
            bad_sort = client.get("/api/web/records?sort_by=invalid", headers=headers)
            bad_order = client.get("/api/web/records?order=up", headers=headers)

    assert res_default.status_code == 200
    assert res_default.json()["total"] == 6
    assert res_default.json()["page"] == 1
    assert res_default.json()["page_size"] == 5
    assert res_default.json()["sort_by"] == "id"
    assert res_default.json()["order"] == "asc"

    assert res_custom.status_code == 200
    assert res_custom.json()["page"] == 2
    assert res_custom.json()["sort_by"] == "price"
    assert res_custom.json()["order"] == "desc"

    assert bad_page.status_code == 422
    assert bad_size.status_code == 422
    assert bad_sort.status_code == 422
    assert bad_order.status_code == 422

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    list_logs = [entry for entry in logs if entry["operation"] == "list"]
    assert len(list_logs) == 2
    assert list_logs[0]["request_id"] == list_mock.call_args_list[0].kwargs["request_id"]
    assert list_logs[1]["request_id"] == list_mock.call_args_list[1].kwargs["request_id"]


def test_api_web_search_products_auth_filters_and_validation(capsys):
    sample_items = [
        {
            "id": 17,
            "name": "Teclado",
            "category": "Perifericos",
            "price": Decimal("125.90"),
            "stock": 8,
            "description": "Mecanico",
        }
    ]
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/web/records/search")
            assert unauth.status_code == 401

            token = api.create_web_token("admin")
            headers = {"Authorization": f"Bearer {token}"}
            with patch(
                "db.web_search_products",
                return_value={"items": sample_items, "total": 1},
            ) as search_mock:
                response = client.get(
                    "/api/web/records/search"
                    "?search=tecla&category=Perifericos&min_price=100"
                    "&max_price=200&page=2&page_size=5&sort_by=price&order=desc",
                    headers=headers,
                )
                bad_range = client.get(
                    "/api/web/records/search?min_price=200&max_price=100",
                    headers=headers,
                )
                bad_category = client.get(
                    "/api/web/records/search?category=Invalida",
                    headers=headers,
                )
                bad_price = client.get(
                    "/api/web/records/search?min_price=-1",
                    headers=headers,
                )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["page"] == 2
    assert response.json()["sort_by"] == "price"
    assert response.json()["order"] == "desc"
    search_kwargs = search_mock.call_args.kwargs
    assert search_kwargs["search"] == "tecla"
    assert search_kwargs["category"] == "Perifericos"
    assert search_kwargs["min_price"] == Decimal("100")
    assert search_kwargs["max_price"] == Decimal("200")
    assert search_kwargs["page"] == 2
    assert search_kwargs["page_size"] == 5
    assert search_kwargs["sort_by"] == "price"
    assert search_kwargs["order"] == "desc"
    assert bad_range.status_code == 422
    assert bad_category.status_code == 422
    assert bad_price.status_code == 422

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    search_logs = [entry for entry in logs if entry["operation"] == "search"]
    assert len(search_logs) == 1
    assert search_logs[0]["request_id"] == search_kwargs["request_id"]


def test_api_web_export_products_auth_filters_and_csv(capsys):
    sample_items = [
        {
            "id": 17,
            "name": "Teclado",
            "category": "Perifericos",
            "price": Decimal("125.90"),
            "stock": 8,
            "description": "Mecanico",
        }
    ]
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/web/records/export")
            assert unauth.status_code == 401

            token = api.create_web_token("admin")
            headers = {"Authorization": f"Bearer {token}"}
            with patch("db.web_export_products", return_value=sample_items) as exp_mock:
                response = client.get(
                    "/api/web/records/export"
                    "?search=tecla&category=Perifericos&min_price=100"
                    "&max_price=200&sort_by=price&order=desc",
                    headers=headers,
                )
                bad_range = client.get(
                    "/api/web/records/export?min_price=200&max_price=100",
                    headers=headers,
                )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="produtos.csv"' in response.headers["content-disposition"]
    assert response.text.splitlines() == [
        "id,name,category,price,stock,description",
        "17,Teclado,Perifericos,125.90,8,Mecanico",
    ]
    exp_kwargs = exp_mock.call_args.kwargs
    assert exp_kwargs["search"] == "tecla"
    assert exp_kwargs["category"] == "Perifericos"
    assert exp_kwargs["min_price"] == Decimal("100")
    assert exp_kwargs["max_price"] == Decimal("200")
    assert exp_kwargs["sort_by"] == "price"
    assert exp_kwargs["order"] == "desc"
    assert bad_range.status_code == 422

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    export_logs = [entry for entry in logs if entry["operation"] == "export"]
    assert len(export_logs) == 1
    assert export_logs[0]["request_id"] == exp_kwargs["request_id"]


def test_api_web_delete_product_and_auth(capsys):
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.delete("/api/web/records/7")
            assert unauth.status_code == 401

            token = api.create_web_token("admin")
            headers = {"Authorization": f"Bearer {token}"}

            with patch("db.web_delete_product", side_effect=[True, False]) as del_mock:
                res_ok = client.delete("/api/web/records/7", headers=headers)
                res_missing = client.delete("/api/web/records/8", headers=headers)

    assert res_ok.status_code == 200
    assert res_ok.json() == {"status": "ok", "deleted_id": 7}
    assert res_missing.status_code == 404
    assert res_missing.json()["detail"] == "Produto nao encontrado"

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    del_logs = [entry for entry in logs if entry["operation"] == "delete"]
    assert [entry["status"] for entry in del_logs] == ["success", "error"]
    assert del_logs[0]["request_id"] == del_mock.call_args_list[0].kwargs["request_id"]
    assert del_logs[1]["request_id"] == del_mock.call_args_list[1].kwargs["request_id"]


def test_index_contains_listing_pagination_sort_and_delete_controls():
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            res_index = client.get("/")

    assert res_index.status_code == 200
    for marker in (
        'id="products-table"',
        'id="products-tbody"',
        'id="sort-by"',
        'id="sort-order"',
        'id="prev-page-btn"',
        'id="next-page-btn"',
        'id="page-info"',
        'id="delete-product-btn"',
        'id="search-name"',
        'id="search-category"',
        'id="search-min-price"',
        'id="search-max-price"',
        'id="clear-search-btn"',
        'id="export-csv-btn"',
        'id="dashboard-section"',
        'id="chart-products-by-category"',
        'id="chart-stock-by-category"',
        'id="chart-financial-by-category"',
        "/api/web/records/search",
        "/api/web/records/export",
        "/api/web/dashboard",
        "/api/web/records",
    ):
        assert marker in res_index.text


def test_db_web_get_dashboard(capsys):
    conn, cursor = _make_mock_conn()
    cursor.fetchall.return_value = [
        ("Eletronicos", 2, 10, Decimal("2500.00")),
        ("Perifericos", 3, 15, Decimal("750.50")),
    ]
    with patch("db.get_connection", return_value=conn):
        dashboard = db.web_get_dashboard(request_id="req-dash-db")

    sql = cursor.execute.call_args.args[0]
    assert "COUNT(*)" in sql
    assert "SUM(stock)" in sql
    assert "SUM(price * stock)" in sql
    assert "GROUP BY category" in sql
    conn.close.assert_called_once()

    assert dashboard == {
        "products_by_category": [
            {"category": "Eletronicos", "count": 2},
            {"category": "Perifericos", "count": 3},
        ],
        "stock_by_category": [
            {"category": "Eletronicos", "total_stock": 10},
            {"category": "Perifericos", "total_stock": 15},
        ],
        "financial_by_category": [
            {"category": "Eletronicos", "total_value": Decimal("2500.00")},
            {"category": "Perifericos", "total_value": Decimal("750.50")},
        ],
        "totals": {
            "total_products": 5,
            "total_stock": 25,
            "total_financial_value": Decimal("3250.50"),
        },
    }

    log_entry = json.loads(capsys.readouterr().out.strip())
    assert log_entry["layer"] == "db"
    assert log_entry["client"] == "web"
    assert log_entry["operation"] == "dashboard"
    assert log_entry["status"] == "success"
    assert log_entry["request_id"] == "req-dash-db"


def test_api_web_get_dashboard_and_auth(capsys):
    sample_dashboard = {
        "products_by_category": [{"category": "Perifericos", "count": 2}],
        "stock_by_category": [{"category": "Perifericos", "total_stock": 8}],
        "financial_by_category": [
            {"category": "Perifericos", "total_value": Decimal("400.00")}
        ],
        "totals": {
            "total_products": 2,
            "total_stock": 8,
            "total_financial_value": Decimal("400.00"),
        },
    }
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/web/dashboard")
            assert unauth.status_code == 401

            token = api.create_web_token("admin")
            headers = {"Authorization": f"Bearer {token}"}
            with patch(
                "db.web_get_dashboard", return_value=sample_dashboard
            ) as dash_mock:
                res_ok = client.get("/api/web/dashboard", headers=headers)

    assert res_ok.status_code == 200
    body = res_ok.json()
    assert body["status"] == "ok"
    assert body["products_by_category"] == [{"category": "Perifericos", "count": 2}]
    assert body["stock_by_category"] == [{"category": "Perifericos", "total_stock": 8}]
    assert body["financial_by_category"] == [
        {"category": "Perifericos", "total_value": 400.0}
    ]
    assert body["totals"] == {
        "total_products": 2,
        "total_stock": 8,
        "total_financial_value": 400.0,
    }

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    dash_logs = [entry for entry in logs if entry["operation"] == "dashboard"]
    assert len(dash_logs) == 1
    assert dash_logs[0]["layer"] == "api"
    assert dash_logs[0]["client"] == "web"
    assert dash_logs[0]["status"] == "success"
    assert dash_logs[0]["request_id"] == dash_mock.call_args.kwargs["request_id"]


def test_db_cli_authenticate_user(capsys, monkeypatch):
    monkeypatch.setenv("PASSWORD_SALT", "test-salt")
    good_hash = db.hash_password("admin123")
    conn_ok, cur_ok = _make_mock_conn(fetchone_return=(1, good_hash))
    conn_wrong, _ = _make_mock_conn(fetchone_return=(1, good_hash))
    conn_missing, _ = _make_mock_conn(fetchone_return=None)

    with patch("db.get_connection", return_value=conn_ok):
        assert db.cli_authenticate_user("admin", "admin123", "req-cli-ok") is True
    assert "SELECT id, password_hash FROM users" in cur_ok.execute.call_args.args[0]
    conn_ok.close.assert_called_once()

    with patch("db.get_connection", return_value=conn_wrong):
        assert db.cli_authenticate_user("admin", "errada", "req-cli-bad") is False
    conn_wrong.close.assert_called_once()

    with patch("db.get_connection", return_value=conn_missing):
        assert db.cli_authenticate_user("outro", "admin123", "req-cli-none") is False
    conn_missing.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["client"] for entry in logs] == ["cli", "cli", "cli"]
    assert [entry["operation"] for entry in logs] == ["token", "token", "token"]
    assert [entry["status"] for entry in logs] == ["success", "error", "error"]
    assert [entry["request_id"] for entry in logs] == [
        "req-cli-ok",
        "req-cli-bad",
        "req-cli-none",
    ]


def test_api_cli_token_health_and_auth(capsys, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    cli_token = api.create_cli_token("admin")
    assert api.verify_cli_token(f"Bearer {cli_token}", request_id="req-cli") == "admin"

    for bad_header in (
        None,
        "Bearer sem-ponto",
        "Bearer admin.assinatura-errada",
        f"Bearer {api.create_web_token('admin')}",
    ):
        with pytest.raises(HTTPException) as exc:
            api.verify_cli_token(bad_header, request_id="req-cli-err")
        assert exc.value.status_code == 401

    capsys.readouterr()
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            res_public = client.get("/api/cli/health")
            res_auth_ok = client.get(
                "/api/cli/health",
                headers={"Authorization": f"Bearer {cli_token}"},
            )
            res_auth_bad = client.get(
                "/api/cli/health",
                headers={"Authorization": "Bearer invalido.assinatura"},
            )
            with patch("db.cli_authenticate_user", return_value=True) as auth_ok_mock:
                res_tok_ok = client.post(
                    "/api/cli/token",
                    json={"username": "admin", "password": "admin123"},
                )
            with patch("db.cli_authenticate_user", return_value=False):
                res_tok_bad = client.post(
                    "/api/cli/token",
                    json={"username": "admin", "password": "errada"},
                )

    assert res_public.status_code == 200
    assert res_public.json() == {"status": "ok", "client": "cli"}
    assert res_auth_ok.status_code == 200
    assert res_auth_bad.status_code == 401

    assert res_tok_ok.status_code == 200
    tok_body = res_tok_ok.json()
    assert tok_body["status"] == "ok"
    assert tok_body["token_type"] == "Bearer"
    assert tok_body["username"] == "admin"
    assert api.verify_cli_token(f"Bearer {tok_body['token']}") == "admin"
    assert res_tok_bad.status_code == 401

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    tok_ok_logs = [
        entry
        for entry in logs
        if entry["client"] == "cli"
        and entry["operation"] == "token"
        and entry["status"] == "success"
    ]
    assert len(tok_ok_logs) == 1
    assert tok_ok_logs[0]["request_id"] == auth_ok_mock.call_args.kwargs["request_id"]


def test_cli_module_commands_and_entrypoint(capsys, monkeypatch):
    assert cli.resolve_token("  tok-arg ") == "tok-arg"
    monkeypatch.setenv("CLI_TOKEN", " tok-env ")
    assert cli.resolve_token(None) == "tok-env"
    monkeypatch.delenv("CLI_TOKEN", raising=False)
    assert cli.resolve_token(None) == ""

    def _mock_resp(status: int, payload: dict):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"status": "ok", "client": "cli"}),
    ) as mock_open:
        rc = cli.main(["--base-url", "http://localhost:8000", "health"])
    assert rc == 0
    req = mock_open.call_args.args[0]
    assert req.full_url == "http://localhost:8000/api/cli/health"
    assert req.get_method() == "GET"
    assert json.loads(capsys.readouterr().out.strip()) == {
        "status": "ok",
        "client": "cli",
    }

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"status": "ok", "client": "cli"}),
    ) as mock_auth_open:
        rc_auth = cli.main(["--token", "meu-token", "health", "--auth"])
    assert rc_auth == 0
    assert mock_auth_open.call_args.args[0].headers["Authorization"] == "Bearer meu-token"
    capsys.readouterr()

    rc_no_tok = cli.main(["health", "--auth"])
    assert rc_no_tok == 1
    assert "CLI_TOKEN" in capsys.readouterr().err

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(
            200, {"status": "ok", "token": "t.sig", "token_type": "Bearer"}
        ),
    ) as mock_tok_open:
        rc_tok = cli.main(["token", "--username", "admin", "--password", "admin123"])
    assert rc_tok == 0
    tok_req = mock_tok_open.call_args.args[0]
    assert tok_req.get_method() == "POST"
    assert json.loads(tok_req.data.decode("utf-8")) == {
        "username": "admin",
        "password": "admin123",
    }
    capsys.readouterr()

    http_401 = urllib.error.HTTPError(
        "http://api:8000/api/cli/token",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"detail":"Credenciais invalidas"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_401):
        rc_err = cli.main(["token", "--username", "admin", "--password", "errada"])
    assert rc_err == 1
    assert json.loads(capsys.readouterr().out.strip()) == {
        "detail": "Credenciais invalidas"
    }

    http_502 = urllib.error.HTTPError(
        "http://api:8000/api/cli/health",
        502,
        "Bad Gateway",
        {},
        io.BytesIO(b"upstream down"),
    )
    with patch("urllib.request.urlopen", side_effect=http_502):
        code_502, body_502 = cli.request_api("http://api:8000", "GET", "/api/cli/health")
    assert code_502 == 502
    assert body_502 == {"detail": "upstream down"}

    monkeypatch.setattr(sys, "argv", ["cli.py", "health"])
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"status": "ok", "client": "cli"}),
    ):
        with pytest.raises(SystemExit) as main_exit:
            runpy.run_path(cli.__file__, run_name="__main__")
    assert main_exit.value.code == 0
    capsys.readouterr()


def test_db_cli_create_and_update_product(capsys):
    created_row = (21, "Monitor", "Eletronicos", Decimal("899.90"), 4, "IPS 24")
    conn_create, cur_create = _make_mock_conn(fetchone_return=created_row)
    with patch("db.get_connection", return_value=conn_create):
        created = db.cli_create_product(
            "Monitor",
            "Eletronicos",
            Decimal("899.90"),
            4,
            "IPS 24",
            "req-cli-create",
        )

    assert created == {
        "id": 21,
        "name": "Monitor",
        "category": "Eletronicos",
        "price": Decimal("899.90"),
        "stock": 4,
        "description": "IPS 24",
    }
    assert "INSERT INTO products" in cur_create.execute.call_args.args[0]
    conn_create.commit.assert_called_once()
    conn_create.close.assert_called_once()

    updated_row = (21, "Monitor 4K", "Eletronicos", Decimal("1299.90"), 3, "UHD")
    conn_update, cur_update = _make_mock_conn(fetchone_return=updated_row)
    with patch("db.get_connection", return_value=conn_update):
        updated = db.cli_update_product(
            21,
            "Monitor 4K",
            "Eletronicos",
            Decimal("1299.90"),
            3,
            "UHD",
            "req-cli-update",
        )

    assert updated["name"] == "Monitor 4K"
    assert "UPDATE products" in cur_update.execute.call_args.args[0]
    conn_update.commit.assert_called_once()
    conn_update.close.assert_called_once()

    conn_missing, _ = _make_mock_conn(fetchone_return=None)
    with patch("db.get_connection", return_value=conn_missing):
        missing = db.cli_update_product(
            999,
            "Monitor",
            "Eletronicos",
            Decimal("899.90"),
            4,
            "",
            "req-cli-missing",
        )
    assert missing is None
    conn_missing.commit.assert_called_once()
    conn_missing.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["client"] for entry in logs] == ["cli", "cli", "cli"]
    assert [entry["operation"] for entry in logs] == ["insert", "update", "update"]
    assert [entry["status"] for entry in logs] == ["success", "success", "error"]
    assert [entry["request_id"] for entry in logs] == [
        "req-cli-create",
        "req-cli-update",
        "req-cli-missing",
    ]


def test_api_cli_create_and_update_records(capsys):
    cli_token = api.create_cli_token("admin")
    headers = {"Authorization": f"Bearer {cli_token}"}
    payload = {
        "name": "Headset",
        "category": "Perifericos",
        "price": "199.90",
        "stock": 6,
        "description": "7.1 Surround",
    }
    created_product = {
        "id": 31,
        "name": "Headset",
        "category": "Perifericos",
        "price": Decimal("199.90"),
        "stock": 6,
        "description": "7.1 Surround",
    }
    updated_product = {**created_product, "name": "Headset Pro"}

    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth_post = client.post("/api/cli/records", json=payload)
            unauth_put = client.put("/api/cli/records/31", json=payload)
            assert unauth_post.status_code == 401
            assert unauth_put.status_code == 401

            with patch(
                "db.cli_create_product", return_value=created_product
            ) as create_mock:
                res_create = client.post(
                    "/api/cli/records", json=payload, headers=headers
                )

            with patch(
                "db.cli_update_product", side_effect=[updated_product, None]
            ) as update_mock:
                res_update = client.put(
                    "/api/cli/records/31",
                    json={**payload, "name": "Headset Pro"},
                    headers=headers,
                )
                res_missing = client.put(
                    "/api/cli/records/999", json=payload, headers=headers
                )

    assert res_create.status_code == 201
    assert res_create.json()["id"] == 31
    assert res_update.status_code == 200
    assert res_update.json()["name"] == "Headset Pro"
    assert res_missing.status_code == 404
    assert res_missing.json()["detail"] == "Produto nao encontrado"

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    create_logs = [
        entry
        for entry in logs
        if entry["client"] == "cli" and entry["operation"] == "create"
    ]
    update_logs = [
        entry
        for entry in logs
        if entry["client"] == "cli" and entry["operation"] == "update"
    ]
    assert len(create_logs) == 1
    assert create_logs[0]["request_id"] == create_mock.call_args.kwargs["request_id"]
    assert [entry["status"] for entry in update_logs] == ["success", "error"]
    assert update_logs[0]["request_id"] == update_mock.call_args_list[0].kwargs["request_id"]
    assert update_logs[1]["request_id"] == update_mock.call_args_list[1].kwargs["request_id"]


def test_cli_create_and_update_commands_and_validation(capsys, monkeypatch):
    monkeypatch.delenv("CLI_TOKEN", raising=False)

    for bad_kwargs in (
        {"name": "Teclado", "category": "Perifericos", "price": "10.00", "stock": 1, "product_id": 0},
        {"name": " ", "category": "Perifericos", "price": "10.00", "stock": 1},
        {"name": "Teclado", "category": "Invalida", "price": "10.00", "stock": 1},
        {"name": "Teclado", "category": "Perifericos", "price": "abc", "stock": 1},
        {"name": "Teclado", "category": "Perifericos", "price": "0", "stock": 1},
        {"name": "Teclado", "category": "Perifericos", "price": "10.999", "stock": 1},
        {"name": "Teclado", "category": "Perifericos", "price": "10.00", "stock": -1},
    ):
        with pytest.raises(ValueError):
            cli.validate_product_payload(**bad_kwargs)

    rc_no_tok = cli.main(
        [
            "create",
            "--name",
            "Teclado",
            "--category",
            "Perifericos",
            "--price",
            "99.90",
            "--stock",
            "3",
        ]
    )
    assert rc_no_tok == 1
    assert "CLI_TOKEN" in capsys.readouterr().err

    rc_invalid = cli.main(
        [
            "--token",
            "tok-cli",
            "create",
            "--name",
            "X",
            "--category",
            "Perifericos",
            "--price",
            "99.90",
            "--stock",
            "3",
        ]
    )
    assert rc_invalid == 1
    assert "entre 2 e 120 caracteres" in capsys.readouterr().err

    def _mock_resp(status: int, payload: dict):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(201, {"id": 10, "name": "Teclado"}),
    ) as mock_create:
        rc_create = cli.main(
            [
                "--token",
                "tok-cli",
                "create",
                "--name",
                "  Teclado  ",
                "--category",
                "Perifericos",
                "--price",
                "99.9",
                "--stock",
                "3",
                "--description",
                "ABNT2",
            ]
        )
    assert rc_create == 0
    create_req = mock_create.call_args.args[0]
    assert create_req.get_method() == "POST"
    assert create_req.full_url.endswith("/api/cli/records")
    assert create_req.headers["Authorization"] == "Bearer tok-cli"
    assert json.loads(create_req.data.decode("utf-8")) == {
        "name": "Teclado",
        "category": "Perifericos",
        "price": "99.90",
        "stock": 3,
        "description": "ABNT2",
    }
    capsys.readouterr()

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"id": 10, "name": "Teclado Pro"}),
    ) as mock_update:
        rc_update = cli.main(
            [
                "--token",
                "tok-cli",
                "update",
                "--id",
                "10",
                "--name",
                "Teclado Pro",
                "--category",
                "Perifericos",
                "--price",
                "129.90",
                "--stock",
                "5",
            ]
        )
    assert rc_update == 0
    update_req = mock_update.call_args.args[0]
    assert update_req.get_method() == "PUT"
    assert update_req.full_url.endswith("/api/cli/records/10")
    capsys.readouterr()


def test_db_cli_list_and_delete_products(capsys):
    listed_row = (17, "Teclado", "Perifericos", Decimal("125.90"), 8, "Mecanico")
    conn_list, cur_list = _make_mock_conn(fetchone_return=(6,))
    cur_list.fetchall.return_value = [listed_row]

    with patch("db.get_connection", return_value=conn_list):
        result = db.cli_list_products(
            page=2,
            page_size=5,
            sort_by="price",
            order="desc",
            request_id="req-cli-list",
        )

    assert result == {
        "items": [
            {
                "id": 17,
                "name": "Teclado",
                "category": "Perifericos",
                "price": Decimal("125.90"),
                "stock": 8,
                "description": "Mecanico",
            }
        ],
        "total": 6,
    }
    count_sql = cur_list.execute.call_args_list[0].args[0]
    list_sql, list_params = cur_list.execute.call_args_list[1].args
    assert "SELECT COUNT(*) FROM products" in count_sql
    assert "ORDER BY price DESC, id ASC" in list_sql
    assert list_params == (5, 5)
    conn_list.close.assert_called_once()

    conn_deleted, cur_deleted = _make_mock_conn(fetchone_return=(17,))
    with patch("db.get_connection", return_value=conn_deleted):
        assert db.cli_delete_product(17, "req-cli-delete") is True
    assert "DELETE FROM products" in cur_deleted.execute.call_args.args[0]
    assert cur_deleted.execute.call_args.args[1] == (17,)
    conn_deleted.commit.assert_called_once()
    conn_deleted.close.assert_called_once()

    conn_missing, _ = _make_mock_conn(fetchone_return=None)
    with patch("db.get_connection", return_value=conn_missing):
        assert db.cli_delete_product(999, "req-cli-delete-missing") is False
    conn_missing.commit.assert_called_once()
    conn_missing.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["client"] for entry in logs] == ["cli", "cli", "cli"]
    assert [entry["operation"] for entry in logs] == ["list", "delete", "delete"]
    assert [entry["status"] for entry in logs] == ["success", "success", "error"]
    assert [entry["request_id"] for entry in logs] == [
        "req-cli-list",
        "req-cli-delete",
        "req-cli-delete-missing",
    ]


def test_api_cli_list_and_delete_records(capsys):
    cli_token = api.create_cli_token("admin")
    headers = {"Authorization": f"Bearer {cli_token}"}
    listed_items = [
        {
            "id": 17,
            "name": "Teclado",
            "category": "Perifericos",
            "price": Decimal("125.90"),
            "stock": 8,
            "description": "Mecanico",
        }
    ]

    def list_products(**kwargs):
        db.log_db(
            "cli", "list", "success", kwargs["request_id"], "Produtos listados"
        )
        return {"items": listed_items, "total": 6}

    def delete_product(product_id, request_id):
        deleted = product_id == 17
        db.log_db(
            "cli",
            "delete",
            "success" if deleted else "error",
            request_id,
            "Produto removido" if deleted else "Produto nao encontrado",
            level="INFO" if deleted else "WARN",
        )
        return deleted

    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth_list = client.get("/api/cli/records")
            unauth_delete = client.delete("/api/cli/records/17")
            with patch("db.cli_list_products", side_effect=list_products) as list_mock:
                response_list = client.get(
                    "/api/cli/records?page=2&page_size=5&sort_by=price&order=desc",
                    headers=headers,
                )
            with patch(
                "db.cli_delete_product",
                side_effect=delete_product,
            ) as delete_mock:
                response_deleted = client.delete(
                    "/api/cli/records/17", headers=headers
                )
                response_missing = client.delete(
                    "/api/cli/records/999", headers=headers
                )
            invalid_page = client.get(
                "/api/cli/records?page=0", headers=headers
            )
            invalid_sort = client.get(
                "/api/cli/records?sort_by=invalid", headers=headers
            )
            invalid_order = client.get(
                "/api/cli/records?order=up", headers=headers
            )

    assert unauth_list.status_code == 401
    assert unauth_delete.status_code == 401
    assert response_list.status_code == 200
    assert response_list.json() == {
        "status": "ok",
        "items": [
            {
                "id": 17,
                "name": "Teclado",
                "category": "Perifericos",
                "price": 125.9,
                "stock": 8,
                "description": "Mecanico",
            }
        ],
        "total": 6,
        "page": 2,
        "page_size": 5,
        "sort_by": "price",
        "order": "desc",
    }
    list_kwargs = list_mock.call_args.kwargs
    assert list_kwargs["page"] == 2
    assert list_kwargs["page_size"] == 5
    assert list_kwargs["sort_by"] == "price"
    assert list_kwargs["order"] == "desc"
    assert response_deleted.status_code == 200
    assert response_deleted.json() == {"status": "ok", "deleted_id": 17}
    assert response_missing.status_code == 404
    assert response_missing.json()["detail"] == "Produto nao encontrado"
    assert invalid_page.status_code == 422
    assert invalid_sort.status_code == 422
    assert invalid_order.status_code == 422

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    for operation, call in (
        ("list", list_mock.call_args),
        ("delete", delete_mock.call_args_list[0]),
    ):
        request_id = call.kwargs["request_id"]
        operation_logs = [
            entry
            for entry in logs
            if entry["client"] == "cli"
            and entry["operation"] == operation
            and entry["request_id"] == request_id
        ]
        assert {entry["layer"] for entry in operation_logs} == {"api", "db"}
    assert [
        entry["status"]
        for entry in logs
        if entry["operation"] == "delete" and entry["layer"] == "api"
    ] == ["success", "error"]


def test_cli_list_and_delete_commands(capsys, monkeypatch):
    monkeypatch.delenv("CLI_TOKEN", raising=False)
    assert cli.main(["list"]) == 1
    assert "CLI_TOKEN" in capsys.readouterr().err
    assert cli.main(["delete", "--id", "17"]) == 1
    assert "CLI_TOKEN" in capsys.readouterr().err

    def _mock_resp(status: int, payload: dict):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"items": [], "total": 0}),
    ) as mock_list:
        rc_list = cli.main(
            [
                "--token",
                "tok-cli",
                "list",
                "--page",
                "2",
                "--sort-by",
                "price",
                "--order",
                "desc",
            ]
        )
    assert rc_list == 0
    list_req = mock_list.call_args.args[0]
    assert list_req.get_method() == "GET"
    assert list_req.full_url.endswith(
        "/api/cli/records?page=2&sort_by=price&order=desc"
    )
    assert list_req.headers["Authorization"] == "Bearer tok-cli"
    capsys.readouterr()

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"status": "ok", "deleted_id": 17}),
    ) as mock_delete:
        rc_delete = cli.main(["--token", "tok-cli", "delete", "--id", "17"])
    assert rc_delete == 0
    delete_req = mock_delete.call_args.args[0]
    assert delete_req.get_method() == "DELETE"
    assert delete_req.full_url.endswith("/api/cli/records/17")
    assert delete_req.headers["Authorization"] == "Bearer tok-cli"
    capsys.readouterr()

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(
            404, {"detail": "Produto nao encontrado"}
        ),
    ):
        rc_missing = cli.main(["--token", "tok-cli", "delete", "--id", "999"])
    assert rc_missing == 1
    assert json.loads(capsys.readouterr().out.strip()) == {
        "detail": "Produto nao encontrado"
    }


def test_db_cli_search_products_and_equivalence(capsys):
    row = (17, "Teclado Mecanico", "Perifericos", Decimal("249.90"), 12, "RGB")
    conn_cli, cur_cli = _make_mock_conn(fetchone_return=(1,))
    cur_cli.fetchall.return_value = [row]

    conn_web, cur_web = _make_mock_conn(fetchone_return=(1,))
    cur_web.fetchall.return_value = [row]

    kwargs = {
        "search": "tecla",
        "category": "Perifericos",
        "min_price": Decimal("100.00"),
        "max_price": Decimal("300.00"),
        "page": 1,
        "page_size": 5,
        "sort_by": "price",
        "order": "desc",
    }
    with patch("db.get_connection", return_value=conn_cli):
        res_cli = db.cli_search_products(**kwargs, request_id="req-cli-search")
    with patch("db.get_connection", return_value=conn_web):
        res_web = db.web_search_products(**kwargs, request_id="req-web-search")

    assert res_cli == res_web
    assert cur_cli.execute.call_args_list == cur_web.execute.call_args_list
    conn_cli.close.assert_called_once()
    conn_web.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert logs[0]["client"] == "cli"
    assert logs[0]["operation"] == "search"
    assert logs[0]["request_id"] == "req-cli-search"


def test_api_cli_search_records_auth_and_validation(capsys):
    cli_token = api.create_cli_token("admin")
    headers = {"Authorization": f"Bearer {cli_token}"}
    sample_items = [
        {
            "id": 17,
            "name": "Teclado Mecanico",
            "category": "Perifericos",
            "price": Decimal("249.90"),
            "stock": 12,
            "description": "RGB",
        }
    ]
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/cli/records/search")
            assert unauth.status_code == 401

            with patch(
                "db.cli_search_products",
                return_value={"items": sample_items, "total": 1},
            ) as search_mock:
                res_ok = client.get(
                    "/api/cli/records/search"
                    "?search=tecla&category=Perifericos&min_price=100"
                    "&max_price=300&page=2&page_size=5&sort_by=price&order=desc",
                    headers=headers,
                )
                bad_range = client.get(
                    "/api/cli/records/search?min_price=300&max_price=100",
                    headers=headers,
                )

    assert res_ok.status_code == 200
    body = res_ok.json()
    assert body["total"] == 1
    assert body["page"] == 2
    assert body["sort_by"] == "price"
    assert body["order"] == "desc"
    assert bad_range.status_code == 422

    call_kwargs = search_mock.call_args.kwargs
    assert call_kwargs["search"] == "tecla"
    assert call_kwargs["category"] == "Perifericos"
    assert call_kwargs["min_price"] == Decimal("100")
    assert call_kwargs["max_price"] == Decimal("300")

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    search_logs = [
        entry
        for entry in logs
        if entry["client"] == "cli" and entry["operation"] == "search"
    ]
    assert len(search_logs) == 1
    assert search_logs[0]["request_id"] == call_kwargs["request_id"]


def test_cli_search_command(capsys, monkeypatch):
    monkeypatch.delenv("CLI_TOKEN", raising=False)
    assert cli.main(["search", "--search", "tecla"]) == 1
    assert "CLI_TOKEN" in capsys.readouterr().err

    def _mock_resp(status: int, payload: dict):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"status": "ok", "items": [], "total": 0}),
    ) as mock_search_all:
        rc_all = cli.main(
            [
                "--token",
                "tok-cli",
                "search",
                "--search",
                "  tecla  ",
                "--category",
                "Perifericos",
                "--min-price",
                "100.00",
                "--max-price",
                "300.00",
                "--page",
                "2",
                "--sort-by",
                "price",
                "--order",
                "desc",
            ]
        )
    assert rc_all == 0
    req_all = mock_search_all.call_args.args[0]
    assert req_all.get_method() == "GET"
    assert req_all.full_url.endswith(
        "/api/cli/records/search?page=2&sort_by=price&order=desc"
        "&search=tecla&category=Perifericos&min_price=100.00&max_price=300.00"
    )
    assert req_all.headers["Authorization"] == "Bearer tok-cli"
    capsys.readouterr()

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, {"status": "ok", "items": [], "total": 0}),
    ) as mock_search_defaults:
        rc_def = cli.main(["--token", "tok-cli", "search"])
    assert rc_def == 0
    req_def = mock_search_defaults.call_args.args[0]
    assert req_def.full_url.endswith(
        "/api/cli/records/search?page=1&sort_by=id&order=asc"
    )
    capsys.readouterr()


def test_db_cli_export_products_with_and_without_filters(capsys):
    row = (17, "Teclado Mecanico", "Perifericos", Decimal("249.90"), 12, "RGB")
    conn_filtered, cur_filtered = _make_mock_conn()
    cur_filtered.fetchall.return_value = [row]

    with patch("db.get_connection", return_value=conn_filtered):
        filtered = db.cli_export_products(
            search="tecla",
            category="Perifericos",
            min_price=Decimal("100.00"),
            max_price=Decimal("300.00"),
            sort_by="price",
            order="desc",
            request_id="req-cli-exp-1",
        )

    sql_filtered, params_filtered = cur_filtered.execute.call_args.args
    assert "WHERE" in sql_filtered
    assert "LIMIT" not in sql_filtered
    assert "ORDER BY price DESC, id ASC" in sql_filtered
    assert params_filtered == (
        "%tecla%",
        "Perifericos",
        Decimal("100.00"),
        Decimal("300.00"),
    )
    assert len(filtered) == 1
    assert filtered[0]["name"] == "Teclado Mecanico"
    conn_filtered.close.assert_called_once()

    conn_all, cur_all = _make_mock_conn()
    cur_all.fetchall.return_value = []
    with patch("db.get_connection", return_value=conn_all):
        empty = db.cli_export_products(
            search=None,
            category=None,
            min_price=None,
            max_price=None,
            sort_by="id",
            order="asc",
            request_id="req-cli-exp-2",
        )

    sql_all, params_all = cur_all.execute.call_args.args
    assert "WHERE" not in sql_all
    assert "LIMIT" not in sql_all
    assert params_all == ()
    assert empty == []
    conn_all.close.assert_called_once()

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["client"] for entry in logs] == ["cli", "cli"]
    assert [entry["operation"] for entry in logs] == ["export", "export"]
    assert [entry["request_id"] for entry in logs] == [
        "req-cli-exp-1",
        "req-cli-exp-2",
    ]


def test_api_cli_export_products_auth_filters_and_csv(capsys):
    sample_items = [
        {
            "id": 17,
            "name": "Teclado Mecanico",
            "category": "Perifericos",
            "price": Decimal("249.90"),
            "stock": 12,
            "description": "RGB",
        }
    ]
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/cli/records/export")
            assert unauth.status_code == 401

            cli_token = api.create_cli_token("admin")
            headers = {"Authorization": f"Bearer {cli_token}"}
            with patch("db.cli_export_products", return_value=sample_items) as exp_mock:
                res_ok = client.get(
                    "/api/cli/records/export"
                    "?search=tecla&category=Perifericos&min_price=100"
                    "&max_price=300&sort_by=price&order=desc",
                    headers=headers,
                )
                bad_range = client.get(
                    "/api/cli/records/export?min_price=300&max_price=100",
                    headers=headers,
                )

    assert res_ok.status_code == 200
    assert res_ok.headers["content-type"].startswith("text/csv")
    assert 'filename="produtos_cli.csv"' in res_ok.headers["content-disposition"]
    assert res_ok.text.splitlines() == [
        "id,name,category,price,stock,description",
        "17,Teclado Mecanico,Perifericos,249.90,12,RGB",
    ]
    exp_kwargs = exp_mock.call_args.kwargs
    assert exp_kwargs["search"] == "tecla"
    assert exp_kwargs["category"] == "Perifericos"
    assert exp_kwargs["min_price"] == Decimal("100")
    assert exp_kwargs["max_price"] == Decimal("300")
    assert exp_kwargs["sort_by"] == "price"
    assert exp_kwargs["order"] == "desc"
    assert bad_range.status_code == 422

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    exp_logs = [
        entry
        for entry in logs
        if entry["client"] == "cli" and entry["operation"] == "export"
    ]
    assert len(exp_logs) == 1
    assert exp_logs[0]["request_id"] == exp_kwargs["request_id"]


def test_cli_export_command(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("CLI_TOKEN", raising=False)
    assert cli.main(["export"]) == 1
    assert "CLI_TOKEN" in capsys.readouterr().err

    csv_payload = (
        "id,name,category,price,stock,description\n"
        "17,Teclado Mecanico,Perifericos,249.90,12,RGB\n"
    )

    def _mock_csv_resp(status: int, text: str):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = text.encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_csv_resp(200, csv_payload),
    ) as mock_stdout:
        rc_stdout = cli.main(
            [
                "--token",
                "tok-cli",
                "export",
                "--search",
                "tecla",
                "--category",
                "Perifericos",
                "--min-price",
                "100.00",
                "--max-price",
                "300.00",
                "--sort-by",
                "price",
                "--order",
                "desc",
            ]
        )
    assert rc_stdout == 0
    req_stdout = mock_stdout.call_args.args[0]
    assert req_stdout.get_method() == "GET"
    assert req_stdout.headers["Accept"] == "text/csv"
    assert req_stdout.headers["Authorization"] == "Bearer tok-cli"
    assert req_stdout.full_url.endswith(
        "/api/cli/records/export?sort_by=price&order=desc"
        "&search=tecla&category=Perifericos&min_price=100.00&max_price=300.00"
    )
    assert capsys.readouterr().out == csv_payload

    out_file = tmp_path / "produtos_cli.csv"
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_csv_resp(200, csv_payload),
    ):
        rc_file = cli.main(
            ["--token", "tok-cli", "export", "--output", str(out_file)]
        )
    assert rc_file == 0
    assert out_file.read_text(encoding="utf-8") == csv_payload
    assert json.loads(capsys.readouterr().out.strip()) == {
        "status": "ok",
        "output": str(out_file),
    }

    http_422 = urllib.error.HTTPError(
        "http://api:8000/api/cli/records/export",
        422,
        "Unprocessable Entity",
        {},
        io.BytesIO(b'{"detail":"Faixa de preco invalida"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_422):
        rc_err = cli.main(["--token", "tok-cli", "export"])
    assert rc_err == 1
    assert json.loads(capsys.readouterr().out.strip()) == {
        "detail": "Faixa de preco invalida"
    }

    http_502 = urllib.error.HTTPError(
        "http://api:8000/api/cli/records/export",
        502,
        "Bad Gateway",
        {},
        io.BytesIO(b"gateway error"),
    )
    with patch("urllib.request.urlopen", side_effect=http_502):
        code_502, err_502 = cli.request_csv(
            "http://api:8000", "/api/cli/records/export", token="tok-cli"
        )
    assert code_502 == 502
    assert err_502 == {"detail": "gateway error"}


def test_db_cli_get_dashboard(capsys):
    conn, cursor = _make_mock_conn()
    cursor.fetchall.return_value = [
        ("Eletronicos", 2, 10, Decimal("2500.00")),
        ("Perifericos", 3, 15, Decimal("750.50")),
    ]
    with patch("db.get_connection", return_value=conn):
        dashboard = db.cli_get_dashboard(request_id="req-cli-dashboard-db")

    sql = cursor.execute.call_args.args[0]
    assert "COUNT(*)" in sql
    assert "SUM(stock)" in sql
    assert "SUM(price * stock)" in sql
    assert "GROUP BY category" in sql
    conn.close.assert_called_once()
    assert dashboard == {
        "products_by_category": [
            {"category": "Eletronicos", "count": 2},
            {"category": "Perifericos", "count": 3},
        ],
        "stock_by_category": [
            {"category": "Eletronicos", "total_stock": 10},
            {"category": "Perifericos", "total_stock": 15},
        ],
        "financial_by_category": [
            {"category": "Eletronicos", "total_value": Decimal("2500.00")},
            {"category": "Perifericos", "total_value": Decimal("750.50")},
        ],
        "totals": {
            "total_products": 5,
            "total_stock": 25,
            "total_financial_value": Decimal("3250.50"),
        },
    }

    log_entry = json.loads(capsys.readouterr().out.strip())
    assert log_entry["layer"] == "db"
    assert log_entry["client"] == "cli"
    assert log_entry["operation"] == "dashboard"
    assert log_entry["status"] == "success"
    assert log_entry["request_id"] == "req-cli-dashboard-db"


def test_api_cli_get_dashboard_auth_and_correlated_logs(capsys):
    sample_dashboard = {
        "products_by_category": [{"category": "Perifericos", "count": 2}],
        "stock_by_category": [{"category": "Perifericos", "total_stock": 8}],
        "financial_by_category": [
            {"category": "Perifericos", "total_value": Decimal("400.00")}
        ],
        "totals": {
            "total_products": 2,
            "total_stock": 8,
            "total_financial_value": Decimal("400.00"),
        },
    }
    with patch("db.init_db"):
        with TestClient(api.app) as client:
            unauth = client.get("/api/cli/dashboard")

            cli_token = api.create_cli_token("admin")
            headers = {"Authorization": f"Bearer {cli_token}"}
            with patch(
                "db.cli_get_dashboard", return_value=sample_dashboard
            ) as dashboard_mock:
                response = client.get("/api/cli/dashboard", headers=headers)

    assert unauth.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "products_by_category": [
            {"category": "Perifericos", "count": 2},
        ],
        "stock_by_category": [
            {"category": "Perifericos", "total_stock": 8},
        ],
        "financial_by_category": [
            {"category": "Perifericos", "total_value": 400.0},
        ],
        "totals": {
            "total_products": 2,
            "total_stock": 8,
            "total_financial_value": 400.0,
        },
    }

    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    dashboard_logs = [
        entry
        for entry in logs
        if entry["client"] == "cli" and entry["operation"] == "dashboard"
    ]
    assert len(dashboard_logs) == 1
    assert dashboard_logs[0]["layer"] == "api"
    assert dashboard_logs[0]["client"] == "cli"
    assert dashboard_logs[0]["status"] == "success"
    assert dashboard_logs[0]["request_id"] == dashboard_mock.call_args.kwargs[
        "request_id"
    ]


def test_cli_dashboard_command_and_empty_categories(capsys, monkeypatch):
    monkeypatch.delenv("CLI_TOKEN", raising=False)
    assert cli.main(["dashboard"]) == 1
    assert "CLI_TOKEN" in capsys.readouterr().err

    dashboard = {
        "products_by_category": [
            {"category": "Eletronicos", "count": 2},
            {"category": "Perifericos", "count": 3},
        ],
        "stock_by_category": [
            {"category": "Eletronicos", "total_stock": 10},
            {"category": "Perifericos", "total_stock": 15},
        ],
        "financial_by_category": [
            {"category": "Eletronicos", "total_value": 2500.0},
            {"category": "Perifericos", "total_value": 750.5},
        ],
        "totals": {
            "total_products": 5,
            "total_stock": 25,
            "total_financial_value": 3250.5,
        },
    }

    def _mock_resp(status: int, payload: dict):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_resp(200, dashboard),
    ) as mock_dashboard:
        result = cli.main(["--token", "tok-cli", "dashboard"])

    assert result == 0
    request = mock_dashboard.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.full_url == "http://api:8000/api/cli/dashboard"
    assert request.headers["Authorization"] == "Bearer tok-cli"
    assert capsys.readouterr().out == (
        "Indicadores CLI por categoria\n"
        "\n"
        "1. Produtos por categoria:\n"
        "  Eletronicos: 2 produto(s)\n"
        "  Perifericos: 3 produto(s)\n"
        "Total geral: 5 produto(s)\n"
        "\n"
        "2. Estoque total por categoria:\n"
        "  Eletronicos: 10 unidade(s)\n"
        "  Perifericos: 15 unidade(s)\n"
        "Total geral: 25 unidade(s)\n"
        "\n"
        "3. Valor financeiro em estoque por categoria:\n"
        "  Eletronicos: R$ 2500.00\n"
        "  Perifericos: R$ 750.50\n"
        "Total geral: R$ 3250.50\n"
    )

    empty_dashboard = {
        "products_by_category": [],
        "stock_by_category": [],
        "financial_by_category": [],
        "totals": {
            "total_products": 0,
            "total_stock": 0,
            "total_financial_value": 0,
        },
    }
    empty_output = cli._format_dashboard(empty_dashboard)
    assert "Total geral: 0 produto(s)" in empty_output
    assert "Total geral: 0 unidade(s)" in empty_output
    assert "Total geral: R$ 0.00" in empty_output

    unauthorized = urllib.error.HTTPError(
        "http://api:8000/api/cli/dashboard",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"detail":"Token CLI ausente ou invalido"}'),
    )
    with patch("urllib.request.urlopen", side_effect=unauthorized):
        failed = cli.main(["--token", "invalid", "dashboard"])
    assert failed == 1
    assert json.loads(capsys.readouterr().out.strip()) == {
        "detail": "Token CLI ausente ou invalido"
    }
