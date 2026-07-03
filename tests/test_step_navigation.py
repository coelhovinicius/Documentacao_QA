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


if __name__ == "__main__":
    unittest.main()
