import hmac
import json
import time

import bcrypt
import streamlit as st

SESSION_AUTH_KEY = "authenticated"
SESSION_USER_KEY = "auth_user"

QUERY_PARAM_NAME = "auth"

# Desloga automaticamente após esse tempo sem nenhuma interação com o app.
# Cada requisição válida "renova" essa janela (sliding window) — é isso que
# controla o logout automático.
INACTIVITY_TIMEOUT_MINUTES = 60


# --------------------------------------------------------------------------- #
# Credenciais (st.secrets)
# --------------------------------------------------------------------------- #
def _get_users() -> dict:
    """
    [credentials]
    [credentials.usernames]
    admin = "$2b$12$....hash-bcrypt....."   # gerado com generate_password_hash.py

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


# Hash "dummy" só para gastar o mesmo tempo de bcrypt quando o usuário não
# existe, evitando que o tempo de resposta revele se um username é válido.
_DUMMY_HASH = bcrypt.hashpw(b"senha-invalida-placeholder", bcrypt.gensalt())


def _check_credentials(username: str, password: str) -> bool:
    users = _get_users()
    stored_hash = users.get(username, _DUMMY_HASH.decode())

    try:
        is_valid = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Hash mal formatado no secrets.toml (ex.: alguém colocou senha em texto puro)
        return False

    return is_valid and username in users


# --------------------------------------------------------------------------- #
# Token assinado, guardado na URL (query param) — não guarda senha, só
# usuário + última atividade. Usar a URL em vez de cookie evita depender de
# componentes de terceiros baseados em iframe, que navegadores modernos vêm
# isolando cada vez mais (mesmo cookies "de sessão" ficam presos no
# armazenamento isolado do iframe em vez da página real).
# --------------------------------------------------------------------------- #
def _sign(username: str, last_activity: int) -> str:
    secret = _get_cookie_secret()
    msg = f"{username}:{last_activity}".encode()
    return hmac.new(secret.encode(), msg, "sha256").hexdigest()


def _make_token(username: str, last_activity: int = None) -> str:
    if last_activity is None:
        last_activity = int(time.time())
    payload = {"u": username, "t": last_activity, "sig": _sign(username, last_activity)}
    return json.dumps(payload, separators=(",", ":"))


def _validate_token(raw: str):
    try:
        payload = json.loads(raw)
        username = str(payload["u"])
        last_activity = int(payload["t"])
        sig = str(payload["sig"])
    except Exception:
        return None

    if not hmac.compare_digest(sig, _sign(username, last_activity)):
        return None
    if int(time.time()) - last_activity > INACTIVITY_TIMEOUT_MINUTES * 60:
        return None  # ficou parado tempo demais — precisa logar de novo
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

    token = st.query_params.get(QUERY_PARAM_NAME)
    if token:
        username = _validate_token(token)
        if username:
            st.session_state[SESSION_AUTH_KEY] = True
            st.session_state[SESSION_USER_KEY] = username
            # Renova a janela de inatividade a cada carregamento válido.
            st.query_params[QUERY_PARAM_NAME] = _make_token(username)
            return True
        # Token inválido/expirado: limpa da URL pra não ficar um lixo ali.
        del st.query_params[QUERY_PARAM_NAME]

    _render_login_form()
    return False


def _render_login_form():
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
        st.markdown("## 🧪 QA Automation – Azure DevOps")
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
                st.query_params[QUERY_PARAM_NAME] = _make_token(username)
                st.rerun()
            else:
                st.error("❌ Usuário ou senha inválidos.")


def logout():
    if QUERY_PARAM_NAME in st.query_params:
        del st.query_params[QUERY_PARAM_NAME]
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
