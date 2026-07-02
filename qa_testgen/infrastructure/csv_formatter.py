class AzureCsvFormatter:
    @staticmethod
    def cases_only(test_cases: list, project_name: str) -> str:
        header = (
            "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;"
            "Step Expected;Automation Status;Area Path;Assigned To;State"
        )
        if not test_cases:
            return header

        lines = [header]
        for tc in test_cases:
            titulo = str(tc.get('titulo', '')).replace(';', ',')
            pre = str(tc.get('pre_condicoes', '')).replace(';', ',')
            lines.append(f";Test Case;{titulo};;{pre};;;Not Automated;{project_name};;Design")
            for step in tc.get('passos', []):
                acao = str(step.get('acao', '')).replace(';', ',')
                esp = str(step.get('resultado_esperado', '')).replace(';', ',')
                lines.append(f";;;{step.get('numero','')};;{acao};{esp};;;;")
        return "\n".join(lines)

    @staticmethod
    def plans_suites_cases(test_plans: list, test_cases: list, project_name: str) -> str:
        header = (
            "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;"
            "Step Expected;Automation Status;Area Path;Assigned To;State;Suite;Plan"
        )
        if not test_plans:
            return header

        cases_index = {tc.get('titulo', ''): tc for tc in test_cases}
        lines = [header]

        for plan in test_plans:
            plan_name = str(plan.get('nome', '')).replace(';', ',')
            for suite in plan.get('suites', []):
                suite_name = str(suite.get('nome', '')).replace(';', ',')
                for case_titulo in suite.get('casos', []):
                    tc = cases_index.get(case_titulo)
                    if not tc:
                        continue
                    titulo = str(tc.get('titulo', '')).replace(';', ',')
                    pre = str(tc.get('pre_condicoes', '')).replace(';', ',')
                    lines.append(
                        f";Test Case;{titulo};;{pre};;;Not Automated;{project_name};;Design;{suite_name};{plan_name}"
                    )
                    for step in tc.get('passos', []):
                        acao = str(step.get('acao', '')).replace(';', ',')
                        esp = str(step.get('resultado_esperado', '')).replace(';', ',')
                        lines.append(
                            f";;;{step.get('numero','')};;{acao};{esp};;;;;{suite_name};{plan_name}"
                        )
        return "\n".join(lines)
