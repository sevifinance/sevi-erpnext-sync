import json
import frappe
from frappe.utils.password import get_decrypted_password
from frappe.auth import LoginManager

import requests

from frappe.utils import get_url, now_datetime, add_to_date, random_string, nowdate, getdate
from datetime import timedelta
from .api_functions import create_company_doc, create_user_doc, add_user_permission_for_company, setup_company_user_role_permissions, COMPANY_USER_ROLE
# from frappe.accounts.doctype.company.company import setup_company_defaults

@frappe.whitelist(methods=["POST"])
def generate_magic_login_url(email):
    """
    Generates a magic login link for the given email.
    Args:
        email (str): The email address of the user.
    Returns:
        dict: A dictionary containing the magic_link_url or an error message.
    """

    if not email:
        frappe.throw("Email address is required.", title="Missing Email")
        return

    if not frappe.db.exists("User", email):
        frappe.throw(f"User with email {email} not found.", title="User Not Found")
        return

    user = frappe.get_doc("User", email)

    # Invalidate previous active tokens for this user (optional, but good practice)
    frappe.db.set_value("Magic Login Token", {"user": user.name, "is_used": 0, "expires_at": (">", now_datetime())}, "is_used", 1)

    # Generate a secure token
    token = frappe.generate_hash(length=32)
    expires_at = add_to_date(now_datetime(), minutes=15)

    try:
        # Create and save the Magic Login Token document
        token_doc = frappe.new_doc("Magic Login Token")
        token_doc.user = user.name
        token_doc.token = token
        token_doc.expires_at = expires_at
        token_doc.is_used = 0
        # Store IP and User Agent for security auditing
        token_doc.ip_address = frappe.local.request_ip
        # token_doc.user_agent = get_request_header("User-Agent")
        token_doc.insert(ignore_permissions=True) # Use ignore_permissions if called from a context without user permissions
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Magic Link Generation Failed")
        frappe.throw(f"Could not generate magic link. Please try again later. Error: {str(e)}", title="Generation Error")
        return

    # Construct the magic link URL
    magic_link_url = f"{get_url()}/api/method/sevi.api.process_magic_login?token={token}"
    
    # This endpoint *returns* the URL. Sending the email is a separate concern.
    return {
        "magic_link_url": magic_link_url
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def process_magic_login(token):
    """
    Processes the magic login link, validates the token, and logs the user in.
    Args:
        token (str): The magic login token from the URL.
    """

    if not token:
        frappe.respond_as_web_page("Invalid Link", "The magic login link is missing a token.", http_status_code=400)
        return

    try:
        token_doc_name = frappe.db.get_value("Magic Login Token", {
            "token": token,
            "is_used": 0,
            "expires_at": (">", now_datetime()) # Check if token is not expired
        }, "name")

        if not token_doc_name:
            # Token not found, already used, or expired
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = "/login?magic_link_error=invalid_or_expired" # Redirect to login with an error query param
            return

        token_doc = frappe.get_doc("Magic Login Token", token_doc_name)
        
        # Log in the user
        frappe.local.login_manager.login_as(token_doc.user)

        # Mark the token as used
        token_doc.is_used = 1
        token_doc.db_update()
        frappe.db.commit()

        # Redirect to the desk successful login
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app" 

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Magic Link Processing Failed")
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/login?magic_link_error=processing_failed"
        # frappe.respond_as_web_page("Login Failed", "An error occurred while trying to log you in. Please try again or contact support.", http_status_code=500)
        

@frappe.whitelist(methods=["POST"])
def create_company_api(company_name, company_abbr, company_currency, hoday_from_date, holiday_to_date, country=""):
    """
    Creates a new company.
    Args:
        company_name (str): Full name of the new company.
        company_abbr (str): Abbreviation for the new company.
        company_currency (str): Default currency for the new company (e.g., "USD", "EUR").
        country (str, optional): Country for the company.
    Returns:
        dict: Success message or error, including company details.
    """
    frappe.db.begin()
    try:
        # --- Check for existing company ---
        if frappe.db.exists("Company", {"abbr": company_abbr}):
            frappe.throw(f"Company with abbreviation {company_abbr} already exists.", title="Company Exists")
        
        if frappe.db.exists("Company", {"company_name": company_name}):
            frappe.throw(f"Company with name {company_name} already exists.", title="Company Exists")
        
        try:
            company = create_company_doc(company_name, company_abbr, company_currency, country, hoday_from_date, holiday_to_date)
        except Exception as e:
            frappe.log_error(f"Failed to create company document: {str(e)}", "Company Creation Error")
            frappe.throw(f"Could not create company document: {str(e)}", title="Company Creation Error")

        frappe.db.commit()
        return {
            "status": "success",
            "message": f"Company {company_name} created successfully.",
            "company_name": company.name,
            "company_abbr": company.abbr
        }
    except Exception as e:
        frappe.throw(f"Failed to create company custom throw: {str(e)}", title="Company Creation Error")
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Create Company API Failed")
        frappe.throw(f"Failed to create company: {str(e)}")


@frappe.whitelist(methods=["POST"]) 
def create_user_api(data):
    """
    Whitelisted method to create an Employee, a linked User, and assign roles.

    Args:
        data (dict): A dictionary containing all necessary fields for both Employee and User.
                     Example: {
                        "data": {
                            "employee_name": "Dame Bahiru",
                            "company": "Other Sevi",
                            "date_of_joining": "2024-01-01",
                            "email": "dame.bahiru@sevi.com",
                            "first_name": "Dame",
                            "last_name": "Bahiru",
                            "date_of_birth": "2000-01-01",
                            "gender": "Male"
                        }
                    }

    Returns:
        dict: A dictionary containing the name of the created employee and user email,
              or an error message.
    """
    # Extract employee-specific data
    company = data.get("company")
    first_name = data.get("first_name", "")
    last_name = data.get("last_name", "")
    employee_name = first_name + " " + last_name if first_name and last_name else data.get("employee_name", "")
    date_of_joining = data.get("date_of_joining")
    date_of_birth = data.get("date_of_birth") 
    gender = data.get("gender", "Not Specified")

    # Add other employee fields as needed from the 'data' dictionary

    # Extract user-specific data
    user_email = data.get("email")
    user_type = data.get("user_type", "System User")
    enabled = data.get("enabled", 1)

    permissions_to_set = [
            {"doctype": "Item", "read": 1, "write": 1, "create": 1, "delete": 0, "submit": 0, "cancel": 0, "amend": 0},
            {"doctype": "Customer", "read": 1, "write": 1, "create": 1, "delete": 0},
            {"doctype": "Supplier", "read": 1, "write": 1, "create": 1, "delete": 0},
            {"doctype": "Sales Order", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Purchase Order", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Sales Invoice", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Purchase Invoice", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Company", "read": 1, "write": 0, "create": 0, "delete": 0}, # Allow reading their own company
            # Add more DocTypes and their permissions as needed
        ]
    roles = data.get("roles", permissions_to_set)

    # Basic validation for mandatory fields
    if not first_name and not last_name:
        frappe.throw("First Name and Last Name is mandatory.")
    if not company:
        frappe.throw("Company is mandatory for Employee.")
    if not user_email:
        frappe.throw("User email is mandatory for creating a User.")
    if not date_of_joining:
        frappe.throw("Date of Joining is mandatory for Employee.")
    if not date_of_birth:
        frappe.throw("Date of Birth is mandatory for Employee.")
    if not gender:
        frappe.throw("Gender is mandatory for Employee.")

    if not frappe.has_permission("Employee", "create") or \
       not frappe.has_permission("User", "create"):
        frappe.throw("You do not have permission to create Employees or Users.")

    try:
        employee_doc = frappe.get_doc({
            "doctype": "Employee",
            "first_name": first_name,
            "last_name": last_name,
            "employee_name": employee_name,
            "company": company,
            "date_of_joining": getdate(date_of_joining),
            "date_of_birth": getdate(date_of_birth),
            "gender": gender,
            "status": "Active"
        })
        employee_doc.insert(ignore_permissions=False)
        frappe.db.commit() 

        frappe.msgprint(f"Employee '{employee_doc.name}' created successfully.")

        if frappe.db.exists("User", user_email):
            frappe.throw(f"User with email '{user_email}' already exists.")

        user_doc = frappe.get_doc({
            "doctype": "User",
            "email": user_email,
            "first_name": first_name,
            "last_name": last_name,
            "user_type": user_type,
            "roles": roles 
        })
        user_doc.insert(ignore_permissions=False)
        frappe.db.commit()

        frappe.msgprint(f"User '{user_doc.email}' created and roles assigned successfully.")

        employee_doc.user_id = user_email
        employee_doc.save(ignore_permissions=False)
        frappe.db.commit()

        frappe.msgprint(f"User '{user_email}' linked to Employee '{employee_doc.name}'.")

        return {
            "status": "success",
            "employee_name": employee_doc.name,
            "user_email": user_doc.email,
            "message": "Employee, User, and linking completed successfully."
        }

    except frappe.exceptions.DuplicateEntryError as e:
        frappe.db.rollback()
        frappe.throw(f"Duplicate entry error: {e}")
    except frappe.exceptions.ValidationError as e:
        frappe.db.rollback()
        frappe.throw(f"Validation error: {e}")
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "create_employee_and_user_with_roles API Error")
        frappe.throw(f"An unexpected error occurred: {e}")

