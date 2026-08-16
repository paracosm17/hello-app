import unittest


class IntentionalCiFailure(unittest.TestCase):
    def test_required_ci_blocks_merge(self):
        self.fail("intentional hello-app CI negative proof")


if __name__ == "__main__":
    unittest.main()
