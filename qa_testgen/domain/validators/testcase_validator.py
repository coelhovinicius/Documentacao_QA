from typing import List

from ..models.testcase import TestCase

class TestCaseValidator:
    @staticmethod
    def validate(test_case: TestCase) -> List[str]:
        missing = []
        if not test_case.titulo.strip():
            missing.append("Título")
        if not test_case.pre_condicoes.strip():
            missing.append("Pré-condições")
        if not test_case.passos:
            missing.append("ao menos 1 Step")
        else:
            for index, step in enumerate(test_case.passos, start=1):
                if not step.acao.strip():
                    missing.append(f"Ação do Step {index}")
                if not step.resultado_esperado.strip():
                    missing.append(f"Esperado do Step {index}")
        return missing