@frappe.whitelist(methods=["POST"], allow_guest=True)
def order_create(doc="{\"discount\": \"0000\"}"):
    """
    Creates Order Create with required fields
    """
    # TODO: check if exists and Create linked doctypes if not

    doc_data = json.loads(doc) if doc != "" else json.loads({"discount": "000"})
    frappe.msgprint(f"data : {doc}")

    # TODO: Create a new order create

    return {"message": "order create", "doc_discount": doc_data["discount"]}

@frappe.whitelist(methods=["POST"],)
def sync_order_hook():
    """
    Syinc Erpnext to Sevi api method
    """

    sevi_setting = frappe.get_single("Sevi Settings")

    if not sevi_setting.get_password('sevi_token'):
        frappe.throw("Token is Missing")

    if not sevi_setting.get('order_hook_url'):
        frappe.throw("Order Hook URL is Missing")

    url = sevi_setting.get("sevi_url")
    try:
        payload = "{\"query\":\"mutation SetUpWebhooks($input: WebhookInput!) {\\r\\n  setUpWebhooks(input: $input) {\\r\\n    account\\r\\n    event\\r\\n    id\\r\\n    url\\r\\n  }\\r\\n}\",\"variables\":{\"input\":{\"event\":\"ORDER\",\"url\":\""+sevi_setting.get('order_hook_url')+"\"}}}"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': sevi_setting.get_password('sevi_token')
        }

        response = requests.request("POST", url, headers=headers, data=payload)
        response_data = json.loads(response.text)
        if response_data.get('errors'):
            frappe.throw(msg=response.text, title="Create Order Hook Failed!")
    except:
        frappe.throw(msg="Eror Settingup Order Hook URL", title="Create Order Hook Failed!")

    frappe.msgprint(msg=f"response : {json.loads(response.text)}", title="Order Hook Created Successfully!", indicator="green")

    return {"message": "order create"}

