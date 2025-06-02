import frappe
from frappe.utils.password import get_decrypted_password
from frappe.auth import LoginManager

from frappe.utils import get_url, now_datetime, add_to_date
from datetime import timedelta


# test guest api
@frappe.whitelist(allow_guest=True)
def test_api():
    return {"message": "Hello from Sevi ERPNext API!"}

@frappe.whitelist(allow_guest=True)
def generate_login_link(email):
    """
    generate_login_link with token for user login
    :param email: User's email address
    :return: json response with login link or error message 
    """

    user = frappe.db.get_value("User", {"email": email}, "name")
    if not user:
        return {"error": "User not found"} 

    # Generate a token for the user
    try:
        token = frappe.generate_hash(length=16)
    except Exception as e:
        return {"error": f"Failed to generate token: {str(e)}"}

    # Store the token in the database
    try:
        frappe.db.set_value("User", user, "login_token", token)
    except Exception as e:
        return {"error": f"Failed to store token: {str(e)}"}

    # Generate the login link
    login_link = get_url(f"/api/method/frappe.auth.login_with_token?token={token}")
    return {"login_link": login_link}


@frappe.whitelist(methods=["POST"], allow_guest=True)
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
        