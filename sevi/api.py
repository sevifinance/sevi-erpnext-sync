import frappe
from frappe.utils.password import get_decrypted_password
from frappe.auth import LoginManager

from frappe.utils import get_url, now_datetime, add_to_date, random_string, nowdate
from datetime import timedelta
from .api_functions import create_company_doc, create_user_doc, add_user_permission_for_company, setup_company_user_role_permissions
# from frappe.accounts.doctype.company.company import setup_company_defaults

COMPANY_USER_ROLE = "Company User"

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
def create_user_for_company_api(email, first_name, company_identifier):
    """
    Creates a new user and links them to an existing company, restricting access.
    Args:
        email (str): Email for the new user (will be their username).
        first_name (str): First name of the user.
        company_identifier (str): The name or abbreviation of the company to link the user to.
    Returns:
        dict: Success message or error, including user details.
    """
    frappe.db.begin()
    try:
        # if frappe.db.exists("User", email):
        #     frappe.throw(f"User with email {email} already exists.", title="User Exists")

        company_doc = None
        if frappe.db.exists("Company", company_identifier):
            company_doc = frappe.get_doc("Company", company_identifier)
        elif frappe.db.exists("Company", {"abbr": company_identifier}):
            company_doc_name = frappe.db.get_value("Company", {"abbr": company_identifier}, "name")
            if company_doc_name:
                company_doc = frappe.get_doc("Company", company_doc_name)
        
        if not company_doc:
            frappe.throw(f"Company with identifier '{company_identifier}' not found.", title="Company Not Found")

        user = create_user_doc(email, first_name, company_doc.name) 
        
        add_user_permission_for_company(user.name, company_doc.name)

        # frappe.db.set_value("User", user.name, "default_company", company_doc.name)

        frappe.db.commit()
        return {
            "status": "success",
            "message": f"User {email} created and linked to company {company_doc.name} successfully. Role '{COMPANY_USER_ROLE}' permissions configured.",
            "user_name": user.name,
            "linked_company_name": company_doc.name
        }
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Create User for Company API Failed")
        frappe.throw(f"Failed to create user for company: {str(e)}")

