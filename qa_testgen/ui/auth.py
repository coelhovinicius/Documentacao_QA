import hmac
import json
import time
from datetime import datetime, timedelta

import extra_streamlit_components as stx
import streamlit as st

SESSION_AUTH_KEY = "authenticated"
SESSION_USER_KEY = "auth_user"

COOKIE_NAME = "qa_testgen_auth"
COOKIE_MAX_AGE_DAYS = 7  # duração da sessão persistida no navegador


# --------------------------------------------------------------------------- #
# Cookie manager (componente que lê/escreve cookies do navegador)
# --------------------------------------------------------------------------- #
def _get_cookie_manager() -> stx.CookieManager:
    if "_cookie_manager" not in st.session_state:
        st.session_state["_cookie_manager"] = stx.CookieManager(key="qa_testgen_cookie_manager")
    return st.session_state["_cookie_manager"]


# --------------------------------------------------------------------------- #
# Credenciais (st.secrets)
# --------------------------------------------------------------------------- #
def _get_users() -> dict:
    """
    [credentials]
    [credentials.usernames]
    admin = "sua-senha-aqui"

    cookie_secret = "uma-string-aleatoria-bem-longa"
    """
    try:
        return dict(st.secrets["credentials"]["usernames"])
    except Exception:
        return {}


def _get_cookie_secret() -> str:
    try:
        return str(st.secrets["credentials"]["cookie_secret"])
    except Exception:
        return "troque-este-segredo-antes-de-publicar"


def _check_credentials(username: str, password: str) -> bool:
    users = _get_users()
    if not username or username not in users:
        hmac.compare_digest(password or "", "senha-invalida-placeholder")
        return False
    return hmac.compare_digest(password or "", str(users[username]))


# --------------------------------------------------------------------------- #
# Token assinado, guardado no cookie (não guarda senha, só usuário + validade)
# --------------------------------------------------------------------------- #
def _sign(username: str, expires_at: int) -> str:
    secret = _get_cookie_secret()
    msg = f"{username}:{expires_at}".encode()
    return hmac.new(secret.encode(), msg, "sha256").hexdigest()


def _make_token(username: str) -> str:
    expires_at = int(time.time()) + COOKIE_MAX_AGE_DAYS * 86400
    payload = {"u": username, "exp": expires_at, "sig": _sign(username, expires_at)}
    return json.dumps(payload)


def _validate_token(raw: str):
    try:
        payload = json.loads(raw)
        username = str(payload["u"])
        expires_at = int(payload["exp"])
        sig = str(payload["sig"])
    except Exception:
        return None

    if expires_at < int(time.time()):
        return None
    if not hmac.compare_digest(sig, _sign(username, expires_at)):
        return None
    if username not in _get_users():
        return None
    return username


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def require_login() -> bool:
    """
    Retorna True se autenticado (app pode prosseguir).
    Retorna False se a tela de login foi exibida (o chamador deve parar a execução).
    """
    if st.session_state.get(SESSION_AUTH_KEY):
        return True

    if not _get_users():
        st.error(
            "⚠️ Nenhuma credencial configurada em `st.secrets['credentials']`. "
            "Configure o `.streamlit/secrets.toml` (local) ou os Secrets do Streamlit Cloud (produção)."
        )
        st.stop()

    cookie_manager = _get_cookie_manager()
    cookies = cookie_manager.get_all()

    # Componente ainda não terminou o round-trip com o navegador: aguarda o
    # rerun automático que ele dispara assim que os cookies chegam.
    if cookies is None:
        st.stop()

    token = cookies.get(COOKIE_NAME)
    if token:
        username = _validate_token(token)
        if username:
            st.session_state[SESSION_AUTH_KEY] = True
            st.session_state[SESSION_USER_KEY] = username
            return True

    _render_login_form(cookie_manager)
    return False


def _render_login_form(cookie_manager: stx.CookieManager):
    st.markdown(
        """
        <style>
            [data-testid="stSidebar"] {display: none;}
            [data-testid="stToolbar"] {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown("## 🧪 QA TestGen")
        st.caption("Acesso restrito. Informe suas credenciais para continuar.")
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Usuário")
            password = st.text_input("Senha", type="password")
            submitted = st.form_submit_button("Entrar", use_container_width=True, type="primary")

        if submitted:
            username = username.strip()
            if _check_credentials(username, password):
                st.session_state[SESSION_AUTH_KEY] = True
                st.session_state[SESSION_USER_KEY] = username

                expires_at = datetime.now() + timedelta(days=COOKIE_MAX_AGE_DAYS)
                cookie_manager.set(
                    COOKIE_NAME,
                    _make_token(username),
                    expires_at=expires_at,
                    key="set_auth_cookie",
                )
                st.rerun()
            else:
                st.error("❌ Usuário ou senha inválidos.")


def logout():
    cookie_manager = _get_cookie_manager()
    try:
        cookie_manager.delete(COOKIE_NAME, key="del_auth_cookie")
    except KeyError:
        pass  # cookie já não existia

    st.session_state.pop(SESSION_AUTH_KEY, None)
    st.session_state.pop(SESSION_USER_KEY, None)
    st.rerun()


def render_logout_control():
    """Controle de logout fixado no rodapé da sidebar (usuário logado + botão Sair)."""
    user = st.session_state.get(SESSION_USER_KEY, "")

    st.markdown(
        """
        <style>
            [data-testid="stSidebarUserContent"] {
                display: flex;
                flex-direction: column;
                min-height: 100%;
            }
            div[class*="st-key-sidebar_logout_box"] {
                margin-top: auto;
                padding-top: 1rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        with st.container(key="sidebar_logout_box"):
            if user:
                st.caption(f"👤 Logado como **{user}**")
            if st.button("🚪 Sair", use_container_width=True, key="btn_logout"):
                logout()
