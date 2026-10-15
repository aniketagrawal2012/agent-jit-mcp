"""
Agent-JIT: Enterprise SAP S/4HANA Catalog Fixtures & Tool Definitions.

Provides realistic enterprise OData v2/v4 services, entity sets, navigation
relationships, and PFCG role profiles modeling production ERP architectures.
"""

from __future__ import annotations

from typing import List, Tuple
from agent_jit_mcp.models import (
    ODataEntitySet,
    ODataField,
    ODataNavigationProperty,
    OperationType,
    PFCGAuthorization,
    SensitivityLevel,
    ToolDefinition,
    UserPrincipal,
)


def get_enterprise_entities() -> List[ODataEntitySet]:
    """Returns canonical SAP S/4HANA OData entity sets with schema fields and foreign keys."""
    return [
        ODataEntitySet(
            service_name="API_GLACCOUNTBALANCE_SRV",
            entity_name="A_GLAccountBalance",
            description="General Ledger Account balances aggregated by company code and fiscal year",
            fields={
                "CompanyCode": ODataField(name="CompanyCode", type_name="Edm.String", is_key=True, label="Company Code"),
                "FiscalYear": ODataField(name="FiscalYear", type_name="Edm.String", is_key=True, label="Fiscal Year"),
                "GLAccount": ODataField(name="GLAccount", type_name="Edm.String", is_key=True, label="GL Account Number"),
                "AmountInCompanyCodeCurrency": ODataField(name="AmountInCompanyCodeCurrency", type_name="Edm.Decimal", label="Balance Amount"),
                "CompanyCodeCurrency": ODataField(name="CompanyCodeCurrency", type_name="Edm.String", label="Currency Code")
            },
            primary_keys=["CompanyCode", "FiscalYear", "GLAccount"],
            navigation_properties=[
                ODataNavigationProperty(name="to_JournalEntryItems", target_entity="A_JournalEntryItemBasic")
            ]
        ),
        ODataEntitySet(
            service_name="API_JOURNALENTRYITEMBASIC_SRV",
            entity_name="A_JournalEntryItemBasic",
            description="Detailed accounting document line items recorded in Universal Journal (ACDOCA)",
            fields={
                "CompanyCode": ODataField(name="CompanyCode", type_name="Edm.String", is_key=True, label="Company Code"),
                "FiscalYear": ODataField(name="FiscalYear", type_name="Edm.String", is_key=True, label="Fiscal Year"),
                "AccountingDocument": ODataField(name="AccountingDocument", type_name="Edm.String", is_key=True, label="Doc Number"),
                "LedgerGLLineItem": ODataField(name="LedgerGLLineItem", type_name="Edm.String", is_key=True, label="Line Item"),
                "GLAccount": ODataField(name="GLAccount", type_name="Edm.String", label="GL Account"),
                "AmountInCompanyCodeCurrency": ODataField(name="AmountInCompanyCodeCurrency", type_name="Edm.Decimal", label="Amount"),
                "EmployeeSalaryMask": ODataField(name="EmployeeSalaryMask", type_name="Edm.Decimal", sensitivity=SensitivityLevel.RESTRICTED, label="Payroll Detail"),
                "PostingDate": ODataField(name="PostingDate", type_name="Edm.DateTime", label="Posting Date")
            },
            primary_keys=["CompanyCode", "FiscalYear", "AccountingDocument", "LedgerGLLineItem"],
            navigation_properties=[
                ODataNavigationProperty(name="to_GLAccountBalance", target_entity="A_GLAccountBalance")
            ]
        ),
        ODataEntitySet(
            service_name="API_PURCHASEORDER_PROCESS_SRV",
            entity_name="A_PurchaseOrder",
            description="Purchase order header in Procurement ledger",
            fields={
                "PurchaseOrder": ODataField(name="PurchaseOrder", type_name="Edm.String", is_key=True, label="PO Number"),
                "CompanyCode": ODataField(name="CompanyCode", type_name="Edm.String", label="Company Code"),
                "Supplier": ODataField(name="Supplier", type_name="Edm.String", label="Supplier ID"),
                "PurchaseOrderDate": ODataField(name="PurchaseOrderDate", type_name="Edm.DateTime", label="Creation Date")
            },
            primary_keys=["PurchaseOrder"],
            navigation_properties=[
                ODataNavigationProperty(name="to_SupplierInvoice", target_entity="A_SupplierInvoice")
            ]
        ),
        ODataEntitySet(
            service_name="API_SUPPLIERINVOICE_PROCESS_SRV",
            entity_name="A_SupplierInvoice",
            description="Supplier invoice header in Accounts Payable",
            fields={
                "SupplierInvoice": ODataField(name="SupplierInvoice", type_name="Edm.String", is_key=True, label="Invoice ID"),
                "FiscalYear": ODataField(name="FiscalYear", type_name="Edm.String", is_key=True, label="Fiscal Year"),
                "CompanyCode": ODataField(name="CompanyCode", type_name="Edm.String", label="Company Code"),
                "InvoicingParty": ODataField(name="InvoicingParty", type_name="Edm.String", label="Vendor ID"),
                "InvoiceGrossAmount": ODataField(name="InvoiceGrossAmount", type_name="Edm.Decimal", label="Gross Amount")
            },
            primary_keys=["SupplierInvoice", "FiscalYear"],
            navigation_properties=[
                ODataNavigationProperty(name="to_JournalEntry", target_entity="A_JournalEntryItemBasic")
            ]
        ),
        ODataEntitySet(
            service_name="API_BUSINESS_PARTNER",
            entity_name="A_BusinessPartner",
            description="Master data for customers, suppliers, and internal entities",
            fields={
                "BusinessPartner": ODataField(name="BusinessPartner", type_name="Edm.String", is_key=True, label="BP Number"),
                "BusinessPartnerCategory": ODataField(name="BusinessPartnerCategory", type_name="Edm.String", label="Category"),
                "BankAccountNumber": ODataField(name="BankAccountNumber", type_name="Edm.String", sensitivity=SensitivityLevel.CONFIDENTIAL, label="Bank Account")
            },
            primary_keys=["BusinessPartner"],
            navigation_properties=[]
        )
    ]


