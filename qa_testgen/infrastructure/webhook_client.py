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

    def _find_key(self, raw, key: str):
        if isinstance(raw, list):
            for item in raw:
                found, value = self._find_key(item, key)
                if found:
                    return True, value
            return False, None
        if isinstance(raw, dict):
            if key in raw:
                return True, raw[key]
            for value in raw.values():
                found, result = self._find_key(value, key)
                if found:
                    return True, result
            return False, None
        if isinstance(raw, str):
            try:
                return self._find_key(json.loads(raw), key)
            except Exception:
                return False, None
        return False, None

    def _preview(self, raw) -> str:
        try:
            text = json.dumps(raw, ensure_ascii=False)
        except Exception:
            text = str(raw)
        return text[:800] + ("..." if len(text) > 800 else "")

    def _try_parse_json(self, raw: str):
        if not isinstance(raw, str):
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _extract_json_object(self, raw: str):
        if not isinstance(raw, str):
            return None
        start = raw.find('{')
        end = raw.rfind('}')
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            return None

    def _looks_like_matrix_row(self, item) -> bool:
        if not isinstance(item, dict):
            return False
        expected = {
            'id',
            'funcionalidade',
            'requisito',
            'cenario',
            'categoria',
            'prioridade',
            'criticidade',
            'observacoes',
        }
        return len(expected.intersection(item.keys())) >= 5

    def _looks_like_matrix_list(self, value) -> bool:
        if not isinstance(value, list) or not value:
            return False
        return all(self._looks_like_matrix_row(item) for item in value if isinstance(item, dict))

    def _extract_required_list(self, raw, key: str) -> list:
        found, value = self._find_key(raw, key)
        if not found:
            if self._looks_like_matrix_list(raw):
                return raw
            if self._looks_like_matrix_row(raw):
                return [raw]
            if isinstance(raw, dict):
                for candidate in raw.values():
                    if self._looks_like_matrix_list(candidate):
                        return candidate
            raise ValueError(
                f"Resposta do orquestrador não contém a chave obrigatória '{key}'. "
                f"Prévia do payload recebido: {self._preview(raw)}"
            )

        if isinstance(value, str):
            parsed = self._try_parse_json(value)
            if isinstance(parsed, list):
                return parsed

        if not isinstance(value, list):
            raise ValueError(
                f"A chave obrigatória '{key}' foi encontrada, mas não é uma lista. "
                f"Prévia do valor recebido: {self._preview(value)}"
            )
        return value

    def _extract(self, raw, key: str) -> list:
        found, value = self._find_key(raw, key)
        return value if found and isinstance(value, list) else []

    def trigger_analysis(self, doc_text: str, project: str) -> dict:
        response = requests.post(
            self.config.webhook_analysis,
            json={"document_text": doc_text, "nome_projeto": project},
            headers=self.headers,
            timeout=120,
        )
        response.raise_for_status()
        data = self._parse(response)
        return {"duvidas": self._extract_required_list(data, "duvidas")}

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
        return {"matriz": self._extract_required_list(data, "matriz")}

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
        return {"casos_de_teste": self._extract_required_list(data, "casos_de_teste")}

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
        return {"planos_de_teste": self._extract_required_list(data, "planos_de_teste")}

    def trigger_matching(self, work_items: list, test_cases: list, project: str) -> dict:
        """
        Chama o webhook do n8n responsável por sugerir o vínculo entre Casos
        de Teste gerados e Work Items existentes no board do Azure DevOps.

        Contrato esperado da resposta do n8n:
        {
            "vinculos": [
                {"work_item_id": 123, "casos": ["Título do Caso 1", "Título do Caso 2"]},
                {"work_item_id": 456, "casos": ["Título do Caso 3"]}
            ]
        }
        Work Items sem nenhum caso relacionado simplesmente não precisam
        aparecer na lista (ou podem aparecer com "casos": []).
        """
        response = requests.post(
            self.config.webhook_matching,
            json={
                "work_items": json.dumps(work_items, ensure_ascii=False),
                "casos_de_teste": json.dumps(test_cases, ensure_ascii=False),
                "nome_projeto": project,
            },
            headers=self.headers,
            timeout=180,
        )
        response.raise_for_status()
        data = self._parse(response)
        return {"vinculos": self._extract_required_list(data, "vinculos")}
