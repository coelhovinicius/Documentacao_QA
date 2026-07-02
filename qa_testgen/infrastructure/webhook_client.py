import json
import requests

class WebhookClient:
    def __init__(self, config):
        self.config = config
        api_key = config.api_key if hasattr(config, 'api_key') else None
        self.headers = {"x-api-key": api_key} if api_key else {}

    def _parse(self, response: requests.Response) -> dict:
        raw = response.text.strip()
        if not raw:
            raise ValueError(
                f"Payload vazio do orquestrador (Status {response.status_code}). "
                "Causa raiz provável: Deadlock no Merge Node do n8n ou falha de roteamento de rede."
            )
        if raw.startswith("```json"):
            raw = raw.replace("```json", "").replace("```", "").strip()
        elif raw.startswith("```"):
            raw = raw.replace("```", "").strip()
        return json.loads(raw)

    def _extract(self, raw, key: str) -> list:
        if isinstance(raw, list):
            for item in raw:
                result = self._extract(item, key)
                if result:
                    return result
            return []
        if not isinstance(raw, dict):
            return []
        if key in raw and isinstance(raw[key], list):
            return raw[key]
        for value in raw.values():
            if isinstance(value, dict):
                result = self._extract(value, key)
                if result:
                    return result
            elif isinstance(value, list):
                for item in value:
                    result = self._extract(item, key)
                    if result:
                        return result
            elif isinstance(value, str):
                try:
                    result = self._extract(json.loads(value), key)
                    if result:
                        return result
                except Exception:
                    pass
        return []

    def trigger_analysis(self, doc_text: str, project: str) -> dict:
        response = requests.post(
            self.config.webhook_analysis,
            json={"document_text": doc_text, "nome_projeto": project},
            headers=self.headers,
            timeout=120,
        )
        response.raise_for_status()
        data = self._parse(response)
        return {"duvidas": self._extract(data, "duvidas")}

    def trigger_matrix(self, doc_text: str, answers: dict, project: str) -> dict:
        response = requests.post(
            self.config.webhook_matrix,
            json={
                "document_text": doc_text,
                "respostas_duvidas": json.dumps(answers, ensure_ascii=False),
                "nome_projeto": project,
            },
            headers=self.headers,
            timeout=300,
        )
        response.raise_for_status()
        data = self._parse(response)
        return {"matriz": self._extract(data, "matriz")}

    def trigger_generation(self, doc_text: str, matriz: list, answers: dict, project: str) -> dict:
        response = requests.post(
            self.config.webhook_generation,
            json={
                "document_text": doc_text,
                "matriz_cobertura": json.dumps(matriz, ensure_ascii=False),
                "respostas_duvidas": json.dumps(answers, ensure_ascii=False),
                "nome_projeto": project,
            },
            headers=self.headers,
            timeout=300,
        )
        response.raise_for_status()
        data = self._parse(response)
        return {"casos_de_teste": self._extract(data, "casos_de_teste")}

    def trigger_plans(
        self, doc_text: str, matriz: list, test_cases: list, answers: dict, project: str
    ) -> dict:
        response = requests.post(
            self.config.webhook_plans,
            json={
                "document_text": doc_text,
                "matriz_cobertura": json.dumps(matriz, ensure_ascii=False),
                "casos_de_teste": json.dumps(test_cases, ensure_ascii=False),
                "respostas_duvidas": json.dumps(answers, ensure_ascii=False),
                "nome_projeto": project,
            },
            headers=self.headers,
            timeout=300,
        )
        response.raise_for_status()
        data = self._parse(response)
        return {"planos_de_teste": self._extract(data, "planos_de_teste")}
