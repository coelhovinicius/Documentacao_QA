from typing import List

from ..models.testplan import TestPlan

class TestPlanValidator:
    @staticmethod
    def validate(test_plan: TestPlan) -> List[str]:
        missing = []
        if not test_plan.nome.strip():
            missing.append("Nome do Plano")
        if not test_plan.suites:
            missing.append("ao menos 1 Suite")
        else:
            for index, suite in enumerate(test_plan.suites, start=1):
                if not suite.nome.strip():
                    missing.append(f"Nome da Suite {index}")
                if not suite.casos:
                    missing.append(f"Casos vinculados à Suite {index}")
        return missing
