import frappe
from frappe.utils.password import get_decrypted_password
from frappe.auth import LoginManager

from frappe.utils import get_url, now_datetime, add_to_date, random_string, nowdate
from datetime import timedelta

COMPANY_USER_ROLE = "Company User"

def create_company_doc(company_name, abbr, default_currency, country, hoday_from_date, holiday_to_date):
    """Creates a Company document and its default Holiday List."""
    
    holiday_list_name = f"Holidays for {abbr}" 
    try:
        if not frappe.db.exists("Holiday List", holiday_list_name):

            holiday_list = frappe.new_doc("Holiday List")
            holiday_list.name = holiday_list_name 
            holiday_list.holiday_list_name = holiday_list_name 
            holiday_list.from_date = frappe.utils.getdate(hoday_from_date) 
            holiday_list.to_date = frappe.utils.getdate(holiday_to_date) 
            holiday_list.save(ignore_permissions=True)
            # frappe.msgprint(f"Holiday List '{holiday_list_name}' created successfully.", indicator='green')
        else:
            holiday_list = frappe.get_doc("Holiday List", holiday_list_name)
            frappe.msgprint(f"Holiday List '{holiday_list_name}' already exists.", indicator='blue')
    except Exception as e:
        frappe.log_error(f"Failed to create or retrieve Holiday List: {str(e)}", "Holiday List Creation Error")
        frappe.throw(f"Could not create or retrieve Holiday List in def: {str(e)}", title="Holiday List Error")

    company = frappe.new_doc("Company")
    company.company_name = company_name
    company.abbr = abbr
    company.default_currency = default_currency
    company.country = country
    company.default_holiday_list = holiday_list.name
    company.insert(ignore_permissions=True)
    frappe.msgprint(f"Company '{company_name}' created with Holiday List '{holiday_list.name}'.", indicator='green')
    return company

def new_doc(doctype, **kwargs):
    """
    Creates a new document of the specified doctype with the provided keyword arguments.
    If the doctype does not exist, it raises an exception.
    """
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(f"Doctype '{doctype}' does not exist.", title="Doctype Not Found")
    
    doc = frappe.new_doc(doctype)
    for key, value in kwargs.items():
        setattr(doc, key, value)
    
    return doc

def create_user_doc(email, first_name, company_context_name=None, permissions=None):
    """Creates a User document and configures the COMPANY_USER_ROLE if new."""

    role_created_now = False
    if not frappe.db.exists("Role", COMPANY_USER_ROLE):
        role = frappe.new_doc("Role")
        role.role_name = COMPANY_USER_ROLE
        role.desk_access = 1 
        role.insert(ignore_permissions=True)
        role_created_now = True
        frappe.msgprint(f"Role '{COMPANY_USER_ROLE}' created.", indicator='green')
    
    if not frappe.db.exists("User", email):
        user = frappe.new_doc("User")
        user.email = email
        user.first_name = first_name
        user.send_welcome_email = 0 
        user.user_type = "System User"
        
        user.add_roles(COMPANY_USER_ROLE)

        user.enabled = 1
        # user.insert(ignore_permissions=True)
        frappe.msgprint(f"User '{email}' created and assigned role '{COMPANY_USER_ROLE}'.", indicator='green')
    else:
        user = frappe.get_doc("User", email)
        if COMPANY_USER_ROLE not in user.roles:
            user.add_roles(COMPANY_USER_ROLE)
            user.email = email  # Ensure email is set correctly
            user.first_name = first_name  # Update first name if needed
            user.enabled = 1  # Ensure user is enabled
            user.user_type = "System User"  # Ensure user type is set correctly
            user.save(ignore_permissions=True)
            frappe.msgprint(f"User '{email}' already exists. Role '{COMPANY_USER_ROLE}' assigned.", indicator='blue')

    if role_created_now:
        setup_company_user_role_permissions(COMPANY_USER_ROLE, permissions=permissions)
        frappe.msgprint(f"Default permissions for role '{COMPANY_USER_ROLE}' have been configured.", indicator='green')
    
    return user

def setup_company_user_role_permissions(role_name, permissions=None):
    """
    Sets up default permissions for the specified role.
    These are basic permissions; further customization might be needed via Role Permissions Manager.
    """
    if permissions is None:
        permissions_to_set = [
            {"doctype": "Item", "read": 1, "write": 1, "create": 1, "delete": 0, "submit": 0, "cancel": 0, "amend": 0},
            {"doctype": "Customer", "read": 1, "write": 1, "create": 1, "delete": 0},
            {"doctype": "Supplier", "read": 1, "write": 1, "create": 1, "delete": 0},
            {"doctype": "Sales Order", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Purchase Order", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Sales Invoice", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Purchase Invoice", "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
            {"doctype": "Company", "read": 1, "write": 0, "create": 0, "delete": 0}, # Allow reading their own company
            {"doctype": "Holiday List", "read": 1, "write": 0, "create": 0, "delete": 0},
            # Add more DocTypes and their permissions as needed
        ]
    else:
        permissions_to_set = permissions

    for perm_config in permissions_to_set:
        doctype_name = perm_config.pop("doctype")
        
        # Check if permission already exists for this role and doctype
        existing_perm = frappe.db.exists("DocPerm", {
            "parent": doctype_name,
            "role": role_name,
            "permlevel": 0 # Usually, we set for permlevel 0
        })

        if not existing_perm:
            try:
                # Add new permission
                docperm = frappe.new_doc("DocPerm")
                docperm.parent = doctype_name
                docperm.parenttype = "DocType"
                docperm.parentfield = "permissions"
                docperm.role = role_name
                docperm.permlevel = 0
                for key, value in perm_config.items():
                    docperm.set(key, value)
                docperm.insert(ignore_permissions=True) # System Manager or equivalent rights needed
            except Exception as e:
                frappe.log_error(f"Failed to add permission for {doctype_name} to role {role_name}: {e}", "Role Permission Setup")
        else:
            # Update existing permission (optional, if you want to ensure these specific flags are set)
            # For simplicity, this example focuses on adding if not exists.
            # To update: frappe.db.set_value("DocPerm", existing_perm_name, perm_config)
            # Or use update_permission_property for specific flags.
            pass
            
    # After adding/updating DocPerms, clear cache for permissions to take effect immediately.
    frappe.clear_cache(doctype="DocPerm")
    frappe.clear_cache(doctype=role_name) # Clear role cache
    # frappe.permissions.reset_perms_for(role_name) # Reset permissions for the role

    frappe.msgprint(f"Permissions for role '{role_name}' processed for specified DocTypes.")

def add_user_permission_for_company(user_name, company_name):
    """
    Adds a User Permission record to restrict the user to a specific company.
    """
    if not frappe.db.exists("User Permission", {
        "user": user_name,
        "allow": "Company",
        "for_value": company_name
    }):
        perm = frappe.new_doc("User Permission")
        perm.user = user_name
        perm.allow = "Company"
        perm.for_value = company_name
        perm.is_default = 1
        perm.insert(ignore_permissions=True)
        frappe.msgprint(f"Added User Permission for User '{user_name}' to Company '{company_name}'.")
    else:
        frappe.msgprint(f"User Permission for User '{user_name}' to Company '{company_name}' already exists.")