def get_enterprise_tools() -> List[ToolDefinition]:
    """Returns realistic SAP OData atomic tools registered in FastMCP."""
    return [
        ToolDefinition(
            tool_id="sap_gl_read_balance",
            name="sap_gl_read_balance",
            description="Query general ledger account balances for a company code and fiscal year.",
            service_name="API_GLACCOUNTBALANCE_SRV",
            entity_name="A_GLAccountBalance",
            operation=OperationType.QUERY,
            required_role="Z_FI_AUDITOR_READ",
            parameters_schema={
                "type": "object",
                "properties": {
                    "CompanyCode": {"type": "string", "description": "4-digit company code, e.g. 1000"},
                    "GLAccount": {"type": "string", "description": "10-digit GL account number"},
                    "FiscalYear": {"type": "string", "description": "4-digit fiscal year"}
                },
                "required": ["CompanyCode", "GLAccount"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "AmountInCompanyCodeCurrency": {"type": "number"},
                    "CompanyCodeCurrency": {"type": "string"}
                }
            },
            semantic_tags=["gl", "ledger", "balance", "audit", "finance"],
            estimated_tokens=320
        ),
        ToolDefinition(
            tool_id="sap_journal_entry_query_items",
            name="sap_journal_entry_query_items",
            description="Search line items in universal journal ACDOCA by accounting document, fiscal year, or GL account.",
            service_name="API_JOURNALENTRYITEMBASIC_SRV",
            entity_name="A_JournalEntryItemBasic",
            operation=OperationType.QUERY,
            required_role="Z_FI_AUDITOR_READ",
            parameters_schema={
                "type": "object",
                "properties": {
                    "CompanyCode": {"type": "string", "description": "Company code"},
                    "AccountingDocument": {"type": "string", "description": "Accounting doc number"},
                    "FiscalYear": {"type": "string", "description": "Fiscal year"},
                    "EmployeeSalaryMask": {"type": "number", "description": "Restricted payroll field"}
                },
                "required": ["CompanyCode"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "LedgerGLLineItem": {"type": "string"},
                    "AmountInCompanyCodeCurrency": {"type": "number"},
                    "EmployeeSalaryMask": {"type": "number"}
                }
            },
            semantic_tags=["journal", "entry", "acdoca", "line_item", "audit", "posting"],
            estimated_tokens=410
        ),
        ToolDefinition(
            tool_id="sap_po_get_details",
            name="sap_po_get_details",
            description="Retrieve procurement purchase order header and item pricing details.",
            service_name="API_PURCHASEORDER_PROCESS_SRV",
            entity_name="A_PurchaseOrder",
            operation=OperationType.READ,
            required_role="Z_MM_PURCHASING_CLERK",
            parameters_schema={
                "type": "object",
                "properties": {
                    "PurchaseOrder": {"type": "string", "description": "10-digit Purchase order number"},
                    "CompanyCode": {"type": "string", "description": "Company code"}
                },
                "required": ["PurchaseOrder"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "Supplier": {"type": "string"},
                    "PurchaseOrderDate": {"type": "string"}
                }
            },
            semantic_tags=["po", "purchase_order", "procurement", "vendor", "buyer"],
            estimated_tokens=350
        ),
        ToolDefinition(
            tool_id="sap_invoice_reconcile_match",
            name="sap_invoice_reconcile_match",
            description="Query supplier invoices in accounts payable and retrieve matching purchase orders.",
            service_name="API_SUPPLIERINVOICE_PROCESS_SRV",
            entity_name="A_SupplierInvoice",
            operation=OperationType.QUERY,
            required_role="Z_MM_PURCHASING_CLERK",
            parameters_schema={
                "type": "object",
                "properties": {
                    "SupplierInvoice": {"type": "string", "description": "Invoice number"},
                    "FiscalYear": {"type": "string", "description": "Fiscal year"}
                },
                "required": ["SupplierInvoice"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "InvoiceGrossAmount": {"type": "number"},
                    "InvoicingParty": {"type": "string"}
                }
            },
            semantic_tags=["invoice", "payable", "ap", "reconcile", "supplier"],
            estimated_tokens=380
        ),
        ToolDefinition(
            tool_id="sap_bp_get_master_data",
            name="sap_bp_get_master_data",
            description="Query business partner master profile, addresses, and sensitive bank accounts.",
            service_name="API_BUSINESS_PARTNER",
            entity_name="A_BusinessPartner",
            operation=OperationType.READ,
            required_role="Z_MD_MASTER_DATA_MAINTAINER",
            parameters_schema={
                "type": "object",
                "properties": {
                    "BusinessPartner": {"type": "string", "description": "Business partner number"},
                    "BankAccountNumber": {"type": "string", "description": "Confidential bank account"}
                },
                "required": ["BusinessPartner"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "BusinessPartnerCategory": {"type": "string"},
                    "BankAccountNumber": {"type": "string"}
                }
            },
            semantic_tags=["business_partner", "vendor", "customer", "bank", "master_data"],
            estimated_tokens=390
        ),
        ToolDefinition(
            tool_id="sap_privileged_disburse_payment_run",
            name="sap_privileged_disburse_payment_run",
            description="CRITICAL: Execute automated treasury disbursement payment run (F110) releasing wire transfers.",
            service_name="Z_PAYMENT_RUN_DISBURSE_SRV",
            entity_name="A_PaymentRun",
            operation=OperationType.EXECUTE,
            required_role="Z_TREASURY_CHIEF_OFFICER",
            parameters_schema={
                "type": "object",
                "properties": {
                    "RunDate": {"type": "string"},
                    "RunIdentification": {"type": "string"},
                    "TotalAmount": {"type": "number"}
                },
                "required": ["RunDate", "RunIdentification"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "PaymentStatus": {"type": "string"}
                }
            },
            semantic_tags=["treasury", "wire", "payment", "f110", "disburse", "privileged"],
            estimated_tokens=460
        )
    ]


