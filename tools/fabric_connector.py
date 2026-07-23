"""
fabric_connector.py
Connects to Microsoft Fabric SQL Analytics Endpoint using MSAL token auth.

Works on BOTH:
  - Local development (via .env file)
  - Streamlit Cloud (via st.secrets)

Auth method: Azure AD Service Principal (Client Credentials flow)
  → More secure than username/password, works on any cloud host.

Required credentials:
  FABRIC_SERVER      – SQL Analytics Endpoint hostname
                       e.g. abc123.datawarehouse.fabric.microsoft.com
  FABRIC_DATABASE    – Lakehouse or Warehouse name
  FABRIC_TENANT_ID   – Azure AD Tenant ID (found in Azure Portal → Entra ID)
  FABRIC_CLIENT_ID   – App Registration Client ID
  FABRIC_CLIENT_SECRET – App Registration Client Secret

Credential resolution order:
  1. Streamlit Cloud secrets (st.secrets)
  2. .env file in project root
  3. Environment variables
"""
import os
import struct
import decimal
import datetime
import threading

from dotenv import load_dotenv

# Load .env for local dev (no-op on Streamlit Cloud)
load_dotenv(override=False)

_lock = threading.Lock()
_FABRIC_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


# ── Credential helper ────────────────────────────────────────────────────

def _get_env(key: str) -> str | None:
    """Read a credential — prefers Streamlit Cloud secrets over env vars."""
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(key)


def _check_credentials() -> dict:
    """Return all required credentials; raise if any are missing."""
    keys = [
        "FABRIC_SERVER",
        "FABRIC_DATABASE",
        "FABRIC_TENANT_ID",
        "FABRIC_CLIENT_ID",
        "FABRIC_CLIENT_SECRET",
    ]
    creds = {k: _get_env(k) for k in keys}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing Fabric credentials: {', '.join(missing)}.\n"
            "Set them in Streamlit Cloud → App Settings → Secrets, "
            "or in your local .env file."
        )
    return creds


# ── MSAL token acquisition ────────────────────────────────────────────────

def _get_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """
    Acquire an Azure AD access token using the Client Credentials (app-only) flow.
    Requires: pip install msal
    """
    try:
        import msal
    except ImportError:
        raise ImportError(
            "msal is required for Fabric SQL auth. "
            "Add 'msal>=1.28.0' to requirements.txt"
        )

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_for_client(scopes=[_FABRIC_SCOPE])

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "Unknown error"))
        raise RuntimeError(f"Failed to acquire MSAL token: {error}")

    return result["access_token"]


# ── pyodbc token auth helper ──────────────────────────────────────────────

def _token_to_odbc_attr(token: str) -> bytes:
    """
    Pack the bearer token into the binary format expected by pyodbc
    when using SQL_COPT_SS_ACCESS_TOKEN attribute.
    """
    token_bytes = token.encode("utf-16-le")
    token_len = len(token_bytes)
    # Pack as: [len (4 bytes LE)] + [token bytes]
    return struct.pack(f"<I{token_len}s", token_len, token_bytes)


# ── Main query function ───────────────────────────────────────────────────

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
    Execute a T-SQL query against the Fabric SQL Analytics Endpoint.

    Uses MSAL Client Credentials (service principal) auth — works on
    Streamlit Cloud and any other cloud host without ODBC Driver 18.

    Args:
        query:    T-SQL query string
        max_rows: Safety cap on rows returned (default 500)

    Returns:
        List of row dicts {column_name: value}, or raises on failure.
    """
    import pyodbc

    pyodbc.pooling = False

    creds = _check_credentials()
    server   = creds["FABRIC_SERVER"]
    database = creds["FABRIC_DATABASE"]

    # Get MSAL token
    token = _get_access_token(
        tenant_id=creds["FABRIC_TENANT_ID"],
        client_id=creds["FABRIC_CLIENT_ID"],
        client_secret=creds["FABRIC_CLIENT_SECRET"],
    )

    token_attr = _token_to_odbc_attr(token)

    # SQL_COPT_SS_ACCESS_TOKEN = 1256
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={server},1433;"
        f"Database={database};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=60;"
    )

    with _lock:
        try:
            conn = pyodbc.connect(
                conn_str,
                attrs_before={1256: token_attr},
                timeout=60,
                autocommit=True,
            )
        except Exception as ex:
            raise RuntimeError(
                f"Connection to Fabric failed: {ex}\n\n"
                "If running on Streamlit Cloud, ensure the ODBC Driver 18 "
                "system package is available (see packages.txt)."
            ) from ex

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


# ── Status helper ─────────────────────────────────────────────────────────

def get_fabric_credentials_status() -> dict:
    """Returns a dict with status info about the Fabric connection credentials."""
    keys = [
        "FABRIC_SERVER",
        "FABRIC_DATABASE",
        "FABRIC_TENANT_ID",
        "FABRIC_CLIENT_ID",
        "FABRIC_CLIENT_SECRET",
    ]
    status = {}
    for k in keys:
        val = _get_env(k)
        if k == "FABRIC_CLIENT_SECRET":
            status[k] = "✅ Set" if val else "❌ NOT SET"
        else:
            status[k] = val or "❌ NOT SET"

    status["ready"] = all(_get_env(k) for k in keys)
    return status
