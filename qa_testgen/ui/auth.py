import secrets as _secrets_module
from datetime import datetime, timedelta, timezone

import bcrypt
import streamlit as st

from qa_testgen.infrastructure.access_control_client import AccessControlClient

SESSION_AUTH_KEY = "authenticated"
SESSION_USER_KEY = "auth_user"
SESSION_ID_KEY = "_auth_session_id"
PENDING_USERNAME_KEY = "_access_pending_username"

QUERY_PARAM_NAME = "sid"

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
    """
    try:
        return dict(st.secrets["credentials"]["usernames"])
    except Exception:
        return {}


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
# Sessão via ID opaco na URL — a URL só carrega um identificador aleatório
# (ex.: "?sid=k3F9x..."), sem nenhuma informação legível sobre quem está
# logado. O dado de verdade (usuário, validade) fica guardado no n8n, não
# na URL — isso também permite REVOGAR uma sessão remotamente (o ID para
# de funcionar mesmo que a URL continue circulando por aí).
# --------------------------------------------------------------------------- #
def _new_session_id() -> str:
    return _secrets_module.token_urlsafe(24)


def _expires_at_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)).isoformat()


def _grant_session(config, username: str):
    session_id = _new_session_id()
    try:
        AccessControlClient(config).create_session(session_id, username, _expires_at_iso())
    except Exception:
        pass  # se o n8n estiver fora do ar, a sessão ainda funciona nesta aba (só não sobrevive a F5)
    st.session_state[SESSION_AUTH_KEY] = True
    st.session_state[SESSION_USER_KEY] = username
    st.session_state[SESSION_ID_KEY] = session_id
    st.query_params[QUERY_PARAM_NAME] = session_id
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
    _render_active_sessions(config, client)

    st.divider()
    _render_audit_logs(config, client)


def _render_active_sessions(config, client):
    st.subheader("🔑 Sessões Ativas")
    st.caption(
        "Revogar uma sessão invalida o link de acesso dela imediatamente — mesmo que a pessoa "
        "já tenha o link aberto ou salvo, ele para de funcionar na próxima ação/carregamento."
    )
    my_session_id = st.session_state.get(SESSION_ID_KEY, "")
    try:
        sessions = client.list_sessions()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar as sessões: {error}")
        return

    if not sessions:
        st.caption("Nenhuma sessão ativa no momento.")
        return

    for sess in sessions:
        is_mine = sess.get("session_id") == my_session_id
        created = sess.get("created_at", "")
        try:
            created_fmt = datetime.fromisoformat(created).strftime("%d/%m/%Y %H:%M")
        except Exception:
            created_fmt = created
        c1, c2 = st.columns([4, 1])
        with c1:
            label = f"**{sess.get('username', '')}** — desde {created_fmt}"
            if is_mine:
                label += " *(esta sessão, a sua)*"
            st.write(label)
        with c2:
            if st.button("Revogar", key=f"revoke_session_{sess.get('session_id')}"):
                try:
                    client.revoke_session(sess.get("session_id"))
                    log_action(config, st.session_state.get(SESSION_USER_KEY, ""), "Revogar Sessão", "Administração",
                               f"Revogou a sessão de {sess.get('username', '')}")
                    if is_mine:
                        # Revogou a própria sessão — precisa deslogar localmente também.
                        st.session_state.pop(SESSION_AUTH_KEY, None)
                        st.session_state.pop(SESSION_USER_KEY, None)
                        st.session_state.pop(SESSION_ID_KEY, None)
                        if QUERY_PARAM_NAME in st.query_params:
                            del st.query_params[QUERY_PARAM_NAME]
                    st.rerun()
                except Exception as error:
                    st.error(f"❌ {error}")


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

    session_id = st.query_params.get(QUERY_PARAM_NAME)
    if session_id:
        try:
            data = AccessControlClient(config).get_session(session_id)
        except Exception:
            data = {"valid": False}

        username = data.get("username", "") if data.get("valid") else ""
        if username and username in _get_users():
            st.session_state[SESSION_AUTH_KEY] = True
            st.session_state[SESSION_USER_KEY] = username
            st.session_state[SESSION_ID_KEY] = session_id
            # Renova a janela de inatividade a cada carregamento válido.
            try:
                AccessControlClient(config).renew_session(session_id, _expires_at_iso())
            except Exception:
                pass
            return True
        # ID inválido/expirado/revogado: limpa da URL pra não ficar lixo ali.
        del st.query_params[QUERY_PARAM_NAME]

    pending_username = st.session_state.get(PENDING_USERNAME_KEY)
    if pending_username:
        _render_waiting_screen(config, pending_username)
        return False

    _render_login_form(config)
    return False


def _render_login_form(config):
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
        st.markdown("## 🧪 QA Automation – DevOps")
        st.caption("Acesso restrito. Informe suas credenciais para continuar.")
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Usuário")
            password = st.text_input("Senha", type="password")
            submitted = st.form_submit_button("Entrar", use_container_width=True, type="primary")

        if submitted:
            username = username.strip()
            if _check_credentials(username, password):
                if username == config.owner_username:
                    _grant_session(config, username)
                    st.rerun()
                else:
                    try:
                        client = AccessControlClient(config)
                        status = client.check_status(username)
                        if status == "approved":
                            client.consume(username)
                            _grant_session(config, username)
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


def _render_waiting_screen(config, username: str):
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
                        _grant_session(config, username)
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


def logout(config=None):
    session_id = st.session_state.get(SESSION_ID_KEY)
    if config is not None and session_id:
        try:
            AccessControlClient(config).revoke_session(session_id)
        except Exception:
            pass  # se falhar, a sessão local ainda é encerrada — só não é revogada remotamente
    if QUERY_PARAM_NAME in st.query_params:
        del st.query_params[QUERY_PARAM_NAME]
    st.session_state.pop(SESSION_AUTH_KEY, None)
    st.session_state.pop(SESSION_USER_KEY, None)
    st.session_state.pop(SESSION_ID_KEY, None)
    st.rerun()


def render_logout_control(config=None):
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
            if st.button("🚪 Sair", use_container_width=True, key="btn_logout",
                         help="Encerra e revoga esta sessão — o link deixa de funcionar, mesmo se alguém tiver uma cópia dele."):
                if st.session_state.get('show_execution_report_page') and st.session_state.get('report_pdf_bytes'):
                    st.session_state['_show_logout_report_confirm'] = True
                    st.rerun()
                else:
                    logout(config)

    if st.session_state.get('_show_logout_report_confirm'):
        _confirm_logout_with_report_modal(config)


@st.dialog("⚠️ Sair sem salvar o Relatório de Testes")
def _confirm_logout_with_report_modal(config=None):
    st.markdown(
        "Você tem um Relatório de Testes gerado nesta sessão. Ao sair, essas informações "
        "serão **perdidas** (não ficam salvas em lugar nenhum fora desta sessão). Deseja "
        "sair mesmo assim?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚪 Sair mesmo assim", use_container_width=True, type="primary", key="confirm_logout_report_btn"):
            st.session_state.pop('_show_logout_report_confirm', None)
            logout(config)
    with c2:
        if st.button("✖ Continuar Logado", use_container_width=True, key="cancel_logout_report_btn"):
            st.session_state['_show_logout_report_confirm'] = False
            st.rerun()
