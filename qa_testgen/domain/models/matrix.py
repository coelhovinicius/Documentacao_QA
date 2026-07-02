from dataclasses import dataclass

@dataclass
class MatrixRow:
    id: str
    funcionalidade: str
    requisito: str
    cenario: str
    categoria: str
    prioridade: str
    criticidade: str
    observacoes: str = ""
