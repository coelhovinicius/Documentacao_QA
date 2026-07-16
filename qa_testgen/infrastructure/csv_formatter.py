import csv
import io


class AzureCsvFormatter:
    CASES_HEADER = [
        "ID",
        "Work Item Type",
        "Title",
        "Test Step",
        "Pre condicoes",
        "Step Action",
        "Step Expected",
        "Automation Status",
        "Area Path",
        "Assigned To",
        "State",
    ]

    @staticmethod
    def _write(rows: list) -> str:
        output = io.StringIO()
        writer = csv.writer(output, delimiter=",", lineterminator="\n")
        writer.writerows(rows)
        return output.getvalue().rstrip("\n")

    @staticmethod
    def _text(value) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _titled(test_cases: list) -> dict:
        """
        Mapeia título original -> título prefixado (CT01, CT02, ...), na ordem
        em que os casos aparecem em test_cases. Usado nos dois CSVs para que
        o mesmo caso sempre receba o mesmo número, independente de qual
        exportação está sendo gerada.
        """
        mapping = {}
        for idx, tc in enumerate(test_cases or [], start=1):
            titulo = tc.get('titulo', '')
            mapping[titulo] = f"CT{idx:02d} - {titulo}"
        return mapping

    @staticmethod
    def cases_only(test_cases: list, project_name: str) -> str:
        rows = [AzureCsvFormatter.CASES_HEADER]
        titled = AzureCsvFormatter._titled(test_cases)

        for tc in test_cases or []:
            rows.append([
                "",
                "Test Case",
                AzureCsvFormatter._text(titled.get(tc.get('titulo'), tc.get('titulo'))),
                "",
                AzureCsvFormatter._text(tc.get('pre_condicoes')),
                "",
                "",
                "Not Automated",
                AzureCsvFormatter._text(project_name),
                "",
                "Design",
            ])
            for step in tc.get('passos', []):
                rows.append([
                    "",
                    "",
                    "",
                    AzureCsvFormatter._text(step.get('numero')),
                    "",
                    AzureCsvFormatter._text(step.get('acao')),
                    AzureCsvFormatter._text(step.get('resultado_esperado')),
                    "",
                    "",
                    "",
                    "",
                ])
        return AzureCsvFormatter._write(rows)

    @staticmethod
    def plans_suites_cases(test_plans: list, test_cases: list, project_name: str) -> str:
        rows = [AzureCsvFormatter.CASES_HEADER + ["Suite", "Plan"]]
        cases_index = {tc.get('titulo', ''): tc for tc in test_cases or []}
        titled = AzureCsvFormatter._titled(test_cases)

        for plan in test_plans or []:
            plan_name = AzureCsvFormatter._text(plan.get('nome'))
            for suite in plan.get('suites', []):
                suite_name = AzureCsvFormatter._text(suite.get('nome'))
                for case_titulo in suite.get('casos', []):
                    tc = cases_index.get(case_titulo)
                    if not tc:
                        continue
                    rows.append([
                        "",
                        "Test Case",
                        AzureCsvFormatter._text(titled.get(case_titulo, case_titulo)),
                        "",
                        AzureCsvFormatter._text(tc.get('pre_condicoes')),
                        "",
                        "",
                        "Not Automated",
                        AzureCsvFormatter._text(project_name),
                        "",
                        "Design",
                        suite_name,
                        plan_name,
                    ])
                    for step in tc.get('passos', []):
                        rows.append([
                            "",
                            "",
                            "",
                            AzureCsvFormatter._text(step.get('numero')),
                            "",
                            AzureCsvFormatter._text(step.get('acao')),
                            AzureCsvFormatter._text(step.get('resultado_esperado')),
                            "",
                            "",
                            "",
                            "",
                            suite_name,
                            plan_name,
                        ])
        return AzureCsvFormatter._write(rows)
