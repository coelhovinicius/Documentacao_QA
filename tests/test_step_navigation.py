import unittest

from qa_testgen.ui.application import UserInterface


class StepNavigationTests(unittest.TestCase):
    def test_allows_steps_up_to_max_step(self):
        self.assertTrue(UserInterface.can_access_step(2, 3, 3, [1, 2], False))

    def test_allows_already_completed_future_steps(self):
        self.assertTrue(UserInterface.can_access_step(5, 3, 3, [1, 2, 5], False))

    def test_blocks_future_steps_not_reached_yet(self):
        self.assertFalse(UserInterface.can_access_step(5, 3, 3, [1, 2], False))

    def test_blocks_when_processing(self):
        self.assertFalse(UserInterface.can_access_step(2, 3, 3, [1, 2], True))

    def test_screen_changes_when_menu_area_changes(self):
        # a sidebar se recolhe quando _tela_atual muda — escolher uma área no menu
        # (sem mudar o passo) também precisa contar como troca de tela
        ui = UserInterface.__new__(UserInterface)
        estado = {'step': 5}
        ui.state = type("S", (), {"get": lambda self, k, d=None: estado.get(k, d)})()
        inicio = ui._tela_atual()
        estado['show_api_tests_page'] = True
        na_area = ui._tela_atual()
        self.assertNotEqual(inicio, na_area)
        estado['show_api_tests_page'] = False
        estado['show_bug_page'] = True
        self.assertNotEqual(na_area, ui._tela_atual())
        estado['show_bug_page'] = False
        self.assertEqual(inicio, ui._tela_atual())       # voltou pro assistente, mesmo passo
        estado['show_new_analysis_modal'] = True          # modal não é troca de tela
        self.assertEqual(inicio, ui._tela_atual())


if __name__ == "__main__":
    unittest.main()
