"""
Modelos do módulo Testes de API.

Tudo aqui é dicionário-amigável (to_dict/from_dict) porque o st.session_state
e os editores de tabela do Streamlit trabalham melhor com dict do que com
objetos — os dataclasses servem pra documentar o formato e validar o mínimo.
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional


# Tipos de asserção suportados pelo runner (Python puro — sem JavaScript do
# Postman). "alvo" e "valor" mudam de significado conforme o tipo:
#   status              -> valor: código HTTP esperado (ex.: 200)
#   json_exists         -> alvo: caminho no JSON (ex.: data.token)
#   json_absent         -> alvo: caminho que NÃO deve existir (ex.: errors)
#   json_equals         -> alvo: caminho, valor: valor esperado (string/número/bool)
#   json_not_empty      -> alvo: caminho; string/lista/objeto não pode estar vazio
#   json_type           -> alvo: caminho, valor: string|number|boolean|array|object
#   json_contains       -> alvo: caminho, valor: trecho que o texto deve conter
#   json_equals_var     -> alvo: caminho, valor: nome de variável (compara com valor salvo)
#   header_contains     -> alvo: nome do header, valor: trecho esperado
#   body_contains       -> valor: trecho que o corpo bruto deve conter
#   body_not_contains   -> valor: trecho que o corpo bruto NÃO deve conter
#   response_time_max   -> valor: tempo máximo em ms
ASSERTION_TYPES = [
    "status", "json_exists", "json_absent", "json_equals", "json_not_empty",
    "json_type", "json_contains", "json_equals_var", "header_contains",
    "body_contains", "body_not_contains", "response_time_max",
]

ASSERTION_LABELS = {
    "status": "Status HTTP igual a",
    "json_exists": "Campo JSON existe",
    "json_absent": "Campo JSON NÃO existe",
    "json_equals": "Campo JSON igual a",
    "json_not_empty": "Campo JSON não vazio",
    "json_type": "Campo JSON é do tipo",
    "json_contains": "Campo JSON contém",
    "json_equals_var": "Campo JSON igual à variável",
    "header_contains": "Header contém",
    "body_contains": "Corpo contém",
    "body_not_contains": "Corpo NÃO contém",
    "response_time_max": "Tempo de resposta (ms) até",
}

HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@dataclass
class ApiAssertion:
    tipo: str
    alvo: str = ""
    valor: str = ""
    descricao: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ApiAssertion":
        return ApiAssertion(
            tipo=str(data.get("tipo", "status")),
            alvo=str(data.get("alvo", "") or ""),
            valor=str(data.get("valor", "") if data.get("valor") is not None else ""),
            descricao=str(data.get("descricao", "") or ""),
        )


@dataclass
class ApiVariableExtraction:
    """Depois da resposta, guarda `caminho` do JSON na variável `nome`."""
    nome: str
    caminho: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApiTestCase:
    id: str
    nome: str
    metodo: str = "GET"
    url: str = ""
    headers: dict = field(default_factory=dict)
    body: str = ""
    assercoes: List[ApiAssertion] = field(default_factory=list)
    extrair: List[ApiVariableExtraction] = field(default_factory=list)
    habilitado: bool = True
    descricao: str = ""
    # Avisos do importador (ex.: script JS que não pôde ser convertido).
    avisos: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nome": self.nome,
            "metodo": self.metodo,
            "url": self.url,
            "headers": dict(self.headers),
            "body": self.body,
            "assercoes": [a.to_dict() for a in self.assercoes],
            "extrair": [e.to_dict() for e in self.extrair],
            "habilitado": self.habilitado,
            "descricao": self.descricao,
            "avisos": list(self.avisos),
        }

    @staticmethod
    def from_dict(data: dict) -> "ApiTestCase":
        return ApiTestCase(
            id=str(data.get("id", "")),
            nome=str(data.get("nome", "")),
            metodo=str(data.get("metodo", "GET")).upper(),
            url=str(data.get("url", "")),
            headers=dict(data.get("headers") or {}),
            body=str(data.get("body", "") or ""),
            assercoes=[ApiAssertion.from_dict(a) for a in (data.get("assercoes") or [])],
            extrair=[ApiVariableExtraction(str(e.get("nome", "")), str(e.get("caminho", ""))) for e in (data.get("extrair") or [])],
            habilitado=bool(data.get("habilitado", True)),
            descricao=str(data.get("descricao", "") or ""),
            avisos=list(data.get("avisos") or []),
        )


@dataclass
class ApiAssertionResult:
    descricao: str
    passou: bool
    detalhe: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApiCaseResult:
    case_id: str
    nome: str
    metodo: str
    url_final: str
    request_headers: dict
    request_body: str
    status_code: Optional[int]
    status_text: str
    response_headers: dict
    response_body: str
    tempo_ms: int
    assercoes: List[ApiAssertionResult] = field(default_factory=list)
    erro: str = ""
    pulado: bool = False

    @property
    def passou(self) -> bool:
        if self.pulado or self.erro:
            return False
        return all(a.passou for a in self.assercoes)

    @property
    def resultado_label(self) -> str:
        if self.pulado:
            return "Não Executado"
        if self.erro:
            return "Erro"
        return "Aprovado" if self.passou else "Reprovado"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passou"] = self.passou
        data["resultado_label"] = self.resultado_label
        return data
