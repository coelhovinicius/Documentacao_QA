import requests


class AccessControlError(Exception):
    """Erro vindo do backend de controle de acesso (n8n) — ex.: usuário já existe, usuário não encontrado."""
    pass


class AccessControlClient:
    """
    Cliente do webhook de controle de acesso (n8n) — cadastro de aprovadores
    e fluxo de solicitação/aprovação de login. Toda a persistência (quem é
    aprovador, quem já foi aprovado, solicitações pendentes) mora do lado do
    n8n (workflow static data), não no app.
    """

    def __init__(self, config):
        self.config = config
        api_key = config.api_key if hasattr(config, 'api_key') else None
        self.headers = {"x-api-key": api_key} if api_key else {}

    def _call(self, action: str, **kwargs) -> dict:
        payload = {"action": action, **kwargs}
        response = requests.post(
            self.config.webhook_access_control,
            json=payload,
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        return data or {}

    def list_approvers(self) -> list:
        data = self._call("list_approvers")
        return data.get("approvers", [])

    def add_approver(self, username: str) -> list:
        data = self._call("add_approver", username=username)
        return data.get("approvers", [])

    def remove_approver(self, username: str) -> list:
        data = self._call("remove_approver", username=username)
        return data.get("approvers", [])

    def create_request(self, username: str) -> None:
        self._call("create_request", username=username)

    def list_pending(self) -> list:
        data = self._call("list_pending")
        return data.get("requests", [])

    def decide(self, username: str, approved: bool, decided_by: str) -> None:
        self._call(
            "decide",
            username=username,
            decision="approved" if approved else "denied",
            decided_by=decided_by,
        )

    def check_status(self, username: str) -> str:
        """Retorna 'approved', 'pending', 'denied' ou 'none'."""
        data = self._call("check_status", username=username)
        return data.get("status", "none")

    def consume(self, username: str) -> None:
        """
        Marca a aprovação mais recente desse usuário como "gasta" — chamado
        assim que a sessão é concedida, pra essa aprovação não valer de novo
        num login futuro sem uma nova solicitação/aprovação.
        """
        self._call("consume", username=username)

    # ------------------------------------------------------------------ #
    # Permissões granulares (ex.: 'azure_devops', 'execution_report') —
    # diferente da lista de aprovadores (quem pode aprovar login de outros),
    # essas controlam quem pode acessar áreas específicas do app.
    # ------------------------------------------------------------------ #
    def list_permission(self, permission: str) -> list:
        data = self._call("list_permission", permission=permission)
        return data.get("users", [])

    def grant_permission(self, username: str, permission: str) -> list:
        data = self._call("grant_permission", username=username, permission=permission)
        return data.get("users", [])

    def revoke_permission(self, username: str, permission: str) -> list:
        data = self._call("revoke_permission", username=username, permission=permission)
        return data.get("users", [])

    # ------------------------------------------------------------------ #
    # Logs de auditoria — últimos 500 eventos, mais antigos são
    # descartados automaticamente do lado do n8n.
    # ------------------------------------------------------------------ #
    def log_action(self, username: str, action_name: str, location: str, details: str = "") -> None:
        """
        Registra um evento de auditoria. Nunca deve derrubar a ação do
        usuário se falhar — quem chama deve envolver isso num try/except
        silencioso (logging é um "nice to have", não pode travar o app).
        """
        self._call(
            "log_action",
            username=username,
            action_name=action_name,
            location=location,
            details=details,
        )

    def list_logs(self) -> list:
        data = self._call("list_logs")
        return data.get("logs", [])

    # ------------------------------------------------------------------ #
    # Sessões — o dado de sessão de verdade (quem, quando expira) fica
    # aqui, no n8n. A URL do navegador só carrega um ID curto e opaco.
    # ------------------------------------------------------------------ #
    def create_session(self, session_id: str, username: str, expires_at_iso: str) -> None:
        self._call("create_session", session_id=session_id, username=username, expires_at=expires_at_iso)

    def get_session(self, session_id: str) -> dict:
        """Retorna {'valid': bool, 'username': str, 'expires_at': str} — 'valid' False se não existir/expirou."""
        data = self._call("get_session", session_id=session_id)
        return {
            "valid": bool(data.get("valid")),
            "username": data.get("username", ""),
            "expires_at": data.get("expires_at", ""),
        }

    def renew_session(self, session_id: str, expires_at_iso: str) -> None:
        self._call("renew_session", session_id=session_id, expires_at=expires_at_iso)

    def revoke_session(self, session_id: str) -> None:
        self._call("revoke_session", session_id=session_id)

    def list_sessions(self) -> list:
        data = self._call("list_sessions")
        return data.get("sessions", [])

    # ------------------------------------------------------------------ #
    # Usuários gerenciados dinamicamente — CRUD completo, feito pelo
    # dono/admin do app pela tela de Administração. O dono continua
    # definido SÓ no secrets.toml, nunca aqui. O hash de senha nunca é
    # exibido em tela — list_users() nunca traz esse campo de volta.
    # ------------------------------------------------------------------ #
    def list_users(self) -> list:
        """
        Cada usuário: {"username","email","nome","acesso_direto",
        "is_approver","permissions": [...], "criado_em","criado_por","atualizado_em"}
        """
        data = self._call("list_users")
        return data.get("users", [])

    def create_user(self, username: str, password_hash: str, email: str, nome: str,
                     acesso_direto: bool = False, criado_por: str = "") -> None:
        """Nome, e-mail, usuário e senha são SEMPRE obrigatórios — o backend valida isso também."""
        data = self._call(
            "create_user", username=username, password_hash=password_hash,
            email=email, nome=nome, acesso_direto=acesso_direto, criado_por=criado_por,
        )
        if not data.get("ok"):
            raise AccessControlError(data.get("error", "Não foi possível criar o usuário."))

    def update_user_full(self, username: str, new_username: str, email: str, nome: str,
                          password_hash: str, acesso_direto: bool, is_approver: bool,
                          permissions: list) -> str:
        """
        Salva TUDO de um cadastro numa chamada só — nome, e-mail, nick
        (renomeia se mudou), senha, modo de acesso (direto x aprovação),
        status de aprovador, e a lista completa de permissões (sincroniza:
        adiciona as que faltam, remove as que sobram). Retorna o
        username final (pode ter mudado, se renomeou).
        """
        data = self._call(
            "update_user_full", username=username, new_username=new_username,
            email=email, nome=nome, password_hash=password_hash,
            acesso_direto=acesso_direto, is_approver=is_approver, permissions=permissions,
        )
        if not data.get("ok"):
            raise AccessControlError(data.get("error", "Não foi possível salvar as alterações do usuário."))
        return data.get("username", new_username)

    def delete_user(self, username: str) -> None:
        data = self._call("delete_user", username=username)
        if not data.get("ok"):
            raise AccessControlError(data.get("error", "Não foi possível excluir o usuário."))

    def get_user_login_info(self, username: str) -> dict:
        """
        Só usado internamente pra validar login — nunca exibido em tela.
        Retorna {"password_hash": str, "acesso_direto": bool}.
        """
        data = self._call("get_user_password_hash", username=username)
        return {
            "password_hash": data.get("password_hash") or "",
            "acesso_direto": bool(data.get("acesso_direto")),
        }
