import hmac
import json
import time
from datetime import datetime, timedelta

import bcrypt
import extra_streamlit_components as stx
import streamlit as st

from qa_testgen.infrastructure.access_control_client import AccessControlClient

SESSION_AUTH_KEY = "authenticated"
SESSION_USER_KEY = "auth_user"
PENDING_USERNAME_KEY = "_access_pending_username"

COOKIE_NAME = "qa_testgen_auth"

# Desloga automaticamente após esse tempo sem nenhuma interação com o app.
# Cada requisição válida "renova" essa janela (sliding window) — é isso que
# controla o logout automático.
INACTIVITY_TIMEOUT_MINUTES = 60


def _get_cookie_manager():
    """
    Cria (ou reaproveita, via o parâmetro `key`) a instância do gerenciador
    de cookies. IMPORTANTE: não pode ser envolvida em `@st.cache_resource`
    — o CookieManager já chama um componente internamente (equivalente a
    um widget), e o Streamlit proíbe widgets dentro de função cacheada
    (gera `CachedWidgetWarning`/erro). A própria biblioteca já garante
    estabilidade entre reruns através do parâmetro `key`, então cachear
    por fora era redundante e quebrava o app.
    """
    return stx.CookieManager(key="qa_testgen_cookie_manager")


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


def _grant_session(config, username: str, cookie_manager):
    st.session_state[SESSION_AUTH_KEY] = True
    st.session_state[SESSION_USER_KEY] = username
    expires_at = datetime.now() + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
    cookie_manager.set(
        COOKIE_NAME, _make_token(username),
        expires_at=expires_at, key="set_auth_cookie",
    )
    st.session_state.pop(PENDING_USERNAME_KEY, None)
    log_action(config, username, "Login", "Login", "Sessão iniciada com sucesso")


# --------------------------------------------------------------------------- #
# Controle de acesso — aprovação de login (via n8n)
# --------------------------------------------------------------------------- #
def is_approver(config, username: str) -> bool:
    """O dono do app (config.owner_username) é sempre aprovador implícito."""
    if not username:
        return False
    if username == config.owner_username:
        return True
    try:
        return username in AccessControlClient(config).list_approvers()
    except Exception:
        return False


def log_action(config, username: str, action_name: str, location: str, details: str = "") -> None:
    """
    Registra um evento de auditoria (visível só pro dono do app, na área
    de Logs). Nunca lança exceção — se o log falhar (ex.: n8n fora do ar),
    a ação do usuário continua normalmente, só o registro é perdido.
    """
    try:
        AccessControlClient(config).log_action(username, action_name, location, details)
    except Exception:
        pass


def has_permission(config, username: str, permission: str) -> bool:
    """
    Checa se `username` tem uma permissão granular específica (ex.:
    'azure_devops', 'execution_report'). O dono do app sempre tem todas.
    Em caso de falha ao consultar o n8n, nega por padrão (mais seguro do
    que liberar acesso silenciosamente se a checagem falhar).
    """
    if not username:
        return False
    if username == config.owner_username:
        return True
    try:
        return username in AccessControlClient(config).list_permission(permission)
    except Exception:
        return False


def render_pending_approvals_panel(config):
    """Painel de solicitações pendentes — só visível pra quem é aprovador."""
    username = st.session_state.get(SESSION_USER_KEY, "")
    if not is_approver(config, username):
        st.error("❌ Você não tem permissão para aprovar acessos.")
        return

    st.subheader("🔔 Solicitações Pendentes de Acesso")
    client = AccessControlClient(config)
    try:
        pending = client.list_pending()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar as solicitações: {error}")
        return

    if not pending:
        st.success("✅ Nenhuma solicitação pendente no momento.")
        return

    for req in pending:
        req_user = req.get("username", "")
        requested_at = req.get("requested_at", "")
        with st.container(border=True):
            st.write(f"**{req_user}** — solicitado em {requested_at}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Aprovar", key=f"approve_{req_user}", use_container_width=True, type="primary"):
                    try:
                        client.decide(req_user, True, username)
                        log_action(config, username, "Aprovar Acesso", "Solicitações Pendentes", f"Aprovou o acesso de {req_user}")
                        st.success(f"{req_user} aprovado.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")
            with c2:
                if st.button("🚫 Negar", key=f"deny_{req_user}", use_container_width=True):
                    try:
                        client.decide(req_user, False, username)
                        log_action(config, username, "Negar Acesso", "Solicitações Pendentes", f"Negou o acesso de {req_user}")
                        st.warning(f"{req_user} negado.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")


