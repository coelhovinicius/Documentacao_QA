from dataclasses import dataclass, field
from typing import List

from .teststep import TestStep

@dataclass
class TestCase:
    titulo: str
    pre_condicoes: str
    passos: List[TestStep] = field(default_factory=list)
    requisitos_relacionados: List[str] = field(default_factory=list)  # IDs da Matriz (ex.: "MC-001")
