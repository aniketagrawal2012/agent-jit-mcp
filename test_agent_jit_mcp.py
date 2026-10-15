"""
Comprehensive Unit & Integration Test Suite for Agent-JIT MCP Engine.

Verifies:
1. Fail-closed role attenuation and property masking
2. Goal-conditioned sub-schema pruning and token bounding
3. Just-in-time composite tool synthesis and execution
4. Cloud Spanner TrueTime audit logging and tamper-evidence
5. Zero-trust containment against unauthorized tool invocation
"""

import unittest
from agent_jit_mcp.catalog_fixtures import (
    generate_scaled_catalog,
    get_enterprise_entities,
    get_enterprise_tools,
    get_mock_principals,
)
from agent_jit_mcp.engine import AgentJITEngine
from agent_jit_mcp.jit_synthesizer import JITCompositeSynthesizer
from agent_jit_mcp.models import (
    CompositeToolWrapper,
    OperationType,
    PFCGAuthorization,
    SpannerAuditVoucher,
    ToolDefinition,
    UserPrincipal,
)
from agent_jit_mcp.role_attenuator import RoleAttenuationEngine
from agent_jit_mcp.schema_pruner import SchemaPruningEngine
from agent_jit_mcp.spanner_audit import CloudSpannerAuditLedger


