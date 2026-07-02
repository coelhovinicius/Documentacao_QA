from dataclasses import dataclass

@dataclass
class TestStep:
    numero: int
    acao: str
    resultado_esperado: str
