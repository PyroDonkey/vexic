import unittest


class VexicContractImportTests(unittest.TestCase):
    def test_contract_exposes_memory_service_protocol(self) -> None:
        from vexic.contract import CONTRACT_VERSION, MemoryService, MemoryScope

        self.assertEqual(CONTRACT_VERSION, "0.1.0")
        self.assertTrue(hasattr(MemoryService, "search_long_term"))
        self.assertEqual(MemoryScope.model_fields["tenant_id"].is_required(), True)


if __name__ == "__main__":
    unittest.main()
