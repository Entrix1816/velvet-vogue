from contextlib import contextmanager
import logging
import os
from datetime import datetime
import uuid
from werkzeug.security import check_password_hash, generate_password_hash
import secrets
from email_service import EmailService
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, abort
from sqlalchemy.dialects.postgresql import ARRAY, JSON
from sqlalchemy import and_
from werkzeug.utils import secure_filename
from models import db, User, Category, Product, Order, OrderItem, Cart, FailedEmail

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# -------------------- CONFIGURATION FROM ENV --------------------
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = os.getenv('SQLALCHEMY_TRACK_MODIFICATIONS', 'False').lower() == 'true'
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', 'static/uploads')
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_UPLOAD_SIZE_MB', 16)) * 1024 * 1024
app.config['FLASK_ENV'] = os.getenv('FLASK_ENV', 'production')
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
app.config['PAYSTACK_PUBLIC_KEY'] = os.getenv('PAYSTACK_PUBLIC_KEY')
app.config['PAYSTACK_SECRET_KEY'] = os.getenv('PAYSTACK_SECRET_KEY')

# Email configuration
app.config['SMTP_SERVER'] = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
app.config['SMTP_PORT'] = int(os.getenv('SMTP_PORT', 587))
app.config['SMTP_EMAIL'] = os.getenv('SMTP_EMAIL')
app.config['SMTP_PASSWORD'] = os.getenv('SMTP_PASSWORD')
app.config['SITE_URL'] = os.getenv('SITE_URL', 'http://127.0.0.1:5000')

# Add these with your other config
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}

#admin configuration
app.config['ADMIN_EMAIL'] = os.getenv('ADMIN_EMAIL')
app.config['ADMIN_PASSWORD_HASH']= os.getenv('ADMIN_PASSWORD_HASH')
# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


db.init_app(app)
with app.app_context():
    try:
        db.create_all()
        print("✅ Database tables created")
    except Exception as e:
        print("❌ DB creation failed:", e)


# After db.init_app(app)
@app.teardown_appcontext
def shutdown_session(exception=None):
    """Automatically remove database sessions at the end of each request"""
    if exception:
        db.session.rollback()
    db.session.remove()

# -------------------- CART HELPER FUNCTIONS --------------------
def get_cart():
    """Get current cart from session with size information"""
    return session.get('cart', {})


def save_cart(cart):
    """Save cart to session"""
    session['cart'] = cart
    session.modified = True


def calculate_cart_total(cart):
    """Calculate total price of items in cart"""
    total = 0
    for item in cart.values():
        total += item['price'] * item['quantity']
    return total


def check_stock_availability(product_id, size, quantity):
    """Check if requested quantity for specific size is available"""
    product = db.session.get(Product, product_id)
    if not product:
        return False, "Product not found"
    return product.check_size_availability(size, quantity)


# -------------------- PUBLIC ROUTES --------------------
@app.route('/')
def home():
    """Homepage with product grid"""
    products = Product.query.filter(Product.stock > 0).order_by(Product.created_at.desc()).limit(8).all()
    categories = Category.query.all()
    return render_template('index.html', products=products, categories=categories)


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """Product detail page"""
    product = db.session.get(Product, product_id)
    if not product:
        abort(404)
    related_products = Product.query.filter(
        Product.category_id == product.category_id,
        Product.id != product_id,
        Product.stock > 0
    ).limit(4).all()
    return render_template('product_detail.html', product=product, related=related_products)


@app.route('/collection')
def collection():
    """Collection page with all products and filters"""
    products = Product.query.filter(Product.stock > 0).order_by(Product.created_at.desc()).all()
    categories = Category.query.all()

    # Get min and max prices for filter range
    min_price = db.session.query(db.func.min(Product.price)).scalar() or 0
    max_price = db.session.query(db.func.max(Product.price)).scalar() or 100000

    return render_template('collection.html',
                           products=products,
                           categories=categories,
                           min_price=float(min_price),
                           max_price=float(max_price))


