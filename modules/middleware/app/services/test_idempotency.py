import unittest

from .idempotency import IdempotencyManager


class TestIdempotencyManager(unittest.TestCase):
    def test_check_idempotency(self):
        manager = IdempotencyManager(db_path=":memory:")
        self.assertFalse(manager.check_idempotency("job1", "sku1", "hash1"))
        manager.mark_processed("job1", "sku1", "hash1")
        self.assertTrue(manager.check_idempotency("job1", "sku1", "hash1"))
