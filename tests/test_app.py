import json
from datetime import datetime, timezone
from pathlib import Path
import re


def _read_info_json(module):
    info_path = module.get_info_json_path()
    with open(info_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_repo_file(module, relative_path):
    return Path(module.app.root_path, relative_path).read_text(encoding="utf-8")


def _seed_invoice(
    module,
    cust,
    invoice_no,
    total_amount,
    created_at,
    *,
    is_deleted=False,
    is_paid=False,
    item_names=None,
    dc_numbers=None,
    rounded_flags=None,
):
    item_names = item_names or ["Seed Item"]
    dc_numbers = dc_numbers or [''] * len(item_names)
    rounded_flags = rounded_flags or [False] * len(item_names)

    inv = module.invoice(
        invoiceId=invoice_no,
        customerId=cust.id,
        createdAt=created_at,
        pdfPath=f"static/pdfs/{invoice_no}.pdf",
        totalAmount=total_amount,
        isDeleted=is_deleted,
        payment=is_paid,
    )
    module.db.session.add(inv)
    module.db.session.flush()

    line_total = round(float(total_amount) / max(len(item_names), 1), 2)
    for index, item_name in enumerate(item_names, start=1):
        inventory_item = module.item.query.filter_by(name=item_name).first()
        if not inventory_item:
            inventory_item = module.item(
                name=item_name,
                unitPrice=line_total,
                quantity=10,
                taxPercentage=0,
            )
            module.db.session.add(inventory_item)
            module.db.session.flush()

        module.db.session.add(module.invoiceItem(
            invoiceId=inv.id,
            itemId=inventory_item.id,
            quantity=index,
            rate=line_total,
            discount=0,
            taxPercentage=0,
            line_total=line_total,
            dcNo=dc_numbers[index - 1] if len(dc_numbers) >= index else None,
            rounded=bool(rounded_flags[index - 1]) if len(rounded_flags) >= index else False,
        ))

    return inv


def _seed_income_payment(module, cust, amount, *, invoice_no=None, created_at=None, is_deleted=False):
    txn = module.accountingTransaction(
        customerId=cust.id,
        amount=amount,
        txn_type='income',
        mode='cash',
        account='cash',
        invoice_no=invoice_no,
        remarks='Test payment',
        is_deleted=is_deleted,
    )
    if created_at is not None:
        txn.created_at = created_at
        txn.updated_at = created_at
    module.db.session.add(txn)
    return txn


def test_info_json_uses_earliest_invoice(app_module):
    module = app_module
    early_dt = datetime(2021, 5, 17, 9, 30, tzinfo=timezone.utc)

    with module.app.app_context():
        # Seed a customer and an early invoice before regenerating info.json
        cust = module.customer(name="Test Customer", phone="9990011111")
        module.db.session.add(cust)
        module.db.session.commit()

        invoice = module.invoice(
            invoiceId="INV-0001",
            customerId=cust.id,
            createdAt=early_dt,
            pdfPath="static/pdfs/inv-0001.pdf",
            totalAmount=150.0,
        )
        module.db.session.add(invoice)
        module.db.session.commit()

        info_path = module.get_info_json_path()
        if info_path.exists():
            info_path.unlink()

        module.ensure_info_json()
        payload = _read_info_json(module)

    expected_iso = early_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    expected_human = early_dt.strftime("%d %B %Y")

    data_section = payload["data"]
    assert data_section["account_defaults"]["start_date"] == expected_iso
    assert data_section["meta"]["created_on"] == expected_iso
    assert payload["created_on"] == expected_human


def test_create_bill_endpoint_creates_invoice(app_module):
    module = app_module
    with module.app.app_context():
        customer = module.customer(name="Invoice User", phone="5551230000")
        module.db.session.add(customer)
        module.db.session.commit()

        client = module.app.test_client()

        # Step 1: load the create bill form for this customer to obtain the form token
        form_resp = client.post(
            "/select_customer",
            data={"customer": customer.phone},
            follow_redirects=False,
        )
        html = form_resp.get_data(as_text=True)
        match = re.search(r'name="form_token" value="([^"]+)"', html)
        assert match, "form token not rendered"
        token = match.group(1)

        form_payload = {
            "customer_phone": customer.phone,
            "description[]": ["Service A"],
            "quantity[]": ["2"],
            "rate[]": ["450.00"],
            "total[]": ["900.00"],
            "rounded[]": ["0"],
            "dc_no[]": [""],
            "form_token": token,
        }

        response = client.post("/create-bill", data=form_payload, follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["Location"].startswith("/view-bill/")

        created_invoice = module.invoice.query.filter_by(customerId=customer.id).one()
        assert created_invoice.totalAmount == 900.0
        # Invoice prefix is configurable; just check the format is sane.
        assert re.match(r"^[A-Z]+-\d{6}-\d+$", created_invoice.invoiceId or "")

        items = module.invoiceItem.query.filter_by(invoiceId=created_invoice.id).all()
        assert len(items) == 1
        assert items[0].quantity == 2
        assert items[0].rate == 450.0


def test_create_customers_page_uses_refreshed_layout(app_module):
    module = app_module
    client = module.app.test_client()

    response = client.get("/create_customers?bill_generation=true&next_url=/create-bill", follow_redirects=False)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Customer Setup" in html
    assert "Add New Customer" in html
    assert "Back to customer picker" in html
    assert "What This Record Uses" in html
    assert "Use computer-generated ID" in html
    assert "Keep It Clean" not in html
    assert 'name="next_url" value="/create-bill"' in html
    assert 'name="bill_generation" value="1"' in html


def test_view_customers_page_uses_refreshed_layout(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Directory User", company="Directory Co", phone="5557770011")
        module.db.session.add(cust)
        module.db.session.commit()

    client = module.app.test_client()
    response = client.get("/view_customers", follow_redirects=False)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Customer Directory" in html
    assert "View Customers" in html
    assert "Search by phone, name, or company" in html
    assert "Simple Statement" in html
    assert "Accounting Page" in html
    assert "Create Bill" in html
    assert "Customer List" not in html


def test_create_customer_bill_flow_redirects_into_create_bill(app_module):
    module = app_module
    client = module.app.test_client()

    response = client.post(
        "/create_customers",
        data={
            "company": "Flow Co",
            "name": "Flow User",
            "phone": "5557770098",
            "email": "",
            "gst": "",
            "address": "",
            "businessType": "",
            "bill_generation": "1",
            "next_url": "/create-bill",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/create-bill?customer_id=")

    with module.app.app_context():
        created = module.customer.query.filter_by(phone="5557770098", isDeleted=False).first()
        assert created is not None
        assert response.headers["Location"].endswith(f"customer_id={created.id}")


def test_about_user_page_uses_refreshed_layout(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Profile User", company="Profile Co", phone="5557770012")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/about_user?customer_id={cust.id}", follow_redirects=False)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Customer Profile" in html
    assert "Saved Details" in html
    assert "Open customer accounting" in html
    assert "About User" not in html


def test_edit_user_page_uses_refreshed_layout(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Edit User", company="Edit Co", phone="5557770013")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/edit_user/{cust.id}", follow_redirects=False)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Edit Customer" in html
    assert "Current Record" in html
    assert "Save Changes" in html
    assert "Update Customer" not in html


def test_base_template_uses_renderer_safe_alert_autodismiss_helper(app_module):
    module = app_module
    template_text = _read_repo_file(module, "templates/base.html")

    assert "function closeAlertElement" in template_text
    assert "data-auto-dismiss-ms" in template_text
    assert "window.addEventListener('pageshow', bindAlerts);" in template_text
    assert "bootstrap.Alert.getOrCreateInstance" not in template_text


def test_page_level_alert_templates_use_shared_dismissible_contract(app_module):
    module = app_module

    create_bill_template = _read_repo_file(module, "templates/create_bill.html")
    view_bill_template = _read_repo_file(module, "templates/view_bill_locked.html")
    add_customer_template = _read_repo_file(module, "templates/add_customer.html")
    edit_user_template = _read_repo_file(module, "templates/edit_user.html")
    add_inventory_template = _read_repo_file(module, "templates/add_inventory.html")
    accounting_template = _read_repo_file(module, "templates/accounting.html")

    assert 'alert alert-success alert-dismissible fade show' in create_bill_template
    assert 'data-auto-dismiss-ms="4000"' in create_bill_template
    assert 'data-auto-dismiss-ms="4000"' in view_bill_template
    assert 'alert alert-danger alert-dismissible fade show' in add_customer_template
    assert 'alert alert-success alert-dismissible fade show' in add_customer_template
    assert 'alert alert-danger alert-dismissible fade show' in edit_user_template
    assert 'alert alert-danger alert-dismissible fade show' in add_inventory_template
    assert 'alert alert-success alert-dismissible fade show' in add_inventory_template
    assert 'alert alert-warning alert-dismissible fade show' in accounting_template


def test_desktop_launcher_prefers_modern_windows_renderer_with_fallback(app_module):
    module = app_module
    launcher_text = _read_repo_file(module, "desktop_launcher.py")

    assert 'start_kwargs["gui"] = "edgechromium"' in launcher_text
    assert "Preferred webview renderer unavailable" in launcher_text
    assert "start_desktop_webview(webview)" in launcher_text


def test_missing_customer_routes_redirect_safely(app_module):
    module = app_module
    client = module.app.test_client()

    start_bill_response = client.get("/create-bill?customer_id=999999", follow_redirects=False)
    about_response = client.get("/about_user?customer_id=999999", follow_redirects=False)
    edit_response = client.get("/edit_user/999999", follow_redirects=False)

    assert start_bill_response.status_code == 302
    assert start_bill_response.headers["Location"].endswith("/select_customer")
    assert about_response.status_code == 302
    assert about_response.headers["Location"].endswith("/view_customers")
    assert edit_response.status_code == 302
    assert edit_response.headers["Location"].endswith("/view_customers")


def test_select_customer_page_handles_customers_without_company_names(app_module):
    module = app_module
    with module.app.app_context():
        alpha = module.customer(name="Alpha User", company=None, phone="5557770014")
        beta = module.customer(name="Beta User", company="Beta Co", phone="5557770015")
        module.db.session.add_all([beta, alpha])
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/select_customer", follow_redirects=False)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Alpha User" in html
    assert "Beta Co" in html
    assert html.index("Alpha User") < html.index("Beta Co")


def test_save_bill_draft_creates_draft_without_invoice(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Draft User", company="Draft Co", phone="5557770000")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/bill-drafts/save",
            data={
                "customer_phone": cust.phone,
                "exclude_phone": "on",
                "exclude_addr": "on",
                "dc_enabled": "1",
                "description[]": ["Draft Poster"],
                "quantity[]": ["2"],
                "rate[]": ["125.50"],
                "total[]": ["251.00"],
                "dc_no[]": ["DC-101"],
                "rounded[]": ["1"],
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].startswith("/bill-drafts/")

        saved_draft = module.billDraft.query.filter_by(customerId=cust.id, status="draft").one()
        assert module.invoice.query.count() == 0
        assert saved_draft.itemCount == 1
        assert saved_draft.totalAmount == 250.0

        payload = json.loads(saved_draft.payloadJson)
        assert payload["exclude_phone"] is True
        assert payload["exclude_addr"] is True
        assert payload["dc_enabled"] is True
        assert payload["items"][0]["description"] == "Draft Poster"
        assert payload["items"][0]["dc_no"] == "DC-101"
        assert payload["items"][0]["rounded"] is True


def test_open_bill_draft_restores_saved_form_state(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Restore User", company="Restore Co", phone="5557770001")
        module.db.session.add(cust)
        module.db.session.commit()

        payload = {
            "exclude_phone": True,
            "exclude_gst": False,
            "exclude_addr": True,
            "dc_enabled": True,
            "items": [
                {
                    "description": "Restore Item",
                    "quantity": "3",
                    "rate": "99.50",
                    "dc_no": "DC-RESTORE",
                    "rounded": True,
                }
            ],
        }
        draft = module.billDraft(
            customerId=cust.id,
            status="draft",
            payloadJson=json.dumps(payload),
            totalAmount=300.0,
            itemCount=1,
        )
        module.db.session.add(draft)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/bill-drafts/{draft.id}", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Draft mode" in html
        assert 'name="draft_id" value="' in html
        assert 'value="Restore Item"' in html
        assert 'value="3"' in html
        assert 'value="99.50"' in html
        assert 'value="DC-RESTORE"' in html
        assert 'name="dc_enabled" id="dcEnabledInput" value="1"' in html
        assert 'name="rounded[]" value="1"' in html
        assert "Update Draft" in html
        assert "Delete Draft" in html


def test_update_draft_and_generate_bill_converts_draft(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Convert User", company="Convert Co", phone="5557770002")
        module.db.session.add(cust)
        module.db.session.commit()

        draft = module.billDraft(
            customerId=cust.id,
            status="draft",
            payloadJson=json.dumps(
                {
                    "exclude_phone": False,
                    "exclude_gst": False,
                    "exclude_addr": False,
                    "dc_enabled": False,
                    "items": [
                        {
                            "description": "Old Draft Item",
                            "quantity": "1",
                            "rate": "10.00",
                            "dc_no": "",
                            "rounded": False,
                        }
                    ],
                }
            ),
            totalAmount=10.0,
            itemCount=1,
        )
        module.db.session.add(draft)
        module.db.session.commit()

        client = module.app.test_client()
        update_response = client.post(
            "/bill-drafts/save",
            data={
                "draft_id": str(draft.id),
                "customer_phone": cust.phone,
                "exclude_gst": "on",
                "dc_enabled": "1",
                "description[]": ["Updated Draft Item"],
                "quantity[]": ["4"],
                "rate[]": ["75.00"],
                "total[]": ["300.00"],
                "dc_no[]": ["DC-UPDATE"],
                "rounded[]": ["0"],
            },
            follow_redirects=False,
        )
        assert update_response.status_code == 302

        module.db.session.refresh(draft)
        assert draft.itemCount == 1
        assert draft.totalAmount == 300.0
        updated_payload = json.loads(draft.payloadJson)
        assert updated_payload["exclude_gst"] is True
        assert updated_payload["dc_enabled"] is True
        assert updated_payload["items"][0]["description"] == "Updated Draft Item"

        open_response = client.get(f"/bill-drafts/{draft.id}", follow_redirects=False)
        open_html = open_response.get_data(as_text=True)
        token_match = re.search(r'name="form_token" value="([^"]+)"', open_html)
        assert token_match
        token = token_match.group(1)

        generate_response = client.post(
            "/create-bill",
            data={
                "draft_id": str(draft.id),
                "customer_phone": cust.phone,
                "description[]": ["Updated Draft Item"],
                "quantity[]": ["4"],
                "rate[]": ["75.00"],
                "total[]": ["300.00"],
                "dc_no[]": ["DC-UPDATE"],
                "rounded[]": ["0"],
                "exclude_gst": "on",
                "form_token": token,
            },
            follow_redirects=False,
        )

        assert generate_response.status_code == 302
        assert generate_response.headers["Location"].startswith("/view-bill/")

        module.db.session.refresh(draft)
        created_invoice = module.invoice.query.filter_by(customerId=cust.id).one()
        assert draft.status == "converted"
        assert draft.convertedInvoiceId == created_invoice.id

        draft_list_html = client.get("/bill-drafts", follow_redirects=False).get_data(as_text=True)
        assert f"/bill-drafts/{draft.id}" not in draft_list_html


def test_duplicate_bill_as_draft_copies_items_and_hides_non_active_drafts(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Duplicate User", company="Duplicate Co", phone="5557770003")
        module.db.session.add(cust)
        module.db.session.commit()

        source_invoice = _seed_invoice(
            module,
            cust,
            "INV-DRAFT-DUPE",
            200.0,
            datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc),
            item_names=["Draft Copy Item"],
            dc_numbers=["DC-DUPE"],
            rounded_flags=[True],
        )
        archived_draft = module.billDraft(
            customerId=cust.id,
            status="archived",
            payloadJson=json.dumps({"items": []}),
            totalAmount=0.0,
            itemCount=0,
        )
        module.db.session.add(archived_draft)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(f"/bills/{source_invoice.invoiceId}/duplicate-draft", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["Location"].startswith("/bill-drafts/")

        active_drafts = module.billDraft.query.filter_by(customerId=cust.id, status="draft").all()
        assert len(active_drafts) == 1
        duplicated_draft = active_drafts[0]
        payload = json.loads(duplicated_draft.payloadJson)
        assert payload["dc_enabled"] is True
        assert payload["items"][0]["description"] == "Draft Copy Item"
        assert payload["items"][0]["dc_no"] == "DC-DUPE"
        assert payload["items"][0]["rounded"] is True

        list_html = client.get("/bill-drafts", follow_redirects=False).get_data(as_text=True)
        assert "Draft Copy Item" not in list_html
        assert f"/bill-drafts/{duplicated_draft.id}" in list_html
        assert f"/bill-drafts/{archived_draft.id}" not in list_html


def test_archive_bill_draft_removes_it_from_active_list(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Archive Draft User", company="Archive Co", phone="5557770005")
        module.db.session.add(cust)
        module.db.session.commit()

        draft = module.billDraft(
            customerId=cust.id,
            status="draft",
            payloadJson=json.dumps({"items": [{"description": "Archive Item"}]}),
            totalAmount=50.0,
            itemCount=1,
        )
        module.db.session.add(draft)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            f"/bill-drafts/{draft.id}/archive",
            data={"next": f"/bill-drafts?customer_id={cust.id}"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"] == f"/bill-drafts?customer_id={cust.id}"

        module.db.session.refresh(draft)
        assert draft.status == "archived"
        list_html = client.get("/bill-drafts", follow_redirects=False).get_data(as_text=True)
        assert f"/bill-drafts/{draft.id}" not in list_html


def test_bill_drafts_page_shows_bulk_delete_actions(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Bulk UI User", company="Bulk UI Co", phone="5557770006")
        module.db.session.add(cust)
        module.db.session.commit()

        draft = module.billDraft(
            customerId=cust.id,
            status="draft",
            payloadJson=json.dumps({"items": [{"description": "Bulk UI Item"}]}),
            totalAmount=60.0,
            itemCount=1,
        )
        module.db.session.add(draft)
        module.db.session.commit()

        client = module.app.test_client()
        all_html = client.get("/bill-drafts", follow_redirects=False).get_data(as_text=True)
        customer_html = client.get(f"/bill-drafts?customer_id={cust.id}", follow_redirects=False).get_data(as_text=True)

        assert "Delete All Drafts" in all_html
        assert "Delete Customer Drafts" not in all_html
        assert "Delete All Drafts" in customer_html
        assert "Delete Customer Drafts" in customer_html


def test_bulk_archive_all_bill_drafts_archives_every_active_draft(app_module):
    module = app_module
    with module.app.app_context():
        cust_one = module.customer(name="Bulk All User 1", company="Bulk All Co 1", phone="5557770007")
        cust_two = module.customer(name="Bulk All User 2", company="Bulk All Co 2", phone="5557770008")
        module.db.session.add_all([cust_one, cust_two])
        module.db.session.commit()

        active_one = module.billDraft(customerId=cust_one.id, status="draft", payloadJson=json.dumps({"items": []}), totalAmount=10.0, itemCount=0)
        active_two = module.billDraft(customerId=cust_two.id, status="draft", payloadJson=json.dumps({"items": []}), totalAmount=20.0, itemCount=0)
        converted = module.billDraft(customerId=cust_one.id, status="converted", payloadJson=json.dumps({"items": []}), totalAmount=30.0, itemCount=0)
        module.db.session.add_all([active_one, active_two, converted])
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/bill-drafts/archive-bulk",
            data={"scope": "all", "next": "/bill-drafts"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"] == "/bill-drafts"

        module.db.session.refresh(active_one)
        module.db.session.refresh(active_two)
        module.db.session.refresh(converted)
        assert active_one.status == "archived"
        assert active_two.status == "archived"
        assert converted.status == "converted"


def test_bulk_archive_customer_bill_drafts_only_affects_that_customer(app_module):
    module = app_module
    with module.app.app_context():
        cust_one = module.customer(name="Bulk Customer User 1", company="Bulk Customer Co 1", phone="5557770009")
        cust_two = module.customer(name="Bulk Customer User 2", company="Bulk Customer Co 2", phone="5557770010")
        module.db.session.add_all([cust_one, cust_two])
        module.db.session.commit()

        customer_draft = module.billDraft(customerId=cust_one.id, status="draft", payloadJson=json.dumps({"items": []}), totalAmount=15.0, itemCount=0)
        other_draft = module.billDraft(customerId=cust_two.id, status="draft", payloadJson=json.dumps({"items": []}), totalAmount=25.0, itemCount=0)
        module.db.session.add_all([customer_draft, other_draft])
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/bill-drafts/archive-bulk",
            data={
                "scope": "customer",
                "customer_id": str(cust_one.id),
                "next": f"/bill-drafts?customer_id={cust_one.id}",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"] == f"/bill-drafts?customer_id={cust_one.id}"

        module.db.session.refresh(customer_draft)
        module.db.session.refresh(other_draft)
        assert customer_draft.status == "archived"
        assert other_draft.status == "draft"


def test_home_and_select_customer_surface_draft_entry_points(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Entry User", company="Entry Co", phone="5557770004")
        module.db.session.add(cust)
        module.db.session.commit()

        draft = module.billDraft(
            customerId=cust.id,
            status="draft",
            payloadJson=json.dumps({"items": []}),
            totalAmount=0.0,
            itemCount=0,
        )
        module.db.session.add(draft)
        module.db.session.commit()

        client = module.app.test_client()
        home_html = client.get("/", follow_redirects=False).get_data(as_text=True)
        select_html = client.get("/select_customer", follow_redirects=False).get_data(as_text=True)

        assert "Draft Bills" in home_html
        assert 'href="/bill-drafts"' in home_html
        assert "Open Drafts" in select_html
        assert "Drafts 1" in select_html
        assert f'href="/bill-drafts?customer_id={cust.id}"' in select_html


def test_create_bill_page_shows_previous_bills_for_selected_customer(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="History User", company="History Co", phone="5550001111")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-HIST-001",
            125.0,
            datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
            item_names=["Poster"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-HIST-002",
            300.0,
            datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
            is_paid=True,
            item_names=["Banner", "Sticker"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post("/select_customer", data={"customer": cust.phone}, follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Previous Bills" in html
        assert 'data-history-invoice="INV-HIST-001"' in html
        assert 'data-history-invoice="INV-HIST-002"' in html
        assert "INR 125.00" in html
        assert "2 items" in html


def test_create_bill_history_hides_soft_deleted_invoices(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Soft Delete User", company="Filter Co", phone="5550002222")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-LIVE-001",
            200.0,
            datetime(2026, 3, 24, 8, 0, tzinfo=timezone.utc),
            item_names=["Cards"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-DELETED-001",
            400.0,
            datetime(2026, 3, 24, 9, 0, tzinfo=timezone.utc),
            is_deleted=True,
            item_names=["Flex"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/create-bill?customer_id={cust.id}", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'data-history-invoice="INV-LIVE-001"' in html
        assert 'data-history-invoice="INV-DELETED-001"' not in html


def test_edit_bill_history_excludes_current_invoice(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Edit User", company="Edit Co", phone="5550003333")
        module.db.session.add(cust)
        module.db.session.commit()

        current_invoice = _seed_invoice(
            module,
            cust,
            "INV-CURRENT-001",
            500.0,
            datetime(2026, 3, 27, 7, 0, tzinfo=timezone.utc),
            item_names=["Magazine"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-OLDER-001",
            275.0,
            datetime(2026, 3, 21, 7, 0, tzinfo=timezone.utc),
            item_names=["Flyer"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/edit-bill/{current_invoice.invoiceId}", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'data-history-invoice="INV-OLDER-001"' in html
        assert 'data-history-invoice="INV-CURRENT-001"' not in html


def test_view_bill_locked_shows_customer_navigation_and_highlights_current(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="View Bill User", company="View Bill Co", phone="5550003434")
        other_cust = module.customer(name="Other View Bill User", company="Other View Bill Co", phone="5550003535")
        module.db.session.add_all([cust, other_cust])
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-VIEW-CURRENT",
            125.0,
            datetime(2026, 3, 28, 16, 0, tzinfo=timezone.utc),
            item_names=["Current View Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-VIEW-OLDER",
            90.0,
            datetime(2026, 3, 25, 16, 0, tzinfo=timezone.utc),
            item_names=["Older View Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-VIEW-DELETED",
            55.0,
            datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc),
            is_deleted=True,
            item_names=["Deleted View Item"],
        )
        _seed_invoice(
            module,
            other_cust,
            "INV-VIEW-OTHER",
            60.0,
            datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc),
            item_names=["Other View Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/view-bill/INV-VIEW-CURRENT", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Customer Bills" in html
        assert 'data-view-bill-nav="INV-VIEW-CURRENT"' in html
        assert 'data-view-bill-nav="INV-VIEW-OLDER"' in html
        assert 'data-view-bill-nav="INV-VIEW-DELETED"' not in html
        assert 'data-view-bill-nav="INV-VIEW-OTHER"' not in html
        assert 'data-view-bill-nav="INV-VIEW-CURRENT"' in html and 'data-current="true"' in html
        assert 'href="/view-bill/INV-VIEW-OLDER"' in html
        assert 'href="/view-bill/INV-VIEW-CURRENT"' not in html
        assert 'class="d-flex flex-column gap-3 view-bill-nav-scroll"' in html
        assert 'class="view-bill-new-bill-panel pt-3 mt-3"' in html
        assert "Create New Bill" in html
        assert "Same Customer" in html
        assert "Other Customer" in html
        assert 'class="d-flex justify-content-end gap-2 flex-wrap mt-4 view-bill-bottom-actions"' in html
        assert 'href="/bill_preview_dues/INV-VIEW-CURRENT"' in html
        assert 'href="/bill_preview/INV-VIEW-CURRENT"' in html


def test_view_bill_locked_shows_single_current_bill_in_navigation(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Single View Bill User", company="Single View Bill Co", phone="5550003636")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-VIEW-SINGLE",
            77.0,
            datetime(2026, 3, 28, 17, 0, tzinfo=timezone.utc),
            item_names=["Single View Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/view-bill/INV-VIEW-SINGLE", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert html.count('data-view-bill-nav="INV-VIEW-SINGLE"') == 1
        assert 'data-current="true"' in html
        assert "Current Bill" in html


def test_bill_with_dues_selector_shows_only_same_customer_outstanding_bills(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Due User", company="Due Co", phone="5550004444")
        other_cust = module.customer(name="Other User", company="Other Co", phone="5550005555")
        module.db.session.add_all([cust, other_cust])
        module.db.session.commit()

        current_invoice = _seed_invoice(
            module,
            cust,
            "INV-DUE-CURRENT",
            120.0,
            datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc),
            item_names=["Current Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-DUE-OPEN",
            200.0,
            datetime(2026, 3, 20, 8, 0, tzinfo=timezone.utc),
            item_names=["Open Item"],
        )
        _seed_income_payment(
            module,
            cust,
            50.0,
            invoice_no="INV-DUE-OPEN",
            created_at=datetime(2026, 3, 21, 8, 0, tzinfo=timezone.utc),
        )
        _seed_invoice(
            module,
            cust,
            "INV-DUE-PAID",
            90.0,
            datetime(2026, 3, 19, 8, 0, tzinfo=timezone.utc),
            item_names=["Paid Item"],
        )
        _seed_income_payment(
            module,
            cust,
            90.0,
            invoice_no="INV-DUE-PAID",
            created_at=datetime(2026, 3, 20, 8, 0, tzinfo=timezone.utc),
        )
        _seed_invoice(
            module,
            cust,
            "INV-DUE-DELETED",
            75.0,
            datetime(2026, 3, 18, 8, 0, tzinfo=timezone.utc),
            is_deleted=True,
            item_names=["Deleted Item"],
        )
        _seed_invoice(
            module,
            other_cust,
            "INV-DUE-OTHER",
            140.0,
            datetime(2026, 3, 17, 8, 0, tzinfo=timezone.utc),
            item_names=["Other Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/bill_preview_dues/{current_invoice.invoiceId}", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'data-bill-invoice="INV-DUE-CURRENT"' in html
        assert 'data-bill-invoice="INV-DUE-OPEN"' in html
        assert 'data-bill-invoice="INV-DUE-PAID"' not in html
        assert 'data-bill-invoice="INV-DUE-DELETED"' not in html
        assert 'data-bill-invoice="INV-DUE-OTHER"' not in html
        assert 'data-kind="current"' in html
        assert 'checked' in html
        assert "Select all unpaid" in html
        assert "Unselect all" in html
        assert "Print Bill with Dues" in html


def test_bill_preview_moves_phone_toggle_to_action_buttons(app_module, monkeypatch):
    module = app_module

    class _FakeQrResponse:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: _FakeQrResponse())

    with module.app.app_context():
        cust = module.customer(name="Phone Toggle User", company="Phone Toggle Co", phone="5550001212")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-PHONE-TOGGLE",
            95.0,
            datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc),
            item_names=["Phone Toggle Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/bill_preview/INV-PHONE-TOGGLE", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'id="togglePhoneBtn"' in html
        assert "Hide Phone" in html
        assert 'id="customerPhoneLine"' in html
        assert 'id="toggleCustomerPhone"' not in html


def test_bill_preview_with_dues_ignores_invalid_selected_invoices_and_totals(app_module, monkeypatch):
    module = app_module
    original_due_heading = module.APP_INFO.setdefault("bill_config", {}).get("dues-table-heading")
    module.APP_INFO["bill_config"]["dues-table-heading"] = "Outstanding Bills"
    qr_calls = []

    class _FakeQrResponse:
        status_code = 500

        @staticmethod
        def json():
            return {}

    def _fake_qr_get(*args, **kwargs):
        qr_calls.append(kwargs.get("params") or {})
        return _FakeQrResponse()

    monkeypatch.setattr(module.requests, "get", _fake_qr_get)

    try:
        with module.app.app_context():
            cust = module.customer(name="Preview Due User", company="Preview Due Co", phone="5550006666")
            other_cust = module.customer(name="Preview Other", company="Preview Other Co", phone="5550007777")
            module.db.session.add_all([cust, other_cust])
            module.db.session.commit()

            current_invoice = _seed_invoice(
                module,
                cust,
                "INV-PREVIEW-CURRENT",
                120.0,
                datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc),
                item_names=["Current Preview Item"],
            )
            _seed_invoice(
                module,
                cust,
                "INV-PREVIEW-OPEN",
                80.0,
                datetime(2026, 3, 24, 10, 0, tzinfo=timezone.utc),
                item_names=["Open Preview Item"],
            )
            _seed_income_payment(
                module,
                cust,
                30.0,
                invoice_no="INV-PREVIEW-OPEN",
                created_at=datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
            )
            _seed_invoice(
                module,
                cust,
                "INV-PREVIEW-PAID",
                60.0,
                datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
                item_names=["Paid Preview Item"],
            )
            _seed_income_payment(
                module,
                cust,
                60.0,
                invoice_no="INV-PREVIEW-PAID",
                created_at=datetime(2026, 3, 24, 10, 0, tzinfo=timezone.utc),
            )
            _seed_invoice(
                module,
                other_cust,
                "INV-PREVIEW-OTHER",
                45.0,
                datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc),
                item_names=["Other Preview Item"],
            )
            module.db.session.commit()

            client = module.app.test_client()
            response = client.get(
                "/bill_preview/INV-PREVIEW-CURRENT"
                "?with_dues=1"
                "&include_current=1"
                "&selected_due=INV-PREVIEW-OPEN"
                "&selected_due=INV-PREVIEW-PAID"
                "&selected_due=INV-PREVIEW-OTHER"
                "&selected_due=INV-PREVIEW-CURRENT",
                follow_redirects=False,
            )

            html = response.get_data(as_text=True)
            assert response.status_code == 200
            assert 'data-due-summary-invoice="INV-PREVIEW-CURRENT"' in html
            assert html.count('data-due-summary-invoice="INV-PREVIEW-CURRENT"') == 1
            assert 'data-due-summary-invoice="INV-PREVIEW-OPEN"' in html
            assert 'data-due-summary-invoice="INV-PREVIEW-PAID"' not in html
            assert 'data-due-summary-invoice="INV-PREVIEW-OTHER"' not in html
            assert 'data-due-summary-total="170.00"' in html
            assert "Outstanding Bills" in html
            assert qr_calls
            assert qr_calls[-1].get("am") == "170.00"
    finally:
        if original_due_heading is None:
            module.APP_INFO["bill_config"].pop("dues-table-heading", None)
        else:
            module.APP_INFO["bill_config"]["dues-table-heading"] = original_due_heading


def test_bill_preview_with_dues_excludes_current_when_include_current_is_off(app_module, monkeypatch):
    module = app_module

    class _FakeQrResponse:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: _FakeQrResponse())

    with module.app.app_context():
        cust = module.customer(name="Preview Toggle User", company="Preview Toggle Co", phone="5550008888")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-PREVIEW-TOGGLE-CURRENT",
            100.0,
            datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc),
            item_names=["Current Toggle Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-PREVIEW-TOGGLE-OPEN",
            80.0,
            datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
            item_names=["Old Toggle Item"],
        )
        _seed_income_payment(
            module,
            cust,
            10.0,
            invoice_no="INV-PREVIEW-TOGGLE-OPEN",
            created_at=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            "/bill_preview/INV-PREVIEW-TOGGLE-CURRENT"
            "?with_dues=1"
            "&include_current=0"
            "&selected_due=INV-PREVIEW-TOGGLE-OPEN",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'data-due-summary-invoice="INV-PREVIEW-TOGGLE-CURRENT"' not in html
        assert 'data-due-summary-invoice="INV-PREVIEW-TOGGLE-OPEN"' in html
        assert 'data-due-summary-total="70.00"' in html


def test_bill_preview_with_dues_can_move_summary_below_logo(app_module, monkeypatch):
    module = app_module

    class _FakeQrResponse:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: _FakeQrResponse())

    with module.app.app_context():
        module.APP_INFO.setdefault("bill_config", {})["dues-table-position"] = "below_logo"

        cust = module.customer(name="Preview Position User", company="Preview Position Co", phone="5550010001")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-PREVIEW-POS-CURRENT",
            210.0,
            datetime(2026, 3, 28, 15, 0, tzinfo=timezone.utc),
            item_names=["Current Position Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-PREVIEW-POS-OPEN",
            90.0,
            datetime(2026, 3, 24, 15, 0, tzinfo=timezone.utc),
            item_names=["Open Position Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            "/bill_preview/INV-PREVIEW-POS-CURRENT"
            "?with_dues=1"
            "&include_current=1"
            "&selected_due=INV-PREVIEW-POS-OPEN",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert html.index('class="row mb-4 from-to-block"') < html.index('id="dueSummaryBlock"')
        assert html.index('id="dueSummaryBlock"') < html.index('<div class="invoice-heading-block mb-3">')
        assert '<div class="fw-bold fs-4">&#8377; <span class="money" data-amount="300.0"></span></div>' in html
        assert "<tfoot>" not in html


def test_mark_bill_paid_from_bill_with_dues_page_uses_remaining_balance(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Mark Due User", company="Mark Due Co", phone="5550009999")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-MARK-CURRENT",
            120.0,
            datetime(2026, 3, 28, 14, 0, tzinfo=timezone.utc),
            item_names=["Current Mark Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-MARK-DUE",
            200.0,
            datetime(2026, 3, 22, 14, 0, tzinfo=timezone.utc),
            item_names=["Old Mark Item"],
        )
        _seed_income_payment(
            module,
            cust,
            50.0,
            invoice_no="INV-MARK-DUE",
            created_at=datetime(2026, 3, 23, 14, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/bills/INV-MARK-DUE/mark-paid",
            data={
                "source": "bill_preview_dues",
                "next": "/bill_preview_dues/INV-MARK-CURRENT",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/bill_preview_dues/INV-MARK-CURRENT")

        transactions = (
            module.accountingTransaction.query
            .filter_by(invoice_no="INV-MARK-DUE", txn_type="income", is_deleted=False)
            .order_by(module.accountingTransaction.id.asc())
            .all()
        )
        assert len(transactions) == 2
        assert transactions[-1].amount == 150.0
        assert transactions[-1].remarks == "Marked as paid from Bill with Dues page."

        updated_invoice = module.invoice.query.filter_by(invoiceId="INV-MARK-DUE").one()
        assert updated_invoice.payment is True


def test_accounting_dashboard_shows_only_top_three_due_customers_and_redirects_search(app_module):
    module = app_module
    with module.app.app_context():
        customers = [
            module.customer(name="Alpha Customer", company="Alpha Prints", phone="5551000001"),
            module.customer(name="Beta Customer", company="Beta Works", phone="5551000002"),
            module.customer(name="Gamma Customer", company="Gamma Signs", phone="5551000003"),
            module.customer(name="Delta Customer", company="Delta Offset", phone="5551000004"),
        ]
        module.db.session.add_all(customers)
        module.db.session.commit()

        totals = [500.0, 400.0, 300.0, 200.0]
        for index, cust in enumerate(customers):
            _seed_invoice(
                module,
                cust,
                f"INV-ACCOUNTING-{index}",
                totals[index],
                datetime(2026, 3, 20 + index, 10, 0, tzinfo=timezone.utc),
                item_names=[f"Accounting Item {index}"],
            )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/accounting", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Top Dues" in html
        assert "Recent Transactions" not in html
        assert "Outstanding Balances by Customer" not in html
        assert "Quick Note" not in html
        assert "Company Statement" in html
        assert "input.value = entry.company || entry.name || entry.phone || '';" in html
        assert f'data-accounting-top-due="{customers[0].id}"' in html
        assert f'data-accounting-top-due="{customers[1].id}"' in html
        assert f'data-accounting-top-due="{customers[2].id}"' in html
        assert f'data-accounting-top-due="{customers[3].id}"' not in html
        assert f'href="/accounting/customer/{customers[1].id}"' in html

        search_response = client.get("/accounting?customer=Beta%20Works", follow_redirects=False)
        assert search_response.status_code == 302
        assert search_response.headers["Location"].endswith(f"/accounting/customer/{customers[1].id}")


def test_accounting_dashboard_shows_search_error_for_unknown_customer(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Search User", company="Search Works", phone="5551000010")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-SEARCH-001",
            180.0,
            datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc),
            item_names=["Search Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/accounting?customer=No%20Such%20Customer", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "No customer matched that search." in html


def test_accounting_modal_uses_lazy_bill_loading_layout(app_module):
    module = app_module
    modal_template = _read_repo_file(module, "templates/partials/accounting_modal.html")
    modal_script = _read_repo_file(module, "templates/partials/accounting_modal_script.html")

    assert "Invoice (optional)" not in modal_template
    assert "Load Customer Bills" in modal_template
    assert 'id="amountInput"' in modal_template
    assert 'id="projectedBalanceBox"' in modal_template
    assert modal_template.index('id="amountInput"') < modal_template.index('id="projectedBalanceBox"')
    assert modal_template.index('name="remarks"') < modal_template.index('id="customerBillsPane"')
    assert 'class="accounting-modal-bills-list d-grid gap-2 mt-3 d-none"' in modal_template
    assert '/accounting/customer_invoices/' in modal_script
    assert 'selected_invoice_no[]' in modal_script
    assert 'Select All' in modal_template


def test_accounting_customer_invoices_api_returns_customer_bills_with_outstanding_amounts(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Modal Bills User", company="Modal Bills Co", phone="5553330001")
        other = module.customer(name="Other Modal User", company="Other Modal Co", phone="5553330002")
        module.db.session.add_all([cust, other])
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-MODAL-NEW",
            120.0,
            datetime(2026, 3, 29, 10, 0, tzinfo=timezone.utc),
            item_names=["New Modal Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-MODAL-PAID",
            80.0,
            datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc),
            is_paid=True,
            item_names=["Paid Modal Item"],
        )
        _seed_income_payment(
            module,
            cust,
            80.0,
            invoice_no="INV-MODAL-PAID",
            created_at=datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc),
        )
        _seed_invoice(
            module,
            cust,
            "INV-MODAL-DELETED",
            55.0,
            datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
            is_deleted=True,
            item_names=["Deleted Modal Item"],
        )
        _seed_invoice(
            module,
            other,
            "INV-MODAL-OTHER",
            90.0,
            datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
            item_names=["Other Modal Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/accounting/customer_invoices/{cust.id}", follow_redirects=False)

        payload = response.get_json()
        assert response.status_code == 200
        assert payload["customer_id"] == cust.id
        assert [row["invoice_no"] for row in payload["invoices"]] == ["INV-MODAL-NEW", "INV-MODAL-PAID"]
        assert payload["invoices"][0]["outstanding_amount"] == 120.0
        assert payload["invoices"][0]["selectable"] is True
        assert payload["invoices"][1]["outstanding_amount"] == 0.0
        assert payload["invoices"][1]["is_paid"] is True
        assert payload["invoices"][1]["selectable"] is False


def test_accounting_dashboard_post_with_selected_bills_creates_split_income_transactions(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Split Pay User", company="Split Pay Co", phone="5553330010")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-SPLIT-ONE",
            100.0,
            datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
            item_names=["Split Item One"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-SPLIT-TWO",
            90.0,
            datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc),
            item_names=["Split Item Two"],
        )
        _seed_income_payment(
            module,
            cust,
            30.0,
            invoice_no="INV-SPLIT-TWO",
            created_at=datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/accounting",
            data={
                "next_url": "/accounting",
                "txn_type": "income",
                "customer_id": str(cust.id),
                "txn_date": "2026-03-29",
                "amount": "999.00",
                "mode": "bank",
                "account": "current",
                "remarks": "Split payment from modal",
                "selected_invoice_no[]": ["INV-SPLIT-ONE", "INV-SPLIT-TWO"],
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/accounting")

        txns = (
            module.accountingTransaction.query
            .filter_by(customerId=cust.id, remarks="Split payment from modal", is_deleted=False)
            .order_by(module.accountingTransaction.invoice_no.asc(), module.accountingTransaction.id.asc())
            .all()
        )
        assert len(txns) == 2
        assert [(txn.invoice_no, txn.amount) for txn in txns] == [
            ("INV-SPLIT-ONE", 100.0),
            ("INV-SPLIT-TWO", 60.0),
        ]

        invoice_one = module.invoice.query.filter_by(invoiceId="INV-SPLIT-ONE").one()
        invoice_two = module.invoice.query.filter_by(invoiceId="INV-SPLIT-TWO").one()
        assert invoice_one.payment is True
        assert invoice_two.payment is True


def test_accounting_dashboard_post_rejects_selected_bills_from_another_customer(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Valid Split User", company="Valid Split Co", phone="5553330020")
        other = module.customer(name="Wrong Split User", company="Wrong Split Co", phone="5553330021")
        module.db.session.add_all([cust, other])
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-SPLIT-VALID",
            70.0,
            datetime(2026, 3, 24, 10, 0, tzinfo=timezone.utc),
            item_names=["Valid Split Item"],
        )
        _seed_invoice(
            module,
            other,
            "INV-SPLIT-WRONG",
            85.0,
            datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
            item_names=["Wrong Split Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/accounting",
            data={
                "next_url": "/accounting",
                "txn_type": "income",
                "customer_id": str(cust.id),
                "txn_date": "2026-03-29",
                "amount": "155.00",
                "mode": "cash",
                "account": "cash",
                "remarks": "Should not save",
                "selected_invoice_no[]": ["INV-SPLIT-VALID", "INV-SPLIT-WRONG"],
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/accounting")
        assert (
            module.accountingTransaction.query
            .filter_by(remarks="Should not save", is_deleted=False)
            .count()
        ) == 0


def test_home_page_uses_company_books_and_client_statement_buttons(app_module):
    module = app_module
    with module.app.app_context():
        client = module.app.test_client()
        response = client.get("/", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert ">Statements<" not in html
        assert ">Company Books<" in html
        assert ">Client Statement<" in html
        assert "Add Transaction" in html
        assert "Generate UPI QR" not in html
        assert 'cloudUploadBtn' not in html
        assert 'data-bs-target="#recordTxnModal"' in html
        assert 'name="next_url" value="/"' in html


def test_accounting_dashboard_post_can_return_to_home(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Home Modal User", company="Home Modal Co", phone="5552000099")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/accounting",
            data={
                "next_url": "/",
                "txn_type": "income",
                "customer_id": str(cust.id),
                "invoice_no": "",
                "txn_date": "2026-03-28",
                "amount": "55.00",
                "mode": "cash",
                "account": "cash",
                "remarks": "Posted from home page",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

        txn = (
            module.accountingTransaction.query
            .filter_by(customerId=cust.id, remarks="Posted from home page", is_deleted=False)
            .order_by(module.accountingTransaction.id.desc())
            .first()
        )
        assert txn is not None
        assert txn.amount == 55.0


def test_accounting_dashboard_post_shows_success_flash_on_home(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Home Flash User", company="Home Flash Co", phone="5552000100")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/accounting",
            data={
                "next_url": "/",
                "txn_type": "income",
                "customer_id": str(cust.id),
                "txn_date": "2026-03-29",
                "amount": "75.00",
                "mode": "cash",
                "account": "cash",
                "remarks": "Posted with flash",
            },
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Transaction recorded successfully." in html


def test_company_statement_page_defaults_to_simple_mode_and_legacy_accounting_redirects(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Statement Redirect User", company="Statement Redirect Co", phone="5551000011")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()

        page_response = client.get("/accounting/statement?start=2026-03-01&end=2026-03-31", follow_redirects=False)
        page_html = page_response.get_data(as_text=True)
        assert page_response.status_code == 200
        assert "Company Statement" in page_html
        assert "Simple Statement" in page_html
        assert "Accounting Statement" in page_html
        assert 'name="customer"' not in page_html
        assert 'name="type"' not in page_html

        legacy_response = client.get(
            f"/statements/accounting?customer={cust.phone}&start=2026-03-01&end=2026-03-31&export=pdf",
            follow_redirects=False,
        )
        assert legacy_response.status_code == 302
        assert legacy_response.headers["Location"].endswith(
            f"/accounting/customer/{cust.id}/statement?start=2026-03-01&end=2026-03-31"
        )

        no_match_response = client.get(
            "/statements/accounting?customer=Statement%20Redirect%20Co&start=2026-03-01&end=2026-03-31&export=pdf",
            follow_redirects=False,
        )
        assert no_match_response.status_code == 302
        assert no_match_response.headers["Location"].endswith(
            "/accounting/statement?start=2026-03-01&end=2026-03-31&mode=accounting&export=pdf"
        )


def test_legacy_statement_routes_redirect_to_accounting_flow(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Legacy Route User", company="Legacy Route Co", phone="5551000015")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()

        blank_response = client.get("/statements/blank", follow_redirects=False)
        assert blank_response.status_code == 302
        assert blank_response.headers["Location"].endswith("/accounting/statement")

        statements_response = client.get(
            "/statements?scope=month&year=2026&month=3&phone=5551000015&export=pdf",
            follow_redirects=False,
        )
        assert statements_response.status_code == 302
        assert statements_response.headers["Location"].endswith(
            f"/accounting/customer/{cust.id}/simple-statement?start=2026-03-01&end=2026-03-31"
        )

        simple_response = client.get(
            "/statements_company?phone=5551000015&start=2026-03-01&end=2026-03-31&format=simple_pdf",
            follow_redirects=False,
        )
        assert simple_response.status_code == 302
        assert simple_response.headers["Location"].endswith(
            f"/accounting/customer/{cust.id}/simple-statement?start=2026-03-01&end=2026-03-31"
        )

        customer_response = client.get(
            "/statements_company?phone=5551000015&start=2026-03-01&end=2026-03-31",
            follow_redirects=False,
        )
        assert customer_response.status_code == 302
        assert customer_response.headers["Location"].endswith(
            f"/accounting/customer/{cust.id}?start=2026-03-01&end=2026-03-31"
        )


def test_accounting_dashboard_shows_full_paid_total_for_due_customer(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Paid Total User", company="Paid Total Co", phone="5551000012")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-PAID-TOTAL-OLD",
            80.0,
            datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc),
            is_paid=True,
            item_names=["Old Paid Item"],
        )
        _seed_income_payment(
            module,
            cust,
            80.0,
            invoice_no="INV-PAID-TOTAL-OLD",
            created_at=datetime(2026, 3, 11, 9, 0, tzinfo=timezone.utc),
        )
        _seed_invoice(
            module,
            cust,
            "INV-PAID-TOTAL-OPEN",
            200.0,
            datetime(2026, 3, 20, 9, 0, tzinfo=timezone.utc),
            item_names=["Open Due Item"],
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get("/accounting", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Paid Total Co" in html
        assert "INR 80.00" in html
        assert "Due INR 200.00" in html


def test_accounting_customer_page_defaults_to_all_time_and_shows_customer_only_data(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Ledger User", company="Ledger Co", phone="5551000020")
        other_cust = module.customer(name="Other Ledger User", company="Other Ledger Co", phone="5551000030")
        module.db.session.add_all([cust, other_cust])
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-LEDGER-OLD",
            100.0,
            datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
            item_names=["Old Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-LEDGER-NEW",
            80.0,
            datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
            item_names=["New Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-LEDGER-DELETED",
            50.0,
            datetime(2026, 3, 26, 9, 0, tzinfo=timezone.utc),
            is_deleted=True,
            item_names=["Deleted Item"],
        )
        _seed_invoice(
            module,
            other_cust,
            "INV-LEDGER-OTHER",
            75.0,
            datetime(2026, 3, 24, 9, 0, tzinfo=timezone.utc),
            item_names=["Other Item"],
        )
        _seed_income_payment(
            module,
            cust,
            50.0,
            created_at=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(f"/accounting/customer/{cust.id}", follow_redirects=False)

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert f'data-accounting-customer-page="{cust.id}"' in html
        assert 'data-accounting-invoice="INV-LEDGER-OLD"' in html
        assert 'data-accounting-invoice="INV-LEDGER-NEW"' in html
        assert 'data-accounting-invoice="INV-LEDGER-DELETED"' not in html
        assert 'data-accounting-invoice="INV-LEDGER-OTHER"' not in html
        assert re.search(r'data-accounting-summary="total_invoiced"[^>]*data-value="180\\.00"', html)
        assert re.search(r'data-accounting-summary="total_paid"[^>]*data-value="50\\.00"', html)
        assert re.search(r'data-accounting-summary="balance_due"[^>]*data-value="130\\.00"', html)
        assert 'data-accounting-transaction="' in html
        assert f'/accounting/customer/{cust.id}/statement' in html
        assert 'Simple Statement' in html
        assert f'/accounting/customer/{cust.id}/simple-statement' in html
        assert f'action="/bills/INV-LEDGER-OLD/mark-paid"' in html
        assert 'name="source" value="accounting_customer"' in html
        assert f'href="/create-bill?customer_id={cust.id}"' in html
        assert 'data-bs-target="#recordTxnModal"' in html
        assert re.search(rf'<option value="{cust.id}"[^>]*selected', html)


def test_accounting_customer_page_date_filter_narrows_invoices_transactions_and_print_url(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Filter Ledger User", company="Filter Ledger Co", phone="5551000040")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-FILTER-OLD",
            120.0,
            datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc),
            item_names=["Old Filter Item"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-FILTER-NEW",
            90.0,
            datetime(2026, 3, 24, 9, 0, tzinfo=timezone.utc),
            item_names=["New Filter Item"],
        )
        _seed_income_payment(
            module,
            cust,
            40.0,
            created_at=datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
        )
        _seed_income_payment(
            module,
            cust,
            20.0,
            created_at=datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            f"/accounting/customer/{cust.id}?start=2026-03-20&end=2026-03-31",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'data-accounting-invoice="INV-FILTER-OLD"' not in html
        assert 'data-accounting-invoice="INV-FILTER-NEW"' in html
        assert re.search(r'data-accounting-summary="total_invoiced"[^>]*data-value="90\\.00"', html)
        assert re.search(r'data-accounting-summary="total_paid"[^>]*data-value="40\\.00"', html)
        assert re.search(r'data-accounting-summary="balance_due"[^>]*data-value="50\\.00"', html)
        assert "2026-03-20" in html
        assert "2026-03-31" in html
        assert f'/accounting/customer/{cust.id}/statement?start=2026-03-20&amp;end=2026-03-31' in html
        assert f'/accounting/customer/{cust.id}/simple-statement?start=2026-03-20&amp;end=2026-03-31' in html
        assert 'start=2026-03-20' in html
        assert 'end=2026-03-31' in html


def test_accounting_customer_statement_pdf_uses_ledger_payments_and_balance(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Statement Customer", company="Statement Co", phone="5552000001")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-STMT-001",
            100.0,
            datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc),
            item_names=["Statement Item A"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-STMT-002",
            50.0,
            datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc),
            item_names=["Statement Item B"],
        )
        _seed_income_payment(
            module,
            cust,
            70.0,
            invoice_no="INV-STMT-001",
            created_at=datetime(2026, 3, 11, 9, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            f"/accounting/customer/{cust.id}/statement?start=2026-03-01&end=2026-03-31",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Customer Accounting Statement" in html
        assert "Payments Received" in html
        assert "Balance Due" in html
        assert "INR 150.00" in html
        assert "INR 70.00" in html
        assert "INR 80.00" in html
        assert "INV-STMT-001" in html
        assert "Test payment" in html


def test_company_statement_supports_simple_and_accounting_modes(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Company Statement User", company="Company Statement Co", phone="5552000090")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-COMPANY-STMT-001",
            210.0,
            datetime(2026, 3, 9, 9, 0, tzinfo=timezone.utc),
            item_names=["Company Statement Item"],
        )
        _seed_income_payment(
            module,
            cust,
            75.0,
            invoice_no="INV-COMPANY-STMT-001",
            created_at=datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        simple_response = client.get(
            "/accounting/statement?start=2026-03-01&end=2026-03-31",
            follow_redirects=False,
        )
        simple_html = simple_response.get_data(as_text=True)
        assert simple_response.status_code == 200
        assert "Simple Statement" in simple_html
        assert "All Transactions" not in simple_html
        assert "Payments Received" not in simple_html
        assert "INV-COMPANY-STMT-001" in simple_html
        assert "Company Statement Co" in simple_html
        assert "Test payment" not in simple_html
        assert 'action="/bills/INV-COMPANY-STMT-001/mark-paid"' in simple_html

        accounting_response = client.get(
            "/accounting/statement?start=2026-03-01&end=2026-03-31&mode=accounting",
            follow_redirects=False,
        )
        accounting_html = accounting_response.get_data(as_text=True)
        assert accounting_response.status_code == 200
        assert "Invoices Raised" in accounting_html
        assert "All Transactions" in accounting_html
        assert "Mode Breakdown" not in accounting_html
        assert "Account Breakdown" not in accounting_html
        assert "Customer Summary" not in accounting_html
        assert "Daily Totals" not in accounting_html
        assert "INV-COMPANY-STMT-001" in accounting_html
        assert "Company Statement Co" in accounting_html
        assert "Test payment" in accounting_html
        assert 'name="source" value="accounting_statement"' in accounting_html


def test_company_statement_simple_pdf_stays_invoice_only(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Company PDF User", company="Company PDF Co", phone="5552000091")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-COMPANY-PDF-001",
            180.0,
            datetime(2026, 3, 11, 9, 0, tzinfo=timezone.utc),
            item_names=["Company PDF Item"],
        )
        _seed_income_payment(
            module,
            cust,
            80.0,
            invoice_no="INV-COMPANY-PDF-001",
            created_at=datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            "/accounting/statement?start=2026-03-01&end=2026-03-31&mode=simple&export=pdf",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Company Statement" in html
        assert "Payments Received" not in html
        assert "All Transactions" not in html
        assert "INV-COMPANY-PDF-001" in html
        assert "Company_PDF_Co" in html


def test_accounting_customer_statement_pdf_uses_accounting_template_with_payments(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Statement PDF Customer", company="Statement PDF Co", phone="5552000002")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-STMT-PDF-001",
            120.0,
            datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
            item_names=["Statement PDF Item"],
        )
        _seed_income_payment(
            module,
            cust,
            40.0,
            invoice_no="INV-STMT-PDF-001",
            created_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            f"/accounting/customer/{cust.id}/statement?start=2026-03-01&end=2026-03-31",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Customer Accounting Statement" in html
        assert "Payments Received" in html
        assert "INR 40.00" in html
        assert "Statement_PDF_Co" in html
        assert "accounting_statement" in html
        assert 'class="brand-logo"' in html
        assert 'src="/static/' in html


def test_accounting_customer_simple_statement_keeps_invoice_only_layout(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Simple Statement Customer", company="Simple Statement Co", phone="5552000003")
        module.db.session.add(cust)
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-SIMPLE-001",
            120.0,
            datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
            item_names=["Simple Item A"],
        )
        _seed_invoice(
            module,
            cust,
            "INV-SIMPLE-002",
            55.0,
            datetime(2026, 3, 9, 9, 0, tzinfo=timezone.utc),
            item_names=["Simple Item B"],
        )
        _seed_income_payment(
            module,
            cust,
            40.0,
            invoice_no="INV-SIMPLE-001",
            created_at=datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            f"/accounting/customer/{cust.id}/simple-statement?start=2026-03-01&end=2026-03-31",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Company Statement" in html
        assert "Payments Received" not in html
        assert "Balance Due" not in html
        assert "INV-SIMPLE-001" in html
        assert "INV-SIMPLE-002" in html
        assert "175.00" in html
        assert "Simple_Statement_Co" in html
        assert "simple_statement" in html


def test_accounting_dashboard_post_can_return_to_customer_page(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Modal Return User", company="Modal Return Co", phone="5552000004")
        module.db.session.add(cust)
        module.db.session.commit()

        client = module.app.test_client()
        response = client.post(
            "/accounting",
            data={
                "next_url": f"/accounting/customer/{cust.id}?start=2026-03-01&end=2026-03-31",
                "txn_type": "income",
                "customer_id": str(cust.id),
                "invoice_no": "",
                "txn_date": "2026-03-28",
                "amount": "42.00",
                "mode": "bank",
                "account": "current",
                "remarks": "Posted from customer page",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/accounting/customer/{cust.id}?start=2026-03-01&end=2026-03-31")

        txn = (
            module.accountingTransaction.query
            .filter_by(customerId=cust.id, remarks="Posted from customer page", is_deleted=False)
            .order_by(module.accountingTransaction.id.desc())
            .first()
        )
        assert txn is not None
        assert txn.amount == 42.0


def test_accounting_customer_statement_pdf_keeps_payments_when_id_is_not_in_search_fields(app_module):
    module = app_module
    with module.app.app_context():
        cust = module.customer(name="Alpha Ledger", company="North Works", phone="5553000000")
        other = module.customer(name="Beta Ledger", company="South Works", phone="1111111111")
        module.db.session.add(cust)
        module.db.session.add(other)
        module.db.session.commit()

        id_digits = set(str(cust.id))
        safe_digit = next(d for d in "9876543210" if d not in id_digits)
        cust.phone = safe_digit * 10
        other.phone = f"000{cust.id}000999"
        module.db.session.commit()

        _seed_invoice(
            module,
            cust,
            "INV-ID-FILTER-001",
            140.0,
            datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc),
            item_names=["ID Filter Item"],
        )
        _seed_income_payment(
            module,
            cust,
            55.0,
            invoice_no="INV-ID-FILTER-001",
            created_at=datetime(2026, 3, 13, 9, 0, tzinfo=timezone.utc),
        )
        _seed_invoice(
            module,
            other,
            "INV-ID-FILTER-OTHER",
            85.0,
            datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc),
            item_names=["Other Customer Item"],
        )
        _seed_income_payment(
            module,
            other,
            25.0,
            invoice_no="INV-ID-FILTER-OTHER",
            created_at=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc),
        )
        module.db.session.commit()

        client = module.app.test_client()
        response = client.get(
            f"/accounting/customer/{cust.id}/statement?start=2026-03-01&end=2026-03-31",
            follow_redirects=False,
        )

        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Customer Accounting Statement" in html
        assert "North Works" in html
        assert "Payments Received" in html
        assert "INR 55.00" in html
        assert "INV-ID-FILTER-001" in html
        assert "South Works" not in html
        assert "INV-ID-FILTER-OTHER" not in html
        assert "INR 25.00" not in html