def generate_scaled_catalog(target_size: int = 100) -> List[ToolDefinition]:
    """Generates an enterprise-scale catalog of N realistic ERP tools for scalability benchmarks."""
    base_tools = get_enterprise_tools()
    catalog: List[ToolDefinition] = list(base_tools)

    modules = ["FI", "CO", "MM", "SD", "PP", "QM", "PM", "HR", "TR", "LE"]
    roles = [
        "Z_FI_AUDITOR_READ", "Z_MM_PURCHASING_CLERK", "Z_MD_MASTER_DATA_MAINTAINER",
        "Z_SD_SALES_REP", "Z_CO_COST_ACCOUNTANT", "Z_PM_PLANT_ENGINEER",
        "Z_TREASURY_CHIEF_OFFICER", "Z_BASIS_ADMIN"
    ]

    idx = len(base_tools)
    while len(catalog) < target_size:
        mod = modules[idx % len(modules)]
        role = roles[idx % len(roles)]
        tool_id = f"sap_{mod.lower()}_entity_action_{idx:04d}"
        catalog.append(
            ToolDefinition(
                tool_id=tool_id,
                name=tool_id,
                description=f"Enterprise {mod} module tool {idx} for processing transaction documents and master records.",
                service_name=f"API_{mod}_TRANSACTION_{idx:04d}_SRV",
                entity_name=f"A_{mod}_Entity_{idx:04d}",
                operation=OperationType.QUERY if idx % 2 == 0 else OperationType.READ,
                required_role=role,
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "EntityId": {"type": "string"},
                        "CompanyCode": {"type": "string"}
                    },
                    "required": ["EntityId"]
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "Status": {"type": "string"},
                        "Value": {"type": "number"}
                    }
                },
                semantic_tags=[mod.lower(), "enterprise", "sap", f"tag_{idx % 15}"],
                estimated_tokens=300 + (idx % 100)
            )
        )
        idx += 1

    return catalog