@frappe.whitelist(methods=["POST"],)
def sync_transaction_hook():
    """
    Syinc Erpnext to Sevi api method
    """

    sevi_setting = frappe.get_single("Sevi Settings")

    if not sevi_setting.get_password('sevi_token'):
        frappe.throw("Token is Missing")

    if not sevi_setting.get('transaction_hook_url'):
        frappe.throw("Transaction Hook URL is Missing")

    url = sevi_setting.get("sevi_url")

    try:
        payload = "{\"query\":\"mutation SetUpWebhooks($input: WebhookInput!) {\\r\\n  setUpWebhooks(input: $input) {\\r\\n    account\\r\\n    event\\r\\n    id\\r\\n    url\\r\\n  }\\r\\n}\",\"variables\":{\"input\":{\"event\":\"TRANSACTION\",\"url\":\""+sevi_setting.get('transaction_hook_url')+"\"}}}"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': sevi_setting.get_password('sevi_token')
        }

        response = requests.request("POST", url, headers=headers, data=payload)
        response_data = json.loads(response.text)
        if response_data.get('errors'):
            frappe.throw(msg=response.text, title="Create Transaction Hook Failed!")
    except:
        frappe.throw(msg="Eror Settingup Transaction Hook URL", title="Create Order Hook Failed!")

    frappe.msgprint(msg=f"response : {response.text}", title="Transaction Hook Created Successfully!", indicator="green")

    return {"message": "order created"}

