# Whitelisted API Method for Order Creation and Synchronization in Frappe/ERPNext

Based on your requirements, I'll create a whitelisted API method in Frappe/ERPNext that:
1. Creates an order in ERPNext
2. Synchronizes it with the Sevi system via GraphQL
3. Handles the payment QR code generation

## Implementation

### 1. Create a new Python file for the API

Create a file at `/apps/your_app/your_app/api/order.py`:

```python
import frappe
import requests
import json
from frappe.utils import get_url, now_datetime
from frappe.utils.pdf import get_pdf
from frappe.utils.print_format import download_pdf
import qrcode
import io
import base64

@frappe.whitelist(allow_guest=False)
def create_and_sync_order(order_data):
    """
    Create an order in ERPNext and sync with Sevi system
    
    Args:
        order_data (dict): Order data including items, customer, etc.
        
    Returns:
        dict: Created order and payment link information
    """
    try:
        # Validate input
        if not isinstance(order_data, dict):
            order_data = json.loads(order_data)
            
        # 1. Create the order in ERPNext
        order = create_erpnext_order(order_data)
        
        # 2. Sync with Sevi system
        sevi_response = sync_order_with_sevi(order.name, order_data)
        
        # 3. Generate payment QR code if payment link exists
        payment_qr = None
        if sevi_response.get('paymentLink'):
            payment_qr = generate_qr_code(sevi_response['paymentLink'])
            
        # 4. Attach QR code to the order
        if payment_qr:
            attach_qr_to_order(order.name, payment_qr)
            
        return {
            'erpnext_order': order.name,
            'sevi_order_id': sevi_response.get('orderId'),
            'payment_link': sevi_response.get('paymentLink'),
            'status': 'success'
        }
        
    except Exception as e:
        frappe.log_error(f"Order sync failed: {str(e)}")
        return {
            'status': 'error',
            'message': str(e)
        }

def create_erpnext_order(order_data):
    """
    Create Sales Order in ERPNext
    """
    # Map Sevi order data to ERPNext fields
    so = frappe.new_doc("Sales Order")
    so.customer = get_or_create_customer(order_data.get('customerId'), order_data.get('billing'))
    so.transaction_date = now_datetime().date()
    so.delivery_date = frappe.utils.add_days(so.transaction_date, 7)
    
    # Add items
    for item in order_data.get('items', []):
        so.append("items", {
            'item_code': get_or_create_item(item),
            'qty': item.get('quantity', 1),
            'rate': item.get('unitPrice'),
            'uom': item.get('quantityUnit', 'Nos')
        })
    
    # Set additional fields
    so.po_no = order_data.get('platformReference')
    so.sevi_order_id = order_data.get('orderId')
    so.sevi_vendor_id = order_data.get('vendorId')
    so.sevi_payment_method = order_data.get('paymentMethod')
    
    # Save and submit
    so.insert(ignore_permissions=True)
    so.submit()
    
    return so

def get_or_create_customer(customer_id, billing_info):
    """
    Get or create customer in ERPNext based on Sevi customer ID
    """
    if not customer_id and billing_info:
        # Use phone number to find customer
        customer_id = billing_info.get('phoneNumber')
    
    if customer_id:
        # Check if customer exists with sevi_customer_id
        customer = frappe.db.get_value("Customer", {"sevi_customer_id": customer_id}, "name")
        if customer:
            return customer
            
    # Create new customer
    customer = frappe.new_doc("Customer")
    customer.customer_name = billing_info.get('firstName', '') + ' ' + billing_info.get('lastName', '')
    customer.customer_type = "Individual"
    customer.sevi_customer_id = customer_id
    customer.mobile_no = billing_info.get('phoneNumber')
    customer.email_id = billing_info.get('email')
    
    # Create customer address
    customer.insert(ignore_permissions=True)
    
    address = frappe.new_doc("Address")
    address.address_line1 = billing_info.get('address1')
    address.address_line2 = billing_info.get('address2')
    address.city = billing_info.get('city')
    address.country = billing_info.get('country')
    address.pincode = billing_info.get('postcode')
    address.state = billing_info.get('state')
    address.address_type = "Billing"
    address.is_primary_address = 1
    address.is_shipping_address = 1
    address.append("links", {
        "link_doctype": "Customer",
        "link_name": customer.name
    })
    address.insert(ignore_permissions=True)
    
    return customer.name

def get_or_create_item(item_data):
    """
    Get or create item in ERPNext based on Sevi item data
    """
    item_code = frappe.db.get_value("Item", {"sevi_item_id": item_data.get('id')}, "name")
    if item_code:
        return item_code
        
    # Create new item
    item = frappe.new_doc("Item")
    item.item_code = item_data.get('name')[:140]  # Truncate to 140 chars
    item.item_name = item_data.get('name')
    item.description = item_data.get('description')
    item.stock_uom = item_data.get('quantityUnit', 'Nos')
    item.is_stock_item = 0
    item.sevi_item_id = item_data.get('id')
    item.sevi_vendor_id = item_data.get('vendorId')
    item.insert(ignore_permissions=True)
    
    # Add item price
    price = frappe.new_doc("Item Price")
    price.item_code = item.name
    price.price_list = "Standard Selling"
    price.price_list_rate = item_data.get('unitPrice')
    price.insert(ignore_permissions=True)
    
    return item.name

def sync_order_with_sevi(order_name, order_data):
    """
    Sync order with Sevi system using GraphQL
    """
    # Get Sevi API settings
    settings = frappe.get_doc("Sevi Settings")
    if not settings or not settings.enabled:
        frappe.throw("Sevi integration is not enabled or configured")
    
    # Prepare GraphQL mutation
    graphql_mutation = """
    mutation OrderCreate($input: OrderInput!) {
        orderCreate(input: $input) {
            order {
                id
                paymentLink
                referenceNumber
            }
            paymentOptions {
                method
                reference
                description
            }
        }
    }
    """
    
    # Prepare variables
    variables = {
        "input": {
            "billing": order_data.get('billing'),
            "items": order_data.get('items'),
            "shipping": order_data.get('shipping'),
            "paymentMethod": order_data.get('paymentMethod', 'INSTALLMENTS_MPESA'),
            "platformReference": order_name,
            "vendorId": order_data.get('vendorId'),
            "customerId": order_data.get('customerId'),
            "returnURL": get_url() + "/api/method/your_app.api.order.payment_callback"
        }
    }
    
    # Make request to Sevi
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}"
    }
    
    response = requests.post(
        settings.api_url,
        json={"query": graphql_mutation, "variables": variables},
        headers=headers
    )
    
    if response.status_code != 200:
        frappe.throw(f"Sevi API request failed: {response.text}")
        
    data = response.json()
    
    if 'errors' in data:
        frappe.throw(f"Sevi API error: {data['errors'][0]['message']}")
    
    return {
        'orderId': data['data']['orderCreate']['order']['id'],
        'paymentLink': data['data']['orderCreate']['order']['paymentLink']
    }

def generate_qr_code(payment_link):
    """
    Generate QR code from payment link
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(payment_link)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

def attach_qr_to_order(order_name, qr_base64):
    """
    Attach QR code to order as a file
    """
    file = frappe.new_doc("File")
    file.file_name = f"{order_name}_payment_qr.png"
    file.content = qr_base64
    file.attached_to_doctype = "Sales Order"
    file.attached_to_name = order_name
    file.insert(ignore_permissions=True)

@frappe.whitelist(allow_guest=True)
def payment_callback(reference, status):
    """
    Callback endpoint for Sevi payment notifications
    """
    try:
        # Find order by platformReference
        order = frappe.get_doc("Sales Order", {"po_no": reference})
        if not order:
            frappe.log_error(f"Order not found for reference: {reference}")
            return {"status": "error", "message": "Order not found"}
            
        # Update payment status
        if status.lower() == "paid":
            order.db_set("sevi_payment_status", "Paid")
            frappe.db.commit()
            
            # Create payment entry
            create_payment_entry(order)
            
        return {"status": "success"}
        
    except Exception as e:
        frappe.log_error(f"Payment callback failed: {str(e)}")
        return {"status": "error", "message": str(e)}

def create_payment_entry(order):
    """
    Create Payment Entry for paid order
    """
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.posting_date = now_datetime().date()
    pe.mode_of_payment = "Sevi Payment"
    pe.party_type = "Customer"
    pe.party = order.customer
    pe.paid_amount = order.grand_total
    pe.received_amount = order.grand_total
    pe.reference_no = order.sevi_order_id
    pe.reference_date = now_datetime().date()
    
    pe.append("references", {
        "reference_doctype": "Sales Order",
        "reference_name": order.name,
        "allocated_amount": order.grand_total
    })
    
    pe.insert(ignore_permissions=True)
    pe.submit()
    
    return pe

@frappe.whitelist()
def mark_order_delivered(order_name):
    """
    Mark order as delivered in ERPNext and notify Sevi
    """
    try:
        # Update ERPNext order
        order = frappe.get_doc("Sales Order", order_name)
        order.db_set("status", "Delivered")
        order.db_set("per_delivered", 100)
        
        # Notify Sevi
        settings = frappe.get_doc("Sevi Settings")
        if not settings or not settings.enabled:
            return {"status": "success", "message": "ERPNext updated but Sevi not notified (integration disabled)"}
            
        graphql_mutation = """
        mutation OrderDeliver($orderId: String!) {
            orderDeliver(orderId: $orderId) {
                order {
                    id
                    status
                }
            }
        }
        """
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.api_key}"
        }
        
        response = requests.post(
            settings.api_url,
            json={"query": graphql_mutation, "variables": {"orderId": order.sevi_order_id}},
            headers=headers
        )
        
        if response.status_code != 200:
            frappe.log_error(f"Sevi delivery notification failed: {response.text}")
            return {"status": "warning", "message": "ERPNext updated but Sevi notification failed"}
            
        return {"status": "success"}
        
    except Exception as e:
        frappe.log_error(f"Mark order delivered failed: {str(e)}")
        return {"status": "error", "message": str(e)}
```