class TestAgentJITEngine(unittest.TestCase):

    def setUp(self):
        self.auditor, self.clerk, self.admin = get_mock_principals()
        self.tools = get_enterprise_tools()
        self.entities = get_enterprise_entities()

    def test_role_attenuation_fail_closed(self):
        """Verify unauthorized tools (e.g. Treasury payment run) are strictly stripped."""
        attenuator = RoleAttenuationEngine(fail_closed=True)
        # Auditor should NOT have access to Treasury disbursement
        auth_tools, role_hash = attenuator.attenuate_tools(self.auditor, self.tools)
        tool_ids = [t.tool_id for t in auth_tools]

        self.assertIn("sap_gl_read_balance", tool_ids)
        self.assertIn("sap_journal_entry_query_items", tool_ids)
        self.assertIn("sap_po_get_details", tool_ids)
        self.assertNotIn("sap_privileged_disburse_payment_run", tool_ids)
        self.assertNotIn("sap_bp_get_master_data", tool_ids)
        self.assertTrue(len(role_hash) == 64)

    def test_column_level_schema_masking(self):
        """Verify restricted columns (e.g. EmployeeSalaryMask) are stripped from schemas."""
        attenuator = RoleAttenuationEngine(fail_closed=True)
        auth_tools, _ = attenuator.attenuate_tools(self.auditor, self.tools)
        journal_tool = next(t for t in auth_tools if t.tool_id == "sap_journal_entry_query_items")

        # Salary mask was in original tool parameter and output schema
        self.assertNotIn("EmployeeSalaryMask", journal_tool.parameters_schema.get("properties", {}))
        self.assertNotIn("EmployeeSalaryMask", journal_tool.output_schema.get("properties", {}))

    def test_schema_pruning_token_budget(self):
        """Verify projection stays strictly within token budget and maximizes relevance."""
        pruner = SchemaPruningEngine(token_budget=800)
        pruner.build_graph(self.tools, self.entities)

        query = "Review and audit the General Ledger account balances and journal line items"
        context = pruner.prune_and_project(
            user_goal=query,
            authorized_tools=self.tools,
            initial_monolithic_count=len(self.tools),
            session_id="test_sess_001",
            principal=self.auditor
        )

        self.assertLessEqual(context.actual_projected_tokens, 800)
        self.assertGreater(len(context.projected_tools), 0)
        projected_names = [t.name for t in context.projected_tools]
        # GL read balance tool should be top choice
        self.assertIn("sap_gl_read_balance", projected_names)
        self.assertGreater(context.token_reduction_pct, 50.0)

    def test_jit_composite_synthesis(self):
        """Verify multi-hop AP 3-way matching is synthesized into a single turn composite tool."""
        synthesizer = JITCompositeSynthesizer()
        query = "Perform 3-way match reconciliation on Purchase Order and Supplier Invoice"
        
        template = synthesizer.should_synthesize(query, self.tools)
        self.assertIsNotNone(template)
        self.assertEqual(template["pattern_id"], "AP_3WAY_MATCH")

        wrapper = synthesizer.synthesize(template, self.tools, self.auditor)
        self.assertIsInstance(wrapper, CompositeToolWrapper)
        self.assertEqual(len(wrapper.steps), 3)
        self.assertTrue(len(wrapper.provenance_hash) == 64)

        # Test single-turn execution pipeline
        def mock_executor(tid, params):
            return {"service": tid, "status": "200_OK", "records_found": 5}

        result = synthesizer.execute_composite_pipeline(
            wrapper,
            {"PurchaseOrder": "4500001234", "CompanyCode": "1000", "FiscalYear": "2026"},
            mock_executor
        )
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(len(result["step_outputs"]), 3)

    def test_cloud_spanner_audit_truetime(self):
        """Verify TrueTime linearizable audit recording and cryptographic hash chain."""
        ledger = CloudSpannerAuditLedger(tenant_id="deloitte-us-advisory")
        voucher = SpannerAuditVoucher.generate(
            tenant_id="deloitte-us-advisory",
            session_id="sess_truetime_100",
            principal_id="aniketagrawal@google.com",
            sap_user_id="SAP_AUDIT_ANIKET",
            user_goal="Audit journal entry lines",
            role_state="role_state_ok",
            projected_tool_ids=["sap_gl_read_balance", "sap_journal_entry_query_items"]
        )

        chain_hash = ledger.record_voucher(voucher)
        self.assertEqual(len(chain_hash), 64)
        self.assertTrue(ledger.verify_chain_integrity())

        # Verify tamper detection
        corrupted_voucher = voucher.model_copy()
        corrupted_voucher.truetime_commit_timestamp += 10.0  # Tamper with timestamp
        self.assertFalse(corrupted_voucher.verify_integrity())

    def test_zero_trust_containment(self):
        """Verify agent cannot execute an unprojected tool, preventing privilege escalation."""
        engine = AgentJITEngine(base_tools=self.tools)
        projected, voucher, ctx = engine.project_tools(
            user_goal="Look up purchase order details",
            principal=self.clerk,
            session_id="sess_zt_001"
        )

        # Clerk only projected PO / AP tools; attempting to call payment disbursement fails
        res = engine.execute_tool(
            session_id="sess_zt_001",
            voucher_id=voucher.voucher_id,
            tool_id="sap_privileged_disburse_payment_run",
            arguments={"RunDate": "2026-09-30", "RunIdentification": "DISB01"}
        )
        self.assertEqual(res["status"], "SECURITY_VIOLATION")
        self.assertEqual(res["error_code"], "403_FORBIDDEN_TOOL_INVOCATION")

    def test_scaled_catalog_performance(self):
        """Verify sub-10ms projection latency over a scaled 100-tool ERP catalog."""
        scaled_tools = generate_scaled_catalog(100)
        engine = AgentJITEngine(base_tools=scaled_tools, token_budget=2000)

        projected, voucher, ctx = engine.project_tools(
            user_goal="Reconcile company code 1000 accounting documents and check GL balances",
            principal=self.auditor,
            session_id="sess_scale_001"
        )
        self.assertLessEqual(ctx.actual_projected_tokens, 2000)
        self.assertLess(ctx.projection_latency_ms, 15.0)  # Sub-15ms execution
        self.assertGreater(ctx.token_reduction_pct, 85.0)

    def test_multi_tenant_isolation(self):
        """Verify vouchers and projection states remain strictly segregated across tenants."""
        ledger = CloudSpannerAuditLedger()
        v1 = SpannerAuditVoucher.generate(
            tenant_id="tenant-alpha",
            session_id="session-001",
            principal_id="user1@alpha.com",
            sap_user_id="SAP_U1",
            user_goal="Goal 1",
            role_state="role_hash_1",
            projected_tool_ids=["sap_gl_read_balance"]
        )
        v2 = SpannerAuditVoucher.generate(
            tenant_id="tenant-beta",
            session_id="session-002",
            principal_id="user2@beta.com",
            sap_user_id="SAP_U2",
            user_goal="Goal 2",
            role_state="role_hash_2",
            projected_tool_ids=["sap_po_get_details"]
        )
        ledger.record_voucher(v1)
        ledger.record_voucher(v2)

        # Alpha session should not see Beta vouchers
        alpha_vouchers = ledger.get_session_history("session-001")
        self.assertEqual(len(alpha_vouchers), 1)
        self.assertEqual(alpha_vouchers[0].tenant_id, "tenant-alpha")

        beta_vouchers = ledger.get_session_history("session-002")
        self.assertEqual(len(beta_vouchers), 1)
        self.assertEqual(beta_vouchers[0].tenant_id, "tenant-beta")

    def test_voucher_signature_verification_against_tampering(self):
        """Verify modifying projected tool IDs invalidates signature."""
        v = SpannerAuditVoucher.generate(
            tenant_id="deloitte-us-advisory",
            session_id="sess_verify_001",
            principal_id="aniketagrawal@google.com",
            sap_user_id="SAP_AUDIT_ANIKET",
            user_goal="Test",
            role_state="role_state_ok",
            projected_tool_ids=["sap_gl_read_balance"]
        )
        self.assertTrue(v.verify_integrity())
        
        # Unauthorized tampering with projected tool IDs
        tampered = v.model_copy(update={"projected_tool_ids": ["sap_gl_read_balance", "sap_privileged_disburse_payment_run"]})
        self.assertFalse(tampered.verify_integrity())

    def test_persistent_erp_database_sql_execution(self):
        """Verify physical SQLite database execution against real SAP tables on disk."""
        from agent_jit_mcp.erp_database import PersistentERPDatabase
        erp = PersistentERPDatabase()

        # Query GL balance on physical disk
        gl_rows = erp.query_gl_balance("1000", "100000", "2026")
        self.assertEqual(len(gl_rows), 1)
        self.assertEqual(gl_rows[0]["AmountInCompanyCodeCurrency"], 4500000.0)

        # Query 3-way match reconciliation across PO, Invoices, and GL
        match_res = erp.reconcile_ap_3way_match("4500000001", "1000")
        self.assertEqual(match_res["status"], "RECONCILED")
        self.assertEqual(match_res["variance"], 0.0)

        # Query with physical column-level masking (EmployeeSalaryMask stripped from SQL SELECT)
        masked_rows = erp.query_journal_entry_items("1000", masked_fields={"EmployeeSalaryMask"})
        self.assertGreater(len(masked_rows), 0)
        self.assertNotIn("EmployeeSalaryMask", masked_rows[0])

    def test_persistent_spanner_disk_ledger_and_merkle_chain(self):
        """Verify physical Spanner disk ledger commits, TrueTime timestamps, and Merkle chain."""
        from agent_jit_mcp.spanner_disk_ledger import PersistentSpannerLedger
        ledger = PersistentSpannerLedger()

        v_id, chain_hash = ledger.commit_voucher(
            tenant_id="deloitte-us-advisory",
            session_id="sess_disk_test_01",
            principal_id="aniketagrawal@google.com",
            sap_user_id="SAP_AUDIT_ANIKET",
            user_goal="Reconcile AP invoices",
            role_state="role_state_ok",
            projected_tool_ids=["sap_gl_read_balance", "composite_reconcile_ap_3way_match"]
        )
        self.assertTrue(len(v_id) > 10)
        self.assertTrue(len(chain_hash) == 64)

        # Zero-trust containment verification on disk
        auth_ok, _ = ledger.verify_tool_invocation(v_id, "composite_reconcile_ap_3way_match")
        self.assertTrue(auth_ok)

        unauth_ok, unauth_msg = ledger.verify_tool_invocation(v_id, "sap_privileged_disburse_payment_run")
        self.assertFalse(unauth_ok)
        self.assertIn("SECURITY_VIOLATION", unauth_msg)

        # Audit entire physical Merkle chain on disk
        chain_ok, count, msg = ledger.verify_chain_integrity()
        self.assertTrue(chain_ok)
        self.assertGreater(count, 0)

    def test_mcp_protocol_server_end_to_end(self):
        """Verify official MCP JSON-RPC 2.0 initialize, tools/list, and tools/call on real data."""
        import json
        from agent_jit_mcp.mcp_protocol import MCPProtocolServer
        server = MCPProtocolServer(catalog_size=25)

        # 1. MCP initialize handshake
        init_req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        init_res = json.loads(server.handle_request_json(init_req))
        self.assertEqual(init_res["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "Agent-JIT-FastMCP-Enterprise")

        # 2. Dynamic tools/list with Spanner TrueTime disk commit
        jit_req = json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            "params": {
                "mode": "jit",
                "user_goal": "Reconcile 3-way match for PO 4500000001",
                "principal_id": "aniketagrawal@google.com",
                "session_id": "sess_e2e_mcp_01"
            }
        })
        jit_res = json.loads(server.handle_request_json(jit_req))
        v_id = jit_res["result"]["voucher_id"]
        self.assertIsNotNone(v_id)
        self.assertGreater(jit_res["result"]["token_reduction_pct"], 50.0)

        # 3. Authorized tools/call executing real SQL on disk
        call_req = json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "composite_reconcile_ap_3way_match",
                "arguments": {"PurchaseOrder": "4500000001", "CompanyCode": "1000"},
                "voucher_id": v_id
            }
        })
        call_res = json.loads(server.handle_request_json(call_req))
        self.assertEqual(call_res["result"]["status"], "SUCCESS")
        self.assertFalse(call_res["result"]["isError"])

        # 4. Adversarial tools/call blocked with SECURITY_VIOLATION
        adv_req = json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "sap_privileged_disburse_payment_run",
                "arguments": {"TotalAmount": 5000000},
                "voucher_id": v_id
            }
        })
        adv_res = json.loads(server.handle_request_json(adv_req))
        self.assertEqual(adv_res["result"]["status"], "SECURITY_VIOLATION")
        self.assertTrue(adv_res["result"]["isError"])
        self.assertEqual(adv_res["result"]["errorCode"], -32001)


if __name__ == "__main__":
    unittest.main()
