"""
fabric_connector.py
Lightweight pyodbc helper that lets Streamlit query the Microsoft Fabric
Lakehouse directly, using the same credentials as the fabric-mcp MCP server.

Credential resolution order:
  1. Streamlit Cloud secrets (st.secrets) — used when deployed on Streamlit Cloud
  2. .env file in the fabric-mcp directory — used for local fabric-mcp setup
  3. .env in the project root — general local fallback
"""
import os
import decimal
import datetime
import threading
import pyodbc
from dotenv import load_dotenv

# Disable connection pooling to avoid threading issues in Streamlit
pyodbc.pooling = False

# Load credentials from the fabric-mcp .env file (local dev)
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "fabric mcp", "fabric-mcp", ".env")
load_dotenv(dotenv_path=os.path.abspath(_ENV_PATH), override=False)

# Fallback: also try loading from current working directory
load_dotenv(override=False)

_lock = threading.Lock()


def _get_env(key: str) -> str | None:
    """Read a credential — prefers Streamlit Cloud secrets over env vars."""
    try:
        import streamlit as st
        return st.secrets.get(key) or os.getenv(key)
    except Exception:
        return os.getenv(key)


def _build_conn_str() -> str:
    server   = _get_env("FABRIC_SERVER")
    database = _get_env("FABRIC_DATABASE")
    username = _get_env("FABRIC_USERNAME")
    password = _get_env("FABRIC_PASSWORD")

    missing = [k for k, v in {
        "FABRIC_SERVER": server,
        "FABRIC_DATABASE": database,
        "FABRIC_USERNAME": username,
        "FABRIC_PASSWORD": password,
    }.items() if not v]

    if missing:
        raise EnvironmentError(
            f"Missing Fabric credentials in .env: {', '.join(missing)}. "
            "Ensure FABRIC_SERVER, FABRIC_DATABASE, FABRIC_USERNAME, and FABRIC_PASSWORD are set."
        )

    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={server};"
        f"Database={database};"
        f"UID={username};"
        f"PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Authentication=ActiveDirectoryPassword;"
        "Connection Timeout=60;"
    )


def _safe_value(v):
    """Convert a pyodbc row value to a JSON/pandas-safe Python type."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, datetime.time):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def run_fabric_sql(query: str, max_rows: int = 500) -> list[dict]:
    """
    Execute a T-SQL query against the Fabric Lakehouse and return results
    as a list of dicts (column_name → value).

    Args:
        query:    T-SQL query string
        max_rows: Safety cap on rows returned (default 500)

    Returns:
        List of row dicts, or raises on connection/query failure.
    """
    conn_str = _build_conn_str()

    with _lock:
        try:
            conn = pyodbc.connect(conn_str, timeout=60, autocommit=True)
        except Exception as ex:
            raise RuntimeError(f"Connection to Fabric failed: {ex}") from ex

        try:
            cursor = conn.cursor()
            try:
                cursor.execute(query)
            except Exception as ex:
                raise RuntimeError(f"Query execution failed: {ex}") from ex

            if cursor.description is None:
                return []

            cols = [d[0] for d in cursor.description]
            rows = []
            fetched = cursor.fetchmany(max_rows)
            while fetched:
                for row in fetched:
                    rows.append(dict(zip(cols, [_safe_value(v) for v in row])))
                if len(rows) >= max_rows:
                    break
                fetched = cursor.fetchmany(max_rows - len(rows))
            return rows
        finally:
            try:
                conn.close()
            except Exception:
                pass


def get_fabric_credentials_status() -> dict:
    """Returns a dict with status info about the Fabric connection credentials."""
    server   = _get_env("FABRIC_SERVER")
    database = _get_env("FABRIC_DATABASE")
    username = _get_env("FABRIC_USERNAME")
    password = _get_env("FABRIC_PASSWORD")
    return {
        "server":   server or "❌ NOT SET",
        "database": database or "❌ NOT SET",
        "username": username or "❌ NOT SET",
        "password": "✅ Set" if password else "❌ NOT SET",
        "ready":    all([server, database, username, password]),
    }