@app.route('/api/collection/filter', methods=['POST'])
def filter_collection():
    """API endpoint for filtering products"""
    try:
        data = request.get_json()
        category_ids = data.get('categories', [])
        min_price = float(data.get('minPrice', 0))
        max_price = float(data.get('maxPrice', 9999999))
        sizes = data.get('sizes', [])

        # Start query
        query = Product.query.filter(Product.stock > 0)

        # Filter by category
        if category_ids and len(category_ids) > 0:
            query = query.filter(Product.category_id.in_(category_ids))

        # Filter by price
        query = query.filter(and_(Product.price >= min_price, Product.price <= max_price))

        # Filter by size availability
        if sizes and len(sizes) > 0:
            size_filters = []
            for size in sizes:
                size_filters.append(
                    db.cast(Product.sizes[size], db.Integer) > 0
                )
            if size_filters:
                query = query.filter(db.or_(*size_filters))

        products = query.all()

        return jsonify({
            'success': True,
            'products': [p.to_dict() for p in products]
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/category/<int:category_id>')
def category_products(category_id):
    """Filter products by category"""
    category = db.session.get(Category, category_id)
    if not category:
        abort(404)
    products = Product.query.filter_by(category_id=category_id).filter(Product.stock > 0).all()
    categories = Category.query.all()
    return render_template('collection.html', products=products, categories=categories, active_category=category_id)


# -------------------- CART API ROUTES --------------------
@app.route('/api/cart/add', methods=['POST'])
def add_to_cart():
    """Add item to cart with size and stock validation"""
    try:
        data = request.get_json()
        product_id = str(data.get('product_id'))
        size = data.get('size')
        quantity = int(data.get('quantity', 1))

        if not size:
            return jsonify({'success': False, 'error': 'Please select a size'}), 400

        # Check stock for specific size
        product = db.session.get(Product, int(product_id))
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        # Validate size exists and has stock
        available, message = product.check_size_availability(size, quantity)
        if not available:
            return jsonify({'success': False, 'error': message}), 400

        # Get current cart
        cart = get_cart()

        # Create unique key for product+size combination
        cart_key = f"{product_id}_{size}"

        # Calculate current quantity in cart for this size
        current_in_cart = cart.get(cart_key, {}).get('quantity', 0)
        requested_total = current_in_cart + quantity

        # Validate against available stock for this size
        available, message = product.check_size_availability(size, requested_total)
        if not available:
            return jsonify({
                'success': False,
                'error': message,
                'available': product.sizes.get(size, 0)
            }), 400

        # Update cart
        if cart_key in cart:
            cart[cart_key]['quantity'] = requested_total
        else:
            cart[cart_key] = {
                'product_id': int(product_id),
                'name': product.name,
                'price': float(product.price),
                'size': size,
                'quantity': quantity,
                'image': product.image_urls[0] if product.image_urls else None,
                'max_stock': product.sizes.get(size, 0)
            }

        save_cart(cart)

        # Calculate totals
        cart_total = calculate_cart_total(cart)
        item_count = sum(item['quantity'] for item in cart.values())

        return jsonify({
            'success': True,
            'message': f'{product.name} (Size {size}) added to cart',
            'cart_count': item_count,
            'cart_total': cart_total,
            'item_quantity': requested_total,
            'remaining_stock': product.sizes.get(size, 0) - requested_total
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/cart/update', methods=['POST'])
def update_cart():
    """Update item quantity in cart"""
    try:
        data = request.get_json()
        cart_key = data.get('cart_key')  # Format: product_id_size
        quantity = int(data.get('quantity'))

        if quantity < 0:
            return jsonify({'success': False, 'error': 'Invalid quantity'}), 400

        cart = get_cart()

        if cart_key not in cart:
            return jsonify({'success': False, 'error': 'Item not in cart'}), 404

        item = cart[cart_key]

        if quantity == 0:
            # Remove item
            del cart[cart_key]
            message = 'Item removed from cart'
        else:
            # Check stock for this size
            product = db.session.get(Product, item['product_id'])
            if not product:
                return jsonify({'success': False, 'error': 'Product not found'}), 404

            available, message = product.check_size_availability(item['size'], quantity)
            if not available:
                return jsonify({
                    'success': False,
                    'error': message,
                    'max_allowed': product.sizes.get(item['size'], 0)
                }), 400

            # Update quantity
            item['quantity'] = quantity
            message = 'Cart updated'

        save_cart(cart)

        # Recalculate totals
        cart_total = calculate_cart_total(cart)
        item_count = sum(item['quantity'] for item in cart.values())

        return jsonify({
            'success': True,
            'message': message,
            'cart_count': item_count,
            'cart_total': cart_total,
            'item_quantity': quantity if quantity > 0 else 0
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/cart/remove', methods=['POST'])
def remove_from_cart():
    """Remove item from cart"""
    try:
        data = request.get_json()
        cart_key = data.get('cart_key')

        cart = get_cart()

        if cart_key in cart:
            del cart[cart_key]
            save_cart(cart)

            cart_total = calculate_cart_total(cart)
            item_count = sum(item['quantity'] for item in cart.values())

            return jsonify({
                'success': True,
                'message': 'Item removed',
                'cart_count': item_count,
                'cart_total': cart_total
            })

        return jsonify({'success': False, 'error': 'Item not in cart'}), 404

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/clear-cart')
def clear_cart():
    """Temporary route to clear cart session"""
    session.pop('cart', None)
    return "Cart cleared! You can now go back to the homepage."


@app.route('/api/cart')
def get_cart_api():
    """Get current cart contents"""
    cart = get_cart()
    cart_total = calculate_cart_total(cart)
    item_count = sum(item['quantity'] for item in cart.values())

    return jsonify({
        'success': True,
        'cart': cart,
        'total': cart_total,
        'count': item_count
    })


@app.route('/cart')
def view_cart():
    """View cart page - handles both old and new cart formats"""
    cart = get_cart()
    cart_items = []
    cart_total = 0

    for cart_key, item in cart.items():
        # Handle both old and new formats
        if isinstance(item, dict):
            # Try to get product_id - could be in different formats
            product_id = item.get('product_id')

            # If no product_id, try to extract from cart_key (old format might have just the ID as key)
            if not product_id and cart_key.isdigit():
                product_id = int(cart_key)

            if product_id:
                product = db.session.get(Product, product_id)
                if product:
                    # Get size - default to 'OS' if not present
                    size = item.get('size', 'OS')

                    # Get quantity - default to 1 if not present
                    quantity = item.get('quantity', 1)

                    # Get price - use product price if not in item
                    price = item.get('price', float(product.price))

                    subtotal = price * quantity
                    cart_total += subtotal

                    cart_items.append({
                        'cart_key': cart_key,
                        'product_id': product_id,
                        'id': product_id,  # Add id for backward compatibility
                        'name': item.get('name', product.name),
                        'price': price,
                        'size': size,
                        'quantity': quantity,
                        'image': item.get('image', product.image_urls[0] if product.image_urls else None),
                        'subtotal': subtotal,
                        'max_stock': product.sizes.get(size, 0) if product.sizes else product.stock,
                        'stock_status': product.stock_status
                    })

    return render_template('cart.html', cart_items=cart_items, total=cart_total)


@app.route('/api/products/<int:product_id>')
def get_product(product_id):
    """Get single product details"""
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'success': False, 'error': 'Product not found'}), 404
    return jsonify(product.to_dict())


@app.route('/checkout', methods=['GET'])
def checkout_page():
    """Render checkout page"""
    # Get cart from session
    cart = get_cart()

    if not cart:
        flash('Your cart is empty', 'error')
        return redirect(url_for('view_cart'))

    # Calculate totals
    cart_items = []
    subtotal = 0

    for cart_key, item in cart.items():
        product = db.session.get(Product, item['product_id'])
        if product:
            item_total = float(item['price']) * item['quantity']
            subtotal += item_total
            cart_items.append({
                'cart_key': cart_key,
                'product_id': item['product_id'],
                'id': item['product_id'],  # Add id for backward compatibility
                'name': item['name'],
                'price': item['price'],
                'size': item['size'],
                'quantity': item['quantity'],
                'image': item.get('image')
            })

    delivery_fee = 2500
    total = subtotal + delivery_fee

    # Debug print to verify key is being passed
    print(f"🔑 Paystack Public Key: {app.config['PAYSTACK_PUBLIC_KEY']}")

    return render_template('checkout.html',
                           cart_items=cart_items,
                           subtotal=subtotal,
                           delivery_fee=delivery_fee,
                           total=total,
                           paystack_public_key=app.config['PAYSTACK_PUBLIC_KEY'])




@app.route('/admin/email-queue-data')
def email_queue_data():
    """Get email queue data for admin dashboard"""
    try:
        email_service = EmailService()
        stats = email_service.get_queue_stats()
        failed_emails = email_service.get_failed_emails(limit=50)

        # Convert failed emails to dict for JSON
        emails_data = []
        for email in failed_emails:
            emails_data.append({
                'id': email.id,
                'email_type': email.email_type,
                'recipient': email.recipient,
                'attempts': email.attempts,
                'max_attempts': email.max_attempts,
                'error_message': email.error_message,
                'status': email.status,
                'next_attempt': email.next_attempt.isoformat() if email.next_attempt else None,
                'created_at': email.created_at.isoformat() if email.created_at else None
            })

        return jsonify({
            'stats': stats,
            'emails': emails_data
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/retry-emails', methods=['POST'])
def retry_failed_emails():
    """Manually trigger email retry"""
    try:
        email_service = EmailService()
        stats = email_service.retry_failed_emails()

        flash(
            f"📧 Retry complete: {stats['sent']} sent, {stats['failed']} failed, "
            f"{stats['permanent_failures']} permanent failures",
            'info'
        )
    except Exception as e:
        flash(f'Error retrying emails: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='email-queue'))


# -------------------- ADMIN ROUTES --------------------
@app.route('/admin-login')
def admin_login_page():
    """Render admin login page"""
    return render_template('admin_login.html')


@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """API endpoint for admin login"""
    try:
        data = request.get_json()
        email = data.get('email', '').lower()
        password = data.get('password', '')

        # Check against environment variables
        if email != os.getenv('ADMIN_EMAIL', '').lower():
            return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

        # Get the stored hash from .env
        stored_hash = os.getenv('ADMIN_PASSWORD_HASH', '')

        # Check password against hash
        if not stored_hash or not check_password_hash(stored_hash, password):
            return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

        # Generate a simple token
        token = secrets.token_hex(16)

        # Store admin session
        session['admin_logged_in'] = True
        session['admin_token'] = token

        return jsonify({
            'success': True,
            'token': token,
            'message': 'Login successful'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    """Logout admin"""
    session.pop('admin_logged_in', None)
    session.pop('admin_token', None)
    return jsonify({'success': True})

@app.route('/admin')
def admin_panel():
    """Main admin dashboard"""
    # Get stats for dashboard
    total_products = Product.query.count()
    total_orders = Order.query.count()
    pending_deliveries = Order.query.filter_by(delivery_status='pending').count()

    # Calculate revenue from paid orders
    paid_orders = Order.query.filter_by(payment_status='paid').all()
    revenue = sum(float(order.total_amount or 0) for order in paid_orders)

    products = Product.query.all()
    orders = Order.query.all()
    users = User.query.all()
    categories = Category.query.all()

    # Create email service instance for template
    email_service = EmailService()

    return render_template(
        'admin.html',
        total_products=total_products,
        total_orders=total_orders,
        pending_deliveries=pending_deliveries,
        revenue=revenue,
        products=products,
        orders=orders,
        customers=users,
        categories=categories,
        email_service=email_service  # Add this
    )


@app.route('/admin/add-category', methods=['POST'])
def add_category():
    """Add new category"""
    try:
        name = request.form.get('name')
        if name:
            # Check if category already exists
            existing = Category.query.filter_by(name=name.lower()).first()
            if existing:
                flash(f'Category "{name}" already exists', 'error')
            else:
                category = Category(name=name.lower())
                db.session.add(category)
                db.session.commit()
                flash(f'Category "{name}" added', 'success')
    except Exception as e:
        flash(f'Error adding category: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/add-product', methods=['POST'])
def add_product():
    """Handle new product submission with size-based inventory and multiple images"""
    try:
        # Get form data
        name = request.form.get('name')
        price = float(request.form.get('price'))
        category_id = request.form.get('category')
        description = request.form.get('description')

        # Get size quantities
        sizes = {}
        size_keys = ['XS', 'S', 'M', 'L', 'XL', 'XXL']
        for size in size_keys:
            qty = request.form.get(f'size_{size}')
            if qty and int(qty) > 0:
                sizes[size] = int(qty)

        if not sizes:
            flash('Please add at least one size with quantity', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        # Calculate total stock
        total_stock = sum(sizes.values())

        # Get category
        category = db.session.get(Category, category_id)
        if not category:
            flash('Please select a valid category', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        # Handle multiple image uploads
        image_urls = []
        if 'images[]' in request.files:
            files = request.files.getlist('images[]')
            for file in files:
                if file and file.filename:
                    # Generate unique filename
                    filename = secure_filename(file.filename)
                    ext = filename.split('.')[-1]
                    unique_name = f"{uuid.uuid4().hex}.{ext}"

                    # Save file
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
                    file.save(file_path)

                    # Store relative path for database
                    image_urls.append(f"/static/uploads/{unique_name}")

        # Create new product
        product = Product(
            name=name,
            price=price,
            category_id=category_id,
            description=description,
            sizes=sizes,
            stock=total_stock,
            image_urls=image_urls
        )

        db.session.add(product)
        db.session.commit()

        flash(f'Product "{name}" added with {total_stock} units across {len(sizes)} sizes', 'success')

    except Exception as e:
        flash(f'Error adding product: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/edit-product/<int:product_id>', methods=['POST'])
def edit_product(product_id):
    """Edit existing product"""
    try:
        product = db.session.get(Product, product_id)
        if not product:
            flash('Product not found', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        product.name = request.form.get('name', product.name)
        product.price = float(request.form.get('price', product.price))
        product.description = request.form.get('description', product.description)

        # Update category if provided
        category_id = request.form.get('category')
        if category_id:
            category = db.session.get(Category, category_id)
            if category:
                product.category_id = category_id

        # Update size quantities
        sizes = {}
        size_keys = ['XS', 'S', 'M', 'L', 'XL', 'XXL']
        for size in size_keys:
            qty = request.form.get(f'size_{size}')
            if qty and int(qty) >= 0:
                sizes[size] = int(qty)

        if sizes:
            product.sizes = sizes
            product.stock = sum(sizes.values())

        # Handle new images if uploaded
        if 'images[]' in request.files:
            files = request.files.getlist('images[]')
            new_images = []
            for file in files:
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    ext = filename.split('.')[-1]
                    unique_name = f"{uuid.uuid4().hex}.{ext}"
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
                    file.save(file_path)
                    new_images.append(f"/static/uploads/{unique_name}")

            # Append new images to existing ones
            if new_images:
                product.image_urls = product.image_urls + new_images

        db.session.commit()
        flash('Product updated successfully', 'success')

    except Exception as e:
        flash(f'Error updating product: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/delete-category/<int:category_id>', methods=['DELETE'])
def delete_category(category_id):
    try:
        category = db.session.get(Category, category_id)
        if not category:
            return jsonify({'success': False, 'error': 'Category not found'}), 404
        # Check if category has products
        if category.products:
            return jsonify({'success': False, 'error': 'Category has products'}), 400
        db.session.delete(category)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/admin/update-stock/<int:product_id>', methods=['POST'])
def update_stock(product_id):
    """Update product stock from admin"""
    try:
        product = db.session.get(Product, product_id)
        if not product:
            flash('Product not found', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        # Update size quantities
        sizes = {}
        size_keys = ['XS', 'S', 'M', 'L', 'XL', 'XXL']
        for size in size_keys:
            qty = request.form.get(f'size_{size}')
            if qty is not None:
                sizes[size] = int(qty)

        if sizes:
            product.sizes = sizes
            product.stock = sum(sizes.values())
            db.session.commit()
            flash(f'Stock updated for {product.name}', 'success')
        else:
            flash('No size data provided', 'error')

    except Exception as e:
        flash(f'Error updating stock: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/delete-product/<int:product_id>', methods=['DELETE', 'POST'])
def delete_product(product_id):
    """Delete product"""
    try:
        product = db.session.get(Product, product_id)
        if not product:
            if request.method == 'DELETE':
                return jsonify({'success': False, 'error': 'Product not found'}), 404
            flash('Product not found', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        # First, check if product has any order items (foreign key constraint)
        order_items_count = OrderItem.query.filter_by(product_id=product_id).count()

        if order_items_count > 0:
            if request.method == 'DELETE':
                return jsonify({'success': False, 'error': 'Cannot delete product that has been ordered'}), 400
            flash('Cannot delete product that has been ordered', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        # Delete image files from server
        for img_url in product.image_urls:
            if img_url.startswith('/static/uploads/'):
                file_path = img_url.replace('/static/uploads/', '')
                full_path = os.path.join(app.config['UPLOAD_FOLDER'], file_path)
                if os.path.exists(full_path):
                    os.remove(full_path)

        db.session.delete(product)
        db.session.commit()

        if request.method == 'DELETE':
            return jsonify({'success': True})

        flash('Product deleted successfully', 'success')

    except Exception as e:
        db.session.rollback()
        if request.method == 'DELETE':
            return jsonify({'success': False, 'error': str(e)}), 400
        flash(f'Error deleting product: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/update-order/<int:order_id>', methods=['POST'])
def update_order_status(order_id):
    """Update order delivery status and send confirmation email"""
    try:
        order = Order.query.get_or_404(order_id)
        order.delivery_status = request.form.get('status', 'delivered')

        # If this is a Pay on Delivery order, also mark payment as paid
        if order.payment_method == 'Pay on Delivery':
            order.payment_status = 'paid'
            flash('✅ Order marked as delivered and payment status updated to paid', 'success')

        db.session.commit()

        # Send delivery confirmation email
        try:
            email_service = EmailService()
            # You'll need to add this method to EmailService
            email_service.send_delivery_confirmation(order)
            if order.payment_method != 'Pay on Delivery':
                flash('✅ Order marked as delivered and email sent to customer', 'success')
        except Exception as email_error:
            print(f"Email error: {email_error}")
            flash('⚠️ Order marked as delivered but email failed to send', 'warning')

    except Exception as e:
        db.session.rollback()
        flash(f'Error updating order: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='orders'))


@app.route('/admin/update-payment/<int:order_id>', methods=['POST'])
def update_payment_status(order_id):
    """Update payment status"""
    order = db.session.get(Order, order_id)
    if not order:
        flash('Order not found', 'error')
        return redirect(url_for('admin_panel', _anchor='orders'))
    order.payment_status = request.form.get('status', 'paid')
    db.session.commit()
    flash('Payment status updated', 'success')
    return redirect(url_for('admin_panel', _anchor='orders'))


# -------------------- API ROUTES (for dynamic frontend) --------------------
@app.route('/api/products')
def api_products():
    """Return all products as JSON"""
    products = Product.query.all()
    return jsonify([p.to_dict() for p in products])


@app.route('/api/orders')
def api_orders():
    """Return all orders as JSON"""
    orders = Order.query.all()
    return jsonify([{
        'id': o.id,
        'order_number': o.order_number,
        'customer_name': o.customer_name,
        'amount': float(o.total_amount) if o.total_amount else 0,
        'payment_status': o.payment_status.upper() if o.payment_status else 'PENDING',
        'delivery_status': o.delivery_status.upper() if o.delivery_status else 'PENDING'
    } for o in orders])


@app.route('/api/cart/sync', methods=['POST'])
def sync_cart():
    """Sync local cart with server (for logged-in users)"""
    try:
        data = request.get_json()
        local_cart = data.get('cart', [])

        # Here you would sync with database cart for logged-in users
        # For now, just return success

        return jsonify({
            'success': True,
            'message': 'Cart synced'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/checkout', methods=['POST'])
def api_checkout():
    """Process checkout, create order, send emails"""
    try:
        data = request.get_json()

        # Validate required fields
        required = ['customer', 'items', 'subtotal', 'delivery_fee', 'total', 'payment_method']
        for field in required:
            if field not in data:
                return jsonify({'success': False, 'error': f'Missing field: {field}'}), 400

        # Create order
        order = Order(
            customer_name=data['customer']['name'],
            customer_email=data['customer']['email'].lower(),
            customer_phone=data['customer']['phone'],
            shipping_address=data['customer']['address'],
            subtotal=data['subtotal'],
            delivery_fee=data['delivery_fee'],
            total_amount=data['total'],
            payment_method=data['payment_method'],
            payment_status=data.get('payment_status', 'pending'),
            transaction_ref=data.get('payment_reference')
        )

        db.session.add(order)
        db.session.flush()  # Get order ID

        # Create order items - handle both id and product_id
        for item in data['items']:
            # Try to get product_id from either field
            product_id = item.get('product_id') or item.get('id')
            if not product_id:
                return jsonify({'success': False, 'error': 'Item missing product_id'}), 400

            product = db.session.get(Product, product_id)
            if not product:
                return jsonify({'success': False, 'error': f'Product not found: {product_id}'}), 400

            order_item = OrderItem(
                order_id=order.id,
                product_id=product_id,
                size=item['size'],
                quantity=item['quantity'],
                price=item['price']
            )
            db.session.add(order_item)

            # Update stock ONLY if payment is confirmed (paid)
            if data.get('payment_status') == 'paid':
                sizes = dict(product.sizes)
                current_stock = sizes.get(item['size'], 0)
                sizes[item['size']] = current_stock - item['quantity']
                product.sizes = sizes
                product.stock = sum(sizes.values())
                product.sold_count += item['quantity']

        db.session.commit()

        # Send emails
        email_service = EmailService()

        # To customer
        email_service.send_order_confirmation(order, order.items)

        # To admin
        email_service.send_admin_notification(order, order.items)

        return jsonify({
            'success': True,
            'order_number': order.order_number,
            'message': 'Order created successfully'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Checkout error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/register', methods=['POST'])
def api_register():
    """Register a new user from checkout"""
    try:
        data = request.get_json()

        # Check if user exists
        existing = User.query.filter_by(email=data['email'].lower()).first()
        if existing:
            return jsonify({'success': False, 'error': 'Email already registered'}), 400

        # Create user
        user = User(
            name=data['name'],
            email=data['email'].lower(),
            password_hash=generate_password_hash(data['password']),
            phone=data.get('phone'),
            address=data.get('address'),
            city=data.get('city'),
            state=data.get('state')
        )

        db.session.add(user)
        db.session.commit()

        return jsonify({'success': True, 'message': 'User registered successfully'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400


# -------------------- INITIALIZATION --------------------
@app.cli.command("init-db")
def init_db():
    """Initialize database with sample data"""
    db.create_all()

    # Add default categories if none exist
    if Category.query.count() == 0:
        default_categories = ['dresses', 'skirts', 'tops', 'bottoms', 'accessories']
        for cat_name in default_categories:
            category = Category(name=cat_name)
            db.session.add(category)
        db.session.commit()
        print("✅ Categories added")

    # Add sample users if none exist
    if User.query.count() == 0:
        from werkzeug.security import generate_password_hash
        sample_users = [
            User(name="amara okonkwo", email="amara.o@email.com",
                 password_hash=generate_password_hash("password123")),
            User(name="zara ibrahim", email="zara.i@email.com",
                 password_hash=generate_password_hash("password123")),
            User(name="chidi nwosu", email="chidi.n@email.com",
                 password_hash=generate_password_hash("password123")),
        ]
        db.session.add_all(sample_users)
        db.session.commit()
        print("✅ Users added")

    # Add sample products with size-based inventory if none exist
    if Product.query.count() == 0:
        categories = Category.query.all()
        if categories:
            sample_products = [
                Product(
                    name="noir drape dress",
                    price=45900,
                    category_id=categories[0].id,
                    description="Elegant black drape dress for evening occasions. Features a flowing silhouette that moves with you.",
                    sizes={"XS": 3, "S": 8, "M": 12, "L": 7, "XL": 4},
                    stock=34,
                    sold_count=23,
                    image_urls=["/static/uploads/sample-dress-1.jpg", "/static/uploads/sample-dress-2.jpg"]
                ),
                Product(
                    name="satin slip skirt",
                    price=28900,
                    category_id=categories[1].id,
                    description="Luxurious satin skirt with subtle shine. Perfect for both day and night looks.",
                    sizes={"XS": 5, "S": 10, "M": 15, "L": 8, "XL": 2},
                    stock=40,
                    sold_count=12,
                    image_urls=["/static/uploads/sample-skirt-1.jpg", "/static/uploads/sample-skirt-2.jpg"]
                ),
                Product(
                    name="velvet corset top",
                    price=32700,
                    category_id=categories[2].id,
                    description="Sumptuous velvet corset with satin ribbons. Adjustable straps for perfect fit.",
                    sizes={"S": 5, "M": 8, "L": 3, "XL": 1},
                    stock=17,
                    sold_count=7,
                    image_urls=["/static/uploads/sample-corset-1.jpg"]
                ),
                Product(
                    name="leather harness",
                    price=19900,
                    category_id=categories[4].id,
                    description="Genuine leather harness with rose-gold hardware. Adjustable for all sizes.",
                    sizes={"One Size": 12},
                    stock=12,
                    sold_count=15,
                    image_urls=["/static/uploads/sample-harness-1.jpg"]
                ),
            ]
            db.session.add_all(sample_products)
            db.session.commit()
            print("✅ Products added")

    # Add sample orders
    if Order.query.count() == 0:
        users = User.query.all()
        products = Product.query.all()
        if users and products:
            sample_orders = [
                Order(
                    user_id=users[0].id,
                    customer_name=users[0].name,
                    customer_email=users[0].email,
                    customer_phone="08012345678",
                    shipping_address="123 Test St, Lagos",
                    subtotal=124500,
                    delivery_fee=2500,
                    total_amount=127000,
                    payment_method="card",
                    payment_status="paid",
                    delivery_status="pending"
                ),
                Order(
                    user_id=users[1].id,
                    customer_name=users[1].name,
                    customer_email=users[1].email,
                    customer_phone="08087654321",
                    shipping_address="456 Test Ave, Abuja",
                    subtotal=89200,
                    delivery_fee=2500,
                    total_amount=91700,
                    payment_method="transfer",
                    payment_status="paid",
                    delivery_status="delivered"
                ),
            ]
            db.session.add_all(sample_orders)
            db.session.commit()

            # Add order items with sizes
            orders = Order.query.all()
            for i, order in enumerate(orders):
                product = products[i % len(products)]
                # Get first available size
                size = list(product.sizes.keys())[0] if product.sizes else "M"
                item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    size=size,
                    quantity=2,
                    price=product.price
                )
                db.session.add(item)
            db.session.commit()
            print("✅ Orders added")

    print("✅ Database initialization complete!")

# Add this temporarily to see all routes
with app.app_context():
    print("\n=== ALL REGISTERED ROUTES ===")
    for rule in app.url_map.iter_rules():
        if 'order' in str(rule).lower():
            print(f"  {rule}")
    print("=============================\n")

if __name__ == '__main__':
    # Create tables if they don't exist
    with app.app_context():
        db.create_all()
    app.run(debug=app.config['DEBUG'])