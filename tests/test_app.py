import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

import api
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