@frappe.whitelist(methods=["POST"], allow_guest=True)
def order_hook():
    """
    Sevi Order Hook
    the request body example

    {
        "order": {
            "platformReference": "someRef",
            "paymentStatus": "",
            "items": [
                {
                    "currency": "KES",
                    "description": "Fast laptop",
                    "gallery": [
                        {
                            "url": "https://sevi-products.s3.eu-central-1.amazonaws.com/5ef5a8523c4a0829ed31b0e0/30965f4f-2ead-49ae-b41b-d2d6fc86dece/org.jpg"
                        }
                    ],
                    "name": "Some order",
                    "quantity": 1,
                    "quantityUnit": "pieces",
                    "unitPrice": 100,
                    "price": 100,
                    "vendorId": "caaabbb",
                    "vendorType": "COMPANY",
                    "vendorName": "selling company",
                    "affiliateId": "",
                    "id": "11fc1228-301c-4dde-8b99-119b949ba868",
                    "ecommercePlatform": "CUSTOM",
                    "createdAt": "2023-06-15T09:27:50.267Z"
                }
            ],
            "amount": 100,
            "userId": "5ef5a8523c4a0829ed31b0e0",
            "status": "PENDING",
            "name": "Mahamed",
            "currency": "KES",
            "customerId": "pbe191f53",
            "creditConfigurationId": "100ToVendorId",
            "vendorId": "caaabbb",
            "phoneNumber": "+254743623754",
            "referenceNumber": "D10138",
            "billingId": "d3b4c356-1107-46b0-8969-baa34c86d5a7",
            "shippingId": "f7c366ba-b37c-43ee-94bb-eda187ce3f5b",
            "deliverDirect": true
        },
        "users": [
            {
                "userId": "5ef5a8523c4a0829ed31b0e0",
                "walletId": "pbe191f53",
                "role": "ADMIN",
                "user": {
                    "name": "John Sevi Boy",
                    "userPhoto": "https://images.unsplash.com/photo-1459180129673-eefb56f79b45?ixlib=rb-1.2.1&ixid=MnwxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8&auto=format&fit=crop&w=1173&q=80",
                    "phoneNumber": "+254743623754",
                    "countryCode": "KE",
                    "documentId": "352342",
                    "email": null
                }
            },
            {
                "userId": "demoUserId",
                "walletId": "demoWalletId",
                "role": "USER",
                "user": {
                    "name": "Demo User",
                    "userPhoto": "https://example.com/demo_user.jpg",
                    "phoneNumber": "+1234567890",
                    "countryCode": "US",
                    "documentId": "123456",
                    "email": "demo@example.com"
                }
            }
        ]
    }

    """

    try:
        payload = frappe.request.json
        order_data = payload.get("order")
        # users_data = payload.get("users")

        if not order_data:
            frappe.throw("Invalid request: 'order' data is missing.")

        # Extract primary customer information (you might need to refine this based on your logic)
        customer_name = order_data.get("name")
        customer_phone = order_data.get("phoneNumber")
        customer_id = order_data.get("customerId") # This could be used to link to an existing ERPNext Customer

        # Attempt to find an existing customer or create a new one
        # This is a simplified example; you'd want more robust logic for customer management
        customer_docname = None
        if customer_phone:
            # Try to find by customer_id if you have a custom field for it, or by name/phone
            existing_customer = frappe.db.get_value("Customer", {"mobile_no": customer_phone})

            if existing_customer:
                customer_docname = existing_customer
            else:
                # Create a new customer if not found
                try:
                    customer_doc = frappe.get_doc({
                        "doctype": "Customer",
                        "customer_name": customer_name,
                        "customer_type": "Individual", # Or "Company" based on your needs
                        "mobile_no": customer_phone,
                        # Add more fields if available in the payload like email
                    })
                    customer_doc.insert(ignore_permissions=True)
                    customer_docname = customer_doc.name
                    frappe.msgprint(f"New Customer '{customer_docname}' created.")
                except Exception as e:
                    frappe.log_error(f"Error creating customer: {e}", "Sevi Order Hook Error")
                    frappe.throw(f"Failed to create customer: {e}")
        else:
            frappe.throw("Customer ID is missing in the order data. Cannot create Sales Order without a customer.")

        if not customer_docname:
            frappe.throw("Could not determine customer for Sales Order.")

        # Create Sales Order
        sales_order = frappe.get_doc({
            "doctype": "Sales Order",
            "customer": customer_docname,
            "transaction_date": frappe.utils.nowdate(),
            "delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7), # Example: deliver in 7 days
            "order_type": "Sales",
            "currency": order_data.get("currency", "KES"),
            "conversion_rate": 1, # Assuming 1 for local currency, adjust if multi-currency
            # "set_warehouse": "Stores - S", # Default warehouse, adjust as needed
            "po_no": order_data.get("platformReference"), # Use platformReference as PO No.
            # "custom_sevi_reference": order_data.get("referenceNumber"), # If you have a custom field for Sevi reference
            "total_commission_amount": 0, # Initialize or calculate if applicable
        })

        # Add items to Sales Order
        items = order_data.get("items", [])
        if not items:
            frappe.throw("No items found in the order payload.")

        for item_data in items:
            item_code = frappe.db.get_value("Item", {"item_code": item_data.get("id")})
            if not item_code:
                # Handle cases where item might not exist in ERPNext
                # Option 1: Create the item (requires more fields and logic)
                # Option 2: Skip the item and log a warning
                # Option 3: Throw an error
                frappe.log_warn(f"Item '{item_data.get('name')}' not found in ERPNext. Skipping.", "Sevi Order Hook Warning")
                continue
            sales_order.append("items", {
                "item_code": item_code,
                "item_name": item_data.get("name"),
                # "description": item_data.get("description"),
                "qty": item_data.get("quantity"),
                # "uom": item_data.get("quantityUnit", "Nos"),
                "rate": item_data.get("unitPrice"),
                "amount": item_data.get("price"),
                # "warehouse": "Stores - SIT", # Default item warehouse
            })

        # Save the Sales Order
        sales_order.insert(ignore_permissions=True)
        sales_order.submit() # Submit the sales order if it should be submitted directly

        frappe.msgprint(f"Sales Order {sales_order.name} created and submitted successfully.")
        frappe.log_error(f"Sales Order {sales_order.name} created successfully.", "Sevi Order Creation Success")


        return {
            "message": f"Sales Order {sales_order.name} created successfully",
            "sales_order_name": sales_order.name
        }

    except frappe.exceptions.ValidationError as e:
        frappe.log_error(f"Validation Error: {e}", "Sevi Order Hook Error")
        frappe.respond_as_json({"error": str(e)}, http_status_code=400)
    except Exception as e:
        frappe.log_error(f"An unexpected error occurred: {e}", "Sevi Order Hook Error")
        frappe.respond_as_json({"error": f"An unexpected error occurred: {e}"}, http_status_code=500)

    return {"message": "order hook is called"}