### 2. Create a DocType for Sevi Settings

Create a new DocType "Sevi Settings" at `/apps/your_app/your_app/doctype/sevi_settings/sevi_settings.json`:

```json
{
  "name": "Sevi Settings",
  "doctype": "DocType",
  "module": "Your App",
  "is_single": 1,
  "autoname": "Sevi Settings",
  "fields": [
    {
      "fieldname": "enabled",
      "label": "Enabled",
      "fieldtype": "Check"
    },
    {
      "fieldname": "api_url",
      "label": "API URL",
      "fieldtype": "Data",
      "default": "https://api.sevi.io/graphql"
    },
    {
      "fieldname": "api_key",
      "label": "API Key",
      "fieldtype": "Password"
    },
    {
      "fieldname": "default_vendor_id",
      "label": "Default Vendor ID",
      "fieldtype": "Data"
    }
  ]
}
```

### 3. Add custom fields to Sales Order

Add these custom fields to the Sales Order DocType:

1. `sevi_order_id` - Data
2. `sevi_vendor_id` - Data
3. `sevi_payment_method` - Select (options: INSTALLMENTS, INSTALLMENTS_MPESA, SEVI_WALLET, CASH_ON_DELIVERY)
4. `sevi_payment_status` - Select (options: Pending, Paid, Partially Paid, Refunded)