def render_admin_panel(config):
    """
    Solicitações Pendentes fica visível pra qualquer aprovador (dono do app
    incluso). O resto (cadastro de aprovadores, permissões granulares) é
    restrito ao dono do app (config.owner_username).
    """
    username = st.session_state.get(SESSION_USER_KEY, "")

    render_pending_approvals_panel(config)

    if username != config.owner_username:
        return

    st.divider()
    st.subheader("🛡️ Administração — Aprovadores de Acesso")
    st.caption(
        "Pessoas cadastradas aqui podem aprovar ou negar solicitações de login de outros "
        "usuários, além de você. Você (dono do app) já é sempre um aprovador, não precisa "
        "se cadastrar."
    )

    client = AccessControlClient(config)
    try:
        approvers = client.list_approvers()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar os aprovadores: {error}")
        return

    if approvers:
        st.write("**Aprovadores atuais:**")
        for a in approvers:
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"- {a}")
            with c2:
                if st.button("Remover", key=f"remove_approver_{a}"):
                    try:
                        client.remove_approver(a)
                        log_action(config, username, "Remover Aprovador", "Administração", f"Removeu {a} da lista de aprovadores")
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")
    else:
        st.caption("Nenhum aprovador cadastrado além de você.")

    st.divider()
    known_users = sorted(u for u in _get_users() if u != config.owner_username)
    with st.form("add_approver_form", clear_on_submit=True):
        if known_users:
            new_username = st.selectbox("Usuário a cadastrar como aprovador", options=known_users)
        else:
            new_username = st.text_input("Usuário a cadastrar como aprovador")
        submitted = st.form_submit_button("➕ Adicionar Aprovador", type="primary")
        if submitted and new_username and new_username.strip():
            new_username = new_username.strip()
            if new_username not in _get_users():
                st.error("❌ Esse nome de usuário não existe nas credenciais configuradas (`secrets.toml`).")
            else:
                try:
                    client.add_approver(new_username)
                    log_action(config, username, "Adicionar Aprovador", "Administração", f"Adicionou {new_username} como aprovador")
                    st.success(f"{new_username} adicionado como aprovador.")
                    st.rerun()
                except Exception as error:
                    st.error(f"❌ {error}")

    st.divider()
    _render_permission_management(config, client, "azure_devops", "🔗 Acesso à Integração com Azure DevOps (Passo 7)")

    st.divider()
    _render_permission_management(config, client, "execution_report", "📊 Acesso ao Relatório de Testes (Passo 8)")

    st.divider()
    _render_audit_logs(config, client)