def get_mock_principals() -> Tuple[UserPrincipal, UserPrincipal, UserPrincipal]:
    """Returns 3 canonical enterprise user principals with varying privilege levels."""
    # 1. Auditor Principal (Read-only Finance & AP)
    auditor = UserPrincipal(
        principal_id="aniketagrawal@google.com",
        tenant_id="deloitte-us-advisory",
        sap_user_id="SAP_AUDIT_ANIKET",
        pfcg_roles=["Z_FI_AUDITOR_READ", "Z_MM_PURCHASING_CLERK"],
        active_authorizations=[
            PFCGAuthorization(
                role_name="Z_FI_AUDITOR_READ",
                authorized_services={"API_GLACCOUNTBALANCE_SRV", "API_JOURNALENTRYITEMBASIC_SRV"},
                authorized_entities={"A_GLAccountBalance", "A_JournalEntryItemBasic"},
                allowed_operations={OperationType.QUERY, OperationType.READ},
                restricted_fields_mask={"EmployeeSalaryMask"}
            ),
            PFCGAuthorization(
                role_name="Z_MM_PURCHASING_CLERK",
                authorized_services={"API_PURCHASEORDER_PROCESS_SRV", "API_SUPPLIERINVOICE_PROCESS_SRV"},
                authorized_entities={"A_PurchaseOrder", "A_SupplierInvoice"},
                allowed_operations={OperationType.QUERY, OperationType.READ}
            )
        ],
        auth_token_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

    # 2. Purchasing Clerk (Procurement only)
    clerk = UserPrincipal(
        principal_id="buyer_clerk@deloitte.com",
        tenant_id="deloitte-us-advisory",
        sap_user_id="SAP_PROCURE_CLERK",
        pfcg_roles=["Z_MM_PURCHASING_CLERK"],
        active_authorizations=[
            PFCGAuthorization(
                role_name="Z_MM_PURCHASING_CLERK",
                authorized_services={"API_PURCHASEORDER_PROCESS_SRV", "API_SUPPLIERINVOICE_PROCESS_SRV"},
                authorized_entities={"A_PurchaseOrder", "A_SupplierInvoice"},
                allowed_operations={OperationType.QUERY, OperationType.READ}
            )
        ],
        auth_token_hash="a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0"
    )

    # 3. Super Admin
    admin = UserPrincipal(
        principal_id="secops_admin@google.com",
        tenant_id="google-cloud-enterprise",
        sap_user_id="SAP_BASIS_SUPER",
        pfcg_roles=["*"],
        active_authorizations=[
            PFCGAuthorization(
                role_name="Z_BASIS_ADMIN",
                authorized_services={"*"},
                authorized_entities={"*"},
                allowed_operations={
                    OperationType.READ, OperationType.QUERY, OperationType.CREATE,
                    OperationType.UPDATE, OperationType.DELETE, OperationType.EXECUTE
                }
            )
        ],
        auth_token_hash="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )

    return auditor, clerk, admin
