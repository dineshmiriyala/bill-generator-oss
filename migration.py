# migration.py

import sqlite3
import os

def migrate_db(db_path):
    if not os.path.exists(db_path):
        print(f"[Migration] Database '{db_path}' not found. Skipping.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='last_backup';")
        table_exists = cursor.fetchone()

        if not table_exists:
            print("[Migration] Creating missing table: last_backup")
            cursor.execute("""
                CREATE TABLE last_backup (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    note TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='layout_config';")
        layout_table_exists = cursor.fetchone()

        if not layout_table_exists:
            print("[Migration] Creating missing table: layout_config")
            cursor.execute("""
                CREATE TABLE layout_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sizes_json TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounting_transaction';")
        accounting_table_exists = cursor.fetchone()

        if not accounting_table_exists:
            print("[Migration] Creating missing table: accounting_transaction")
            cursor.execute("""
                CREATE TABLE accounting_transaction (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    txn_id TEXT NOT NULL UNIQUE,
                    customerId INTEGER,
                    amount REAL NOT NULL,
                    txn_type TEXT NOT NULL DEFAULT 'income',
                    mode TEXT,
                    account TEXT,
                    invoice_no TEXT,
                    remarks TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(customerId) REFERENCES customer(id)
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_accounting_transaction_customerId ON accounting_transaction(customerId);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_accounting_transaction_invoice_no ON accounting_transaction(invoice_no);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_accounting_transaction_txn_type ON accounting_transaction(txn_type);")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='expense_item';")
        expense_table_exists = cursor.fetchone()

        if not expense_table_exists:
            print("[Migration] Creating missing table: expense_item")
            cursor.execute("""
                CREATE TABLE expense_item (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transactionId INTEGER NOT NULL,
                    description TEXT,
                    amount REAL,
                    FOREIGN KEY(transactionId) REFERENCES accounting_transaction(id) ON DELETE CASCADE
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_expense_item_transactionId ON expense_item(transactionId);")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bill_draft';")
        bill_draft_exists = cursor.fetchone()

        if not bill_draft_exists:
            print("[Migration] Creating missing table: bill_draft")
            cursor.execute("""
                CREATE TABLE bill_draft (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customerId INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    payloadJson TEXT NOT NULL DEFAULT '{}',
                    totalAmount REAL NOT NULL DEFAULT 0,
                    itemCount INTEGER NOT NULL DEFAULT 0,
                    convertedInvoiceId INTEGER,
                    createdAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updatedAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(customerId) REFERENCES customer(id),
                    FOREIGN KEY(convertedInvoiceId) REFERENCES invoice(id)
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_bill_draft_customerId ON bill_draft(customerId);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_bill_draft_status ON bill_draft(status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_bill_draft_convertedInvoiceId ON bill_draft(convertedInvoiceId);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_bill_draft_createdAt ON bill_draft(createdAt);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_bill_draft_updatedAt ON bill_draft(updatedAt);")

        cursor.execute("PRAGMA table_info(invoice);")
        invoice_columns = [row[1] for row in cursor.fetchall()]
        if 'payment' not in invoice_columns:
            print("[Migration] Adding missing column: invoice.payment")
            cursor.execute("ALTER TABLE invoice ADD COLUMN payment INTEGER NOT NULL DEFAULT 0;")

        # Ensure exclude flags exist on invoice
        if 'exclude_phone' not in invoice_columns:
            print("[Migration] Adding missing column: invoice.exclude_phone")
            cursor.execute("ALTER TABLE invoice ADD COLUMN exclude_phone INTEGER NOT NULL DEFAULT 0;")
        if 'exclude_gst' not in invoice_columns:
            print("[Migration] Adding missing column: invoice.exclude_gst")
            cursor.execute("ALTER TABLE invoice ADD COLUMN exclude_gst INTEGER NOT NULL DEFAULT 0;")
        if 'exclude_addr' not in invoice_columns:
            print("[Migration] Adding missing column: invoice.exclude_addr")
            cursor.execute("ALTER TABLE invoice ADD COLUMN exclude_addr INTEGER NOT NULL DEFAULT 0;")

        # Ensure soft-delete columns exist on customer
        cursor.execute("PRAGMA table_info(customer);")
        customer_columns = [row[1] for row in cursor.fetchall()]
        if 'isDeleted' not in customer_columns:
            print("[Migration] Adding missing column: customer.isDeleted")
            cursor.execute("ALTER TABLE customer ADD COLUMN isDeleted INTEGER NOT NULL DEFAULT 0;")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_customer_isDeleted ON customer(isDeleted);")
        if 'deletedAt' not in customer_columns:
            print("[Migration] Adding missing column: customer.deletedAt")
            cursor.execute("ALTER TABLE customer ADD COLUMN deletedAt DATETIME;")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_customer_deletedAt ON customer(deletedAt);")

        # Ensure delivery challan number exists on invoice_item (added later)
        cursor.execute("PRAGMA table_info(invoice_item);")
        invoice_item_columns = [row[1] for row in cursor.fetchall()]
        if 'dcNo' not in invoice_item_columns:
            print("[Migration] Adding missing column: invoice_item.dcNo")
            cursor.execute("ALTER TABLE invoice_item ADD COLUMN dcNo TEXT;")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_invoice_item_dcNo ON invoice_item(dcNo);")
        if 'rounded' not in invoice_item_columns:
            print("[Migration] Adding missing column: invoice_item.rounded")
            cursor.execute("ALTER TABLE invoice_item ADD COLUMN rounded INTEGER NOT NULL DEFAULT 0;")

        conn.commit()
        print("[Migration] DB schema is up-to-date.")
    except Exception as e:
        print(f"[Migration ERROR] {e}")
    finally:
        conn.close()
