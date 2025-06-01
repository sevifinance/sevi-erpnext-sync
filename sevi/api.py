from frappe.utils import get_url
from frappe.utils.password import get_decrypted_password
import frappe

from frappe.auth import LoginManager

# test guest api
@frappe.whitelist(allow_guest=True)
def test_api():
    return {"message": "Hello from Sevi ERPNext API!"}
