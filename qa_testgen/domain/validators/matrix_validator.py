from typing import List

from ..models.matrix import MatrixRow

class MatrixValidator:
    @staticmethod
    def validate(row: MatrixRow) -> List[str]:
        missing = []
        if not row.id.strip(): missing.append("ID")
        if not row.funcionalidade.strip(): missing.append("Funcionalidade")
        if not row.requisito.strip(): missing.append("Requisito")
        if not row.cenario.strip(): missing.append("Cenário")
        if not row.categoria.strip(): missing.append("Categoria")
        if not row.prioridade.strip(): missing.append("Prioridade")
        if not row.criticidade.strip(): missing.append("Criticidade")
        return missing