### 4. Create a hook for order submission

Add to `/apps/your_app/your_app/hooks.py`:

```python
def on_submit(doc, method):
    if doc.doctype == "Sales Order" and not doc.sevi_order_id:
        # This is a new order created in ERPNext that needs to sync to Sevi
        order_data = {
            "billing": {
                "firstName": doc.customer_name,
                "phoneNumber": frappe.db.get_value("Customer", doc.customer, "mobile_no"),
                "address1": get_primary_address(doc.customer).address_line1,
                "city": get_primary_address(doc.customer).city,
                "country": get_primary_address(doc.customer).country
            },
            "items": [{
                "name": item.item_name,
                "description": item.description,
                "quantity": item.qty,
                "unitPrice": item.rate,
                "quantityUnit": item.uom
            } for item in doc.items],
            "vendorId": doc.sevi_vendor_id or frappe.db.get_single_value("Sevi Settings", "default_vendor_id"),
            "paymentMethod": doc.sevi_payment_method or "INSTALLMENTS_MPESA",
            "platformReference": doc.name
        }
        
        frappe.enqueue(
            "your_app.api.order.create_and_sync_order",
            order_data=order_data,
            queue="short"
        )

def get_primary_address(customer):
    addresses = frappe.get_all("Dynamic Link", 
        filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
        fields=["parent"]
    )
    if addresses:
        return frappe.get_doc("Address", addresses[0].parent)
    return None
```

