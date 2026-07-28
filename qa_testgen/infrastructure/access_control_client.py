import requests


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