def _render_audit_logs(config, client):
    st.subheader("📜 Logs de Auditoria")
    st.caption(
        "Últimos 500 eventos registrados no app (mais recente primeiro). Eventos mais antigos "
        "são descartados automaticamente."
    )
    try:
        logs = client.list_logs()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar os logs: {error}")
        return

    if not logs:
        st.caption("Nenhum evento registrado ainda.")
        return

    usernames = sorted({log.get("username", "") for log in logs if log.get("username")})
    filtro_usuario = st.selectbox("Filtrar por usuário", options=["Todos"] + usernames, key="log_filter_user")
    logs_filtrados = logs if filtro_usuario == "Todos" else [l for l in logs if l.get("username") == filtro_usuario]

    st.caption(f"{len(logs_filtrados)} evento(s)")
    rows = []
    for log in logs_filtrados:
        ts = log.get("timestamp", "")
        try:
            ts_fmt = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            ts_fmt = ts
        rows.append({
            "Data/Hora": ts_fmt,
            "Usuário": log.get("username", ""),
            "Ação": log.get("action", ""),
            "Local": log.get("location", ""),
            "Detalhes": log.get("details", ""),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_permission_management(config, client, permission: str, title: str):
    """
    Bloco reutilizável de cadastro/remoção pra uma permissão granular
    específica — usado tanto pra Azure DevOps quanto pro Relatório de Testes.
    """
    st.subheader(title)
    st.caption(
        "Além de você (dono do app, que sempre tem acesso), essas pessoas também podem acessar essa área."
    )
    try:
        authorized = client.list_permission(permission)
    except Exception as error:
        st.error(f"❌ Não foi possível carregar a lista: {error}")
        return

    if authorized:
        for a in authorized:
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"- {a}")
            with c2:
                if st.button("Remover", key=f"remove_perm_{permission}_{a}"):
                    try:
                        client.revoke_permission(a, permission)
                        log_action(
                            config, st.session_state.get(SESSION_USER_KEY, ""),
                            "Revogar Permissão", "Administração", f"Revogou '{permission}' de {a}",
                        )
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")
    else:
        st.caption("Ninguém além de você tem acesso ainda.")

    known_users = sorted(u for u in _get_users() if u != config.owner_username)
    with st.form(f"add_perm_form_{permission}", clear_on_submit=True):
        if known_users:
            new_username = st.selectbox("Usuário a autorizar", options=known_users, key=f"perm_select_{permission}")
        else:
            new_username = st.text_input("Usuário a autorizar", key=f"perm_input_{permission}")
        submitted = st.form_submit_button("➕ Autorizar", type="primary", key=f"perm_submit_{permission}")
        if submitted and new_username and new_username.strip():
            new_username = new_username.strip()
            if new_username not in _get_users():
                st.error("❌ Esse nome de usuário não existe nas credenciais configuradas (`secrets.toml`).")
            else:
                try:
                    client.grant_permission(new_username, permission)
                    log_action(
                        config, st.session_state.get(SESSION_USER_KEY, ""),
                        "Conceder Permissão", "Administração", f"Concedeu '{permission}' a {new_username}",
                    )
                    st.success(f"{new_username} autorizado.")
                    st.rerun()
                except Exception as error:
                    st.error(f"❌ {error}")


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def require_login(config) -> bool:
    """
    Retorna True se autenticado (app pode prosseguir).
    Retorna False se a tela de login/espera foi exibida (o chamador deve parar a execução).
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
    # O CookieManager lê os cookies do navegador de forma assíncrona — na
    # primeiríssima execução do script ele pode ainda não ter o valor
    # pronto. Nesse caso, `get_all()` devolve None; esperamos o próximo
    # rerun (que o próprio componente dispara sozinho) antes de decidir
    # que a pessoa não está logada.
    all_cookies = cookie_manager.get_all(key="get_all_cookies_initial")
    if all_cookies is None:
        st.stop()

    token = all_cookies.get(COOKIE_NAME)
    if token:
        username = _validate_token(token)
        if username:
            st.session_state[SESSION_AUTH_KEY] = True
            st.session_state[SESSION_USER_KEY] = username
            # Renova a janela de inatividade a cada carregamento válido.
            expires_at = datetime.now() + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
            cookie_manager.set(
                COOKIE_NAME, _make_token(username),
                expires_at=expires_at, key="renew_auth_cookie",
            )
            return True
        # Token inválido/expirado: limpa o cookie.
        try:
            cookie_manager.delete(COOKIE_NAME, key="delete_invalid_cookie")
        except Exception:
            pass

    pending_username = st.session_state.get(PENDING_USERNAME_KEY)
    if pending_username:
        _render_waiting_screen(config, pending_username, cookie_manager)
        return False

    _render_login_form(config, cookie_manager)
    return False


def _render_login_form(config, cookie_manager):
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
                if username == config.owner_username:
                    _grant_session(config, username, cookie_manager)
                    st.rerun()
                else:
                    try:
                        client = AccessControlClient(config)
                        status = client.check_status(username)
                        if status == "approved":
                            client.consume(username)
                            _grant_session(config, username, cookie_manager)
                            st.rerun()
                        else:
                            if status in ("none", "denied", "consumed"):
                                client.create_request(username)
                            st.session_state[PENDING_USERNAME_KEY] = username
                            st.rerun()
                    except Exception as error:
                        st.error(f"❌ Não foi possível verificar a aprovação de acesso: {error}")
            else:
                st.error("❌ Usuário ou senha inválidos.")


def _render_waiting_screen(config, username: str, cookie_manager):
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
        st.markdown("## ⏳ Aguardando aprovação")
        st.info(
            f"Sua solicitação de acesso como **{username}** foi enviada. Um administrador "
            "precisa aprovar antes que você possa entrar. Isso não é automático — clique em "
            "\"Verificar novamente\" depois que alguém tiver aprovado."
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔄 Verificar novamente", use_container_width=True, type="primary"):
                try:
                    client = AccessControlClient(config)
                    status = client.check_status(username)
                    if status == "approved":
                        client.consume(username)
                        _grant_session(config, username, cookie_manager)
                        st.rerun()
                    elif status == "denied":
                        st.error("❌ Sua solicitação de acesso foi negada.")
                    else:
                        st.info("Ainda aguardando aprovação.")
                except Exception as error:
                    st.error(f"❌ Erro ao verificar status: {error}")
        with c2:
            if st.button("← Cancelar", use_container_width=True):
                st.session_state.pop(PENDING_USERNAME_KEY, None)
                st.rerun()


def logout():
    cookie_manager = _get_cookie_manager()
    try:
        cookie_manager.delete(COOKIE_NAME, key="logout_delete_cookie")
    except Exception:
        pass
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