@frappe.whitelist(methods=["POST"], allow_guest=True)
def transaction_hook():
    """
    Sevi Transaction Hook

    the request body example

    {
        "amount": 700,
        "fee": 0,
        "totalAmount": 700,
        "method": "SEVI",
        "transactionType": "TOP_UP",
        "paymentType": "CREDIT",
        "paymentTypeId": "de3383d4-635a-4cbf-9aec-660e04c75f5f",
        "walletId": "bankKenya",
        "description": "Pay Installment: I-10166",
        "currency": "KES",
        "id": "5dqkt6qnmb",
        "balanceBefore": 10000,
        "balanceAfter": 9500,
        "orderId": "6d59b7e1-4450-4900-b980-5b1129700270",
        "order": {
            "platformReference":"someRef"
        }
    }

    """

    try:
        payload = frappe.request.json
        frappe.log_debug(f"Transaction hook payload: {payload}", "Sevi Transaction Hook Debug")

        amount = payload.get("amount")
        currency = payload.get("currency")
        transaction_type = payload.get("transactionType")
        payment_type = payload.get("paymentType")
        sevi_transaction_id = payload.get("id")
        description = payload.get("description")
        order_id = payload.get("orderId")
        sevi_wallet_id = payload.get("walletId") # Renamed to avoid conflict with ERPNext wallet concepts

        if not all([amount, currency, transaction_type, payment_type, sevi_transaction_id]):
            frappe.throw("Missing essential transaction data in payload.")

        # Determine the ERPNext Bank or Cash Account based on sevi_wallet_id or method
        # IMPORTANT: You need a mapping for sevi_wallet_id to your ERPNext Chart of Accounts.
        # This is a critical point. For example, if "bankKenya" maps to "Bank of Kenya - SIT".
        # You might have a custom DocType to store these mappings, or use a dictionary.
        erpnext_payment_account = None
        
        # Example mapping (you should define this based on your ERPNext setup)
        wallet_to_account_map = {
            "bankKenya": "Bank of Kenya - SIT", # Replace with your actual Bank Account
            "mobileMoney": "M-Pesa Account - SIT", # Example for mobile money
            # Add more mappings as needed
        }
        
        erpnext_payment_account = wallet_to_account_map.get(sevi_wallet_id)

        if not erpnext_payment_account:
            # Fallback or strict error. Let's try to get a default cash/bank account.
            frappe.log_warn(f"No direct ERPNext account mapping for Sevi walletId '{sevi_wallet_id}'. Attempting to find a default Bank/Cash account.", "Sevi Transaction Hook")
            
            # Try to get a default bank or cash account
            default_account_name = frappe.db.get_value("Account", {"account_type": ["in", ["Bank", "Cash"]], "is_group": 0})
            if default_account_name:
                erpnext_payment_account = default_account_name
            else:
                frappe.throw("No ERPNext Bank or Cash Account could be determined for this transaction. Please configure mapping or default accounts.")


        # --- Handle TOP_UP / CREDIT as Payment Entry ---
        if transaction_type == "TOP_UP" and payment_type == "CREDIT":
            # Attempt to find the Sales Order based on orderId (if it's the ERPNext Sales Order name)
            # If your orderId from Sevi is the 'name' of the Sales Order in ERPNext, or a custom field
            sales_order_name = None
            if order_id:
                # OPTION 1: Check if Sevi orderId matches ERPNext Sales Order 'name'
                sales_order_name = frappe.db.get_value("Sales Order", order_id)
                
                # OPTION 2: If you have a custom field in Sales Order for Sevi's orderId
                if not sales_order_name:
                    sales_order_name = frappe.db.get_value("Sales Order", {"custom_sevi_order_id": order_id}) # Example custom field
                
                # OPTION 3: If you used 'platformReference' as 'po_no' in Sales Order
                if not sales_order_name and payload.get("order", {}).get("platformReference"):
                    sales_order_name = frappe.db.get_value("Sales Order", {"po_no": payload["order"]["platformReference"]})

                if not sales_order_name:
                    frappe.log_warn(f"Sales Order with Sevi orderId '{order_id}' or platformReference not found. Payment Entry may not be linked.", "Sevi Transaction Hook Warning")

            # Determine the customer
            customer_name = None
            if sales_order_name:
                customer_name = frappe.db.get_value("Sales Order", sales_order_name, "customer")
            elif description and "Pay Installment:" in description:
                # Example: "Pay Installment: I-10166" -> try to get customer from linked invoice
                try:
                    invoice_name = description.split("Pay Installment: ")[1].strip()
                    customer_name = frappe.db.get_value("Sales Invoice", {"name": invoice_name}, "customer")
                except IndexError:
                    pass # Not in expected format

            # Fallback for customer if not found through SO/SI, but crucial for Payment Entry
            # You might need to have a strategy here, e.g., default customer or strict error
            if not customer_name:
                frappe.throw("Could not determine customer for this transaction. Payment Entry cannot be created without a customer.")

            # Create Payment Entry
            payment_entry = frappe.get_doc({
                "doctype": "Payment Entry",
                "payment_type": "Receive",
                "mode_of_payment": payload.get("method", "Bank Transfer"), # Use Sevi method or a default
                "transaction_date": nowdate(),
                "paid_amount": amount,
                "received_amount": amount,
                "paid_from": "Debtors - SIT", # Default Debtors account, adjust if different
                "paid_to": erpnext_payment_account, # Use the determined Bank/Cash account
                "paid_from_account_currency": currency,
                "paid_to_account_currency": currency,
                "party_type": "Customer",
                "party": customer_name,
                "references": [],
                "custom_sevi_transaction_id": sevi_transaction_id, # Custom field to store Sevi's transaction ID
                "remarks": description or f"Sevi Transaction ID: {sevi_transaction_id}",
            })

            # Link to Sales Order or Sales Invoice if found
            if sales_order_name:
                payment_entry.append("references", {
                    "reference_doctype": "Sales Order",
                    "reference_name": sales_order_name,
                    "bill_no": sales_order_name, # Can also be sales_order_name or actual invoice number if linked later
                    "due_date": nowdate(), # Or relevant due date from SO/SI
                    "total_amount": amount,
                    "outstanding_amount": amount,
                    "allocated_amount": amount
                })
            # Add more logic here to link to Sales Invoice if the description maps to an invoice
            # or if you have a method to find outstanding invoices for the customer

            payment_entry.insert(ignore_permissions=True)
            payment_entry.submit()

            frappe.msgprint(f"Payment Entry {payment_entry.name} created and submitted successfully.")
            frappe.log_error(f"Payment Entry {payment_entry.name} created successfully.", "Sevi Transaction Creation Success")

            return {
                "message": f"Payment Entry {payment_entry.name} created successfully",
                "payment_entry_name": payment_entry.name
            }

        else:
            # --- Handle other transaction types or log for manual review ---
            frappe.msgprint(f"Unsupported transaction type/payment type combination: {transaction_type}/{payment_type}. Logging for review.")
            frappe.log_warn(f"Unsupported Sevi transaction: Type={transaction_type}, PaymentType={payment_type}, ID={sevi_transaction_id}", "Sevi Transaction Hook - Unhandled")
            # You might want to create a Journal Entry for these or a custom doctype
            return {"message": "Transaction received, but not processed for document creation (unsupported type)."}

    except frappe.exceptions.ValidationError as e:
        frappe.log_error(f"Validation Error creating ERPNext document: {e}", "Sevi Transaction Hook Error")
        frappe.respond_as_json({"error": str(e)}, http_status_code=400)
    except Exception as e:
        frappe.log_error(f"An unexpected error occurred during transaction processing: {e}", "Sevi Transaction Hook Error")
        frappe.respond_as_json({"error": f"An unexpected error occurred: {e}"}, http_status_code=500)


    return {"message": "Transaction hook is called"}