### 5. Create a Payment Gateway for Sevi

Create a new Payment Gateway DocType to handle Sevi payments:

```python
from frappe.utils import get_url

def get_payment_url(**kwargs):
    """Return payment URL for Sevi payment gateway"""
    order_id = kwargs.get('order_id')
    amount = kwargs.get('amount')
    
    # Call Sevi API to get payment link
    settings = frappe.get_doc("Sevi Settings")
    graphql_mutation = """
    mutation PaymentRequest($input: PaymentRequestInput!) {
        paymentRequest(input: $input) {
            paymentLink
        }
    }
    """
    
    variables = {
        "input": {
            "method": "KES_MPESA_EXPRESS",
            "referenceNumber": order_id
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}"
    }
    
    response = requests.post(
        settings.api_url,
        json={"query": graphql_mutation, "variables": variables},
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        return data['data']['paymentRequest']['paymentLink']
    else:
        frappe.throw("Failed to get payment link from Sevi")
```

## Usage Examples

### 1. Creating an order from Sevi (webhook)

```python
@frappe.whitelist(allow_guest=True)
def sevi_order_webhook(data):
    """Webhook endpoint for Sevi to create orders in ERPNext"""
    try:
        data = json.loads(data)
        result = create_and_sync_order(data)
        return result
    except Exception as e:
        frappe.log_error(f"Sevi webhook failed: {str(e)}")
        return {"status": "error", "message": str(e)}
```

### 2. Creating an order from ERPNext UI

```javascript
// Client-side script to call the API
frappe.call({
    method: "your_app.api.order.create_and_sync_order",
    args: {
        order_data: {
            "customerId": "SEVI_CUSTOMER_ID",
            "billing": {
                "firstName": "John",
                "lastName": "Doe",
                "phoneNumber": "+254712345678",
                "address1": "123 Main St",
                "city": "Nairobi",
                "country": "KE"
            },
            "items": [
                {
                    "name": "Product A",
                    "description": "Sample product",
                    "quantity": 2,
                    "unitPrice": 1000,
                    "quantityUnit": "Nos"
                }
            ],
            "vendorId": "SEVI_VENDOR_ID",
            "paymentMethod": "INSTALLMENTS_MPESA"
        }
    },
    callback: function(response) {
        if (response.message.status === "success") {
            frappe.msgprint(__("Order created and synced with Sevi"));
            // Show QR code if available
            if (response.message.payment_link) {
                show_payment_qr(response.message.payment_link);
            }
        } else {
            frappe.msgprint(__("Error: ") + response.message.message);
        }
    }
});
```

### 3. Marking an order as delivered

```javascript
frappe.call({
    method: "your_app.api.order.mark_order_delivered",
    args: {
        order_name: "SO-00001"
    },
    callback: function(response) {
        frappe.msgprint(__("Order marked as delivered"));
    }
});
```

## Key Features Implemented

1. **Order Sync**:
   - Creates orders in ERPNext when they come from Sevi (via webhook)
   - Syncs orders created in ERPNext to Sevi (via GraphQL API)
   - Updates delivery status in both systems

2. **Payment Handling**:
   - Generates payment links from Sevi
   - Creates QR codes for payment links
   - Handles payment callbacks from Sevi

3. **Customer and Item Management**:
   - Automatically creates customers and items in ERPNext if they don't exist
   - Maps Sevi IDs to ERPNext records for future reference

4. **Error Handling**:
   - Comprehensive error logging and user feedback
   - Queue-based processing for better performance

This implementation provides a complete two-way synchronization between ERPNext and Sevi systems while handling all the requirements you specified, including order creation, delivery status updates, and payment processing with QR codes.