from dataclasses import dataclass, field
from typing import List

@dataclass
class TestSuite:
    nome: str
    descricao: str = ""
    casos: List[str] = field(default_factory=list)

@dataclass
class TestPlan:
    nome: str
    descricao: str = ""
    suites: List[TestSuite] = field(default_factory=list)
