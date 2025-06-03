import frappe
from frappe.utils.password import get_decrypted_password
from frappe.auth import LoginManager

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

