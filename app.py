from contextlib import contextmanager
from sqlalchemy.orm import joinedload
import logging
import cloudinary
import cloudinary.uploader
import os
from datetime import datetime, timedelta
import uuid
from werkzeug.security import check_password_hash, generate_password_hash
import secrets
from email_service import EmailService
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, abort
import cloudinary.api
from sqlalchemy import and_, func, or_
from werkzeug.utils import secure_filename
from models import db, User, Category, Product, Order, OrderItem, Cart, FailedEmail

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),    # Identifies your account
    api_key=os.getenv('CLOUDINARY_API_KEY'),           # Authenticates your app
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),     # Secret key for security
    secure=True                                         # Use HTTPS URLs
)

# -------------------- CONFIGURATION FROM ENV --------------------
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
database_url = os.getenv("DATABASE_URL")

if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# Add after your other configs
app.config['SESSION_COOKIE_SECURE'] = True  # Required for HTTPS on Render
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = os.getenv('SQLALCHEMY_TRACK_MODIFICATIONS', 'False').lower() == 'true'
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
app.config['SITE_URL'] = os.getenv('SITE_URL')

# Database pool configuration - optimized for high concurrency
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,  # Enable connection health checks
    'pool_recycle': 1800,   # Recycle connections after 30 minutes
    'connect_args': {
        'connect_timeout': 10
    }
}

# Only add pool size in production for better performance
if app.config['FLASK_ENV'] == 'production':
    app.config['SQLALCHEMY_ENGINE_OPTIONS'].update({
        'pool_size': 10,
        'max_overflow': 20,
        'pool_timeout': 30
    })

# Admin configuration
app.config['ADMIN_EMAIL'] = os.getenv('ADMIN_EMAIL')
app.config['ADMIN_PASSWORD_HASH'] = os.getenv('ADMIN_PASSWORD_HASH')

# Initialize database
db.init_app(app)

# -------------------- DATABASE SESSION MANAGEMENT --------------------
@app.teardown_appcontext
def shutdown_session(exception=None):
    """Automatically remove database sessions at the end of each request"""
    if exception:
        db.session.rollback()
        logger.error(f"Database session rolled back due to: {str(exception)}")
    db.session.remove()


@contextmanager
def db_session_management():
    """Context manager for safe database operations"""
    try:
        yield
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Database error: {str(e)}")
        raise
    finally:
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
    try:
        product = db.session.get(Product, product_id)
        if not product:
            return False, "Product not found"
        return product.check_size_availability(size, quantity)
    except Exception as e:
        logger.error(f"Stock check error: {str(e)}")
        return False, "Error checking stock"


# -------------------- PUBLIC ROUTES --------------------
@app.route('/')
def home():
    """Homepage with product grid"""
    try:
        products = Product.query.filter(Product.stock > 0)\
            .order_by(Product.created_at.desc())\
            .limit(8)\
            .all()
        categories = Category.query.limit(20).all()  # Limit categories for performance
        return render_template('index.html', products=products, categories=categories)
    except Exception as e:
        logger.error(f"Homepage error: {str(e)}")
        flash('Unable to load products. Please try again.', 'error')
        return render_template('index.html', products=[], categories=[])


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """Product detail page"""
    try:
        product = db.session.get(Product, product_id)
        if not product:
            abort(404)

        related_products = Product.query.filter(
            Product.category_id == product.category_id,
            Product.id != product_id,
            Product.stock > 0
        ).limit(4).all()

        return render_template('product_detail.html', product=product, related=related_products)
    except Exception as e:
        logger.error(f"Product detail error: {str(e)}")
        abort(500)


@app.route('/collection')
def collection():
    """Collection page with all products and filters"""
    try:
        # Use pagination for products
        page = request.args.get('page', 1, type=int)
        per_page = 24

        products_paginated = Product.query.filter(Product.stock > 0)\
            .order_by(Product.created_at.desc())\
            .paginate(page=page, per_page=per_page, error_out=False)

        categories = Category.query.limit(20).all()

        # Get min and max prices for filter range
        min_price = db.session.query(func.min(Product.price)).scalar() or 0
        max_price = db.session.query(func.max(Product.price)).scalar() or 100000

        return render_template('collection.html',
                               products=products_paginated.items,
                               pagination=products_paginated,
                               categories=categories,
                               min_price=float(min_price),
                               max_price=float(max_price))
    except Exception as e:
        logger.error(f"Collection page error: {str(e)}")
        flash('Unable to load collection. Please try again.', 'error')
        return render_template('collection.html', products=[], categories=[], min_price=0, max_price=0)


@app.route('/api/collection/filter', methods=['POST'])
def filter_collection():
    """API endpoint for filtering products"""
    try:
        data = request.get_json() or {}
        category_ids = data.get('categories', [])
        min_price = float(data.get('minPrice', 0))
        max_price = float(data.get('maxPrice', 9999999))
        sizes = data.get('sizes', [])
        page = data.get('page', 1)
        per_page = data.get('per_page', 24)

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
                    func.coalesce(
                        Product.sizes[size].astext.cast(db.Integer),
                        0
                    ) > 0
                )
            if size_filters:
                query = query.filter(or_(*size_filters))

        # Paginate results
        paginated = query.order_by(Product.created_at.desc())\
            .paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            'success': True,
            'products': [p.to_dict() for p in paginated.items],
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': paginated.page
        })

    except Exception as e:
        logger.error(f"Filter collection error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/category/<int:category_id>')
def category_products(category_id):
    """Filter products by category"""
    try:
        category = db.session.get(Category, category_id)
        if not category:
            abort(404)

        page = request.args.get('page', 1, type=int)
        per_page = 24

        products_paginated = Product.query.filter_by(category_id=category_id)\
            .filter(Product.stock > 0)\
            .order_by(Product.created_at.desc())\
            .paginate(page=page, per_page=per_page, error_out=False)

        categories = Category.query.limit(20).all()

        return render_template('collection.html',
                               products=products_paginated.items,
                               pagination=products_paginated,
                               categories=categories,
                               active_category=category_id)
    except Exception as e:
        logger.error(f"Category products error: {str(e)}")
        abort(500)


# -------------------- CART API ROUTES --------------------
@app.route('/api/cart/add', methods=['POST'])
def add_to_cart():
    """Add item to cart with size and stock validation"""
    try:
        data = request.get_json() or {}
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
        logger.error(f"Add to cart error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/cart/update', methods=['POST'])
def update_cart():
    """Update item quantity in cart"""
    try:
        data = request.get_json() or {}
        cart_key = data.get('cart_key')  # Format: product_id_size
        quantity = int(data.get('quantity', 0))

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
        logger.error(f"Update cart error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/cart/remove', methods=['POST'])
def remove_from_cart():
    """Remove item from cart"""
    try:
        data = request.get_json() or {}
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
        logger.error(f"Remove from cart error: {str(e)}")
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
    try:
        product = db.session.get(Product, product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        return jsonify(product.to_dict())
    except Exception as e:
        logger.error(f"Get product error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


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
        logger.error(f"Email queue data error: {str(e)}")
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
        logger.error(f"Retry emails error: {str(e)}")
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
        data = request.get_json() or {}
        email = data.get('email', '').lower()
        password = data.get('password', '')
        remember = data.get('remember', False)

        # Check against environment variables
        if email != os.getenv('ADMIN_EMAIL', '').lower():
            logger.warning(f"Failed login attempt for email: {email}")
            return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

        # Get the stored hash from .env
        stored_hash = os.getenv('ADMIN_PASSWORD_HASH', '')

        # Check password against hash
        if not stored_hash or not check_password_hash(stored_hash, password):
            logger.warning(f"Failed login attempt - invalid password for: {email}")
            return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

        # Generate a simple token
        token = secrets.token_hex(16)

        # Store admin session
        session['admin_logged_in'] = True
        session['admin_token'] = token

        # Set session lifetime based on remember me
        if remember:
            session.permanent = True
            # You can set a custom lifetime if needed
            # app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
        else:
            session.permanent = False

        logger.info(f"Admin login successful: {email}")

        return jsonify({
            'success': True,
            'token': token,
            'message': 'Login successful'
        })

    except Exception as e:
        logger.error(f"Admin login error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/admin/logout')
def admin_logout():
    """Logout admin and clear session"""
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('admin_login_page'))

@app.route('/api/admin/logout', methods=['POST'])
def admin_api_logout():
    """API endpoint for admin logout"""
    session.clear()
    return jsonify({'success': True})


@app.route('/admin')
def admin_panel():
    """Main admin dashboard"""
    if not session.get('admin_logged_in'):
        flash('Please login to access the admin panel', 'error')
        return redirect(url_for('admin_login_page'))

    try:
        # Get stats for dashboard using efficient queries
        total_products = db.session.query(func.count(Product.id)).scalar() or 0
        total_orders = db.session.query(func.count(Order.id)).scalar() or 0
        pending_deliveries = db.session.query(func.count(Order.id))\
            .filter_by(delivery_status='pending').scalar() or 0

        # Calculate revenue from paid orders
        revenue = db.session.query(func.sum(Order.total_amount))\
            .filter_by(payment_status='paid').scalar() or 0

        # Get paginated data for tables
        products_page = request.args.get('products_page', 1, type=int)
        orders_page = request.args.get('orders_page', 1, type=int)
        customers_page = request.args.get('customers_page', 1, type=int)

        per_page = 20

        products = Product.query.order_by(Product.created_at.desc())\
            .paginate(page=products_page, per_page=per_page, error_out=False)
        orders = Order.query.order_by(Order.created_at.desc())\
            .paginate(page=orders_page, per_page=per_page, error_out=False)
        users = User.query.order_by(User.created_at.desc())\
            .paginate(page=customers_page, per_page=per_page, error_out=False)
        categories = Category.query.limit(50).all()

        # Create email service instance for template
        email_service = EmailService()

        return render_template(
            'admin.html',
            total_products=total_products,
            total_orders=total_orders,
            pending_deliveries=pending_deliveries,
            revenue=float(revenue),
            products=products.items,
            products_pagination=products,
            orders=orders.items,
            orders_pagination=orders,
            customers=users.items,
            customers_pagination=users,
            categories=categories,
            email_service=email_service
        )
    except Exception as e:
        logger.error(f"Admin panel error: {str(e)}")
        flash('Error loading admin panel. Please try again.', 'error')
        return redirect(url_for('admin_login_page'))


@app.route('/admin/add-category', methods=['POST'])
def add_category():
    """Add new category"""
    try:
        name = request.form.get('name', '').strip()
        if not name:
            flash('Category name is required', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        # Check if category already exists
        existing = Category.query.filter(func.lower(Category.name) == name.lower()).first()
        if existing:
            flash(f'Category "{name}" already exists', 'error')
        else:
            category = Category(name=name.lower())
            db.session.add(category)
            db.session.commit()
            flash(f'Category "{name}" added', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Add category error: {str(e)}")
        flash(f'Error adding category: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/add-product', methods=['POST'])
def add_product():
    """Handle new product submission with size-based inventory and multiple images"""
    try:
        # Get form data
        name = request.form.get('name', '').strip()
        if not name:
            flash('Product name is required', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        price = float(request.form.get('price', 0))
        category_id = request.form.get('category')
        description = request.form.get('description', '')

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

        image_urls = []

        if 'images[]' in request.files:
            files = request.files.getlist('images[]')

            for file in files:
                if file and file.filename:
                    try:
                        allowed_extensions = {'png', 'jpg', 'gif', 'webp'}

                        filename = secure_filename(file.filename)
                        ext = filename.rslipt('.',1)[1].lower() if '.' in filename else ''

                        if ext not in allowed_extensions:
                            logger.warning(f"Skipping invalid filetype: {ext}")

                            continue


                        upload_result = cloudinary.uploader.upload(
                            file,
                            folder="velvet_vogue/products",
                            public_id=f"{uuid.uuid4().hex}",
                            resource_type="image",
                            overwrite=True,
                            quality="auto:best",
                            fetch_format="auto"
                        )
                        if upload_result and 'secure_url' in upload_result:
                            image_urls.append(upload_result['secure_url'])
                            logger.info(f"Uploaded: {upload_result['secure_url']}")
                        else:
                            logger.error(f"Upload result missing secure_url: {upload_error}")
                            flash(f"Image upload failed for {filename}", "warning")


                    except Exception as upload_error:
                        logger.error(f"Cloudinary upload failed: {str(upload_error)}")


                    if not image_urls:
                        image_urls = ['https://res.cloudinary.com/demo/image/upload/v1312461204/sample.jpg']
                        flash('Using placeholder image - no images uploaded', 'warning')


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
        db.session.rollback()
        logger.error(f"Add product error: {str(e)}")
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

        # Update basic fields
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

        if 'images[]' in request.files:
            files = request.files.getlist('images[]')
            new_images = []

            for file in files:
                if file and file.filename:
                    try:
                        upload_result = cloudinary.uploader.upload(
                            file,
                            folder="velvet_vogue/products",
                            public_id=f"{uuid.uuid4().hex}",
                            resource_type="image"
                        )

                        new_images.append(upload_result['secure_url'])

                    except Exception as upload_error:
                        logger.error(f"Cloudinary upload failed: {str(upload_error)}")

            if new_images:
                product.image_urls = (product.image_urls or []) + new_images

        db.session.commit()
        flash('Product updated successfully', 'success')

    except Exception as e:
        db.session.rollback()
        logger.error(f"Edit product error: {str(e)}")
        flash(f'Error updating product: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/delete-category/<int:category_id>', methods=['DELETE'])
def delete_category(category_id):
    try:
        category = db.session.get(Category, category_id)
        if not category:
            return jsonify({'success': False, 'error': 'Category not found'}), 404

        # Check if category has products
        product_count = db.session.query(func.count(Product.id))\
            .filter_by(category_id=category_id).scalar() or 0

        if product_count > 0:
            return jsonify({'success': False, 'error': 'Category has products'}), 400

        db.session.delete(category)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Delete category error: {str(e)}")
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
                try:
                    sizes[size] = int(qty)
                except ValueError:
                    sizes[size] = 0

        if sizes:
            product.sizes = sizes
            product.stock = sum(sizes.values())
            db.session.commit()
            flash(f'Stock updated for {product.name}', 'success')
        else:
            flash('No size data provided', 'error')

    except Exception as e:
        db.session.rollback()
        logger.error(f"Update stock error: {str(e)}")
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
        order_items_count = db.session.query(func.count(OrderItem.id))\
            .filter_by(product_id=product_id).scalar() or 0

        if order_items_count > 0:
            if request.method == 'DELETE':
                return jsonify({'success': False, 'error': 'Cannot delete product that has been ordered'}), 400
            flash('Cannot delete product that has been ordered', 'error')
            return redirect(url_for('admin_panel', _anchor='products'))

        import cloudinary.api

        for img_url in product.image_urls or []:
            try:
                # Extract public_id from URL
                public_id = img_url.split("/")[-1].split(".")[0]

                cloudinary.uploader.destroy(
                    f"velvet_vogue/products/{public_id}"
                )

            except Exception as e:
                logger.warning(f"Cloudinary deletion failed: {str(e)}")

        db.session.delete(product)
        db.session.commit()

        if request.method == 'DELETE':
            return jsonify({'success': True})

        flash('Product deleted successfully', 'success')

    except Exception as e:
        db.session.rollback()
        logger.error(f"Delete product error: {str(e)}")
        if request.method == 'DELETE':
            return jsonify({'success': False, 'error': str(e)}), 400
        flash(f'Error deleting product: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='products'))


@app.route('/admin/update-order/<int:order_id>', methods=['POST'])
def update_order_status(order_id):
    logger.info(f"Updating order {order_id}")  # Add this
    try:
        order = db.session.get(Order, order_id)
        if not order:
            logger.error(f"Order {order_id} not found")  # Add this
            flash('Order not found', 'error')
            return redirect(url_for('admin_panel', _anchor='orders'))

        new_status = request.form.get('status', 'delivered')
        logger.info(f"Setting order {order_id} status to {new_status}")  # Add this

        order.delivery_status = new_status

        if order.payment_method == 'Pay on Delivery':
            order.payment_status = 'paid'
            flash('✅ Order marked as delivered and payment status updated to paid', 'success')
        else:
            flash('✅ Order marked as delivered', 'success')

        db.session.commit()
        logger.info(f"Order {order_id} updated successfully")  # Add this

    except Exception as e:
        db.session.rollback()
        logger.error(f"Update order error: {str(e)}")  # Add this
        flash(f'Error updating order: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='orders'))



@app.route('/admin/update-payment/<int:order_id>', methods=['POST'])
def update_payment_status(order_id):
    """Update payment status"""
    try:
        order = db.session.get(Order, order_id)
        if not order:
            flash('Order not found', 'error')
            return redirect(url_for('admin_panel', _anchor='orders'))

        order.payment_status = request.form.get('status', 'paid')
        db.session.commit()
        flash('Payment status updated', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Update payment status error: {str(e)}")
        flash(f'Error updating payment status: {str(e)}', 'error')

    return redirect(url_for('admin_panel', _anchor='orders'))

@app.route('/debug/test-checkout', methods=['POST'])
def debug_test_checkout():
    """Test endpoint for checkout"""
    try:
        data = request.get_json()
        return jsonify({
            'success': True,
            'received_data': data,
            'message': 'Test successful'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/debug/product-images/<int:product_id>')
def debug_product_images(product_id):
    """Debug route to check stored image URLs"""
    if not session.get('admin_logged_in'):
        return "Unauthorized", 401

    product = db.session.get(Product, product_id)
    if not product:
        return "Product not found", 404

    return {
        'product_id': product.id,
        'name': product.name,
        'image_urls': product.image_urls,
        'image_count': len(product.image_urls) if product.image_urls else 0
    }

# -------------------- API ROUTES (for dynamic frontend) --------------------
@app.route('/api/products')
def api_products():
    """Return all products as JSON with pagination"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 50, type=int), 100)  # Limit max per page

        products = Product.query.order_by(Product.created_at.desc())\
            .paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            'success': True,
            'products': [p.to_dict() for p in products.items],
            'total': products.total,
            'pages': products.pages,
            'current_page': products.page
        })
    except Exception as e:
        logger.error(f"API products error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders')
def api_orders():
    """Return all orders as JSON with pagination"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 50, type=int), 100)

        orders = Order.query.order_by(Order.created_at.desc())\
            .paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            'success': True,
            'orders': [{
                'id': o.id,
                'order_number': o.order_number,
                'customer_name': o.customer_name,
                'amount': float(o.total_amount) if o.total_amount else 0,
                'payment_status': o.payment_status.upper() if o.payment_status else 'PENDING',
                'delivery_status': o.delivery_status.upper() if o.delivery_status else 'PENDING'
            } for o in orders.items],
            'total': orders.total,
            'pages': orders.pages,
            'current_page': orders.page
        })
    except Exception as e:
        logger.error(f"API orders error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/cart/sync', methods=['POST'])
def sync_cart():
    """Sync local cart with server (for logged-in users)"""
    try:
        data = request.get_json() or {}
        local_cart = data.get('cart', [])

        # Here you would sync with database cart for logged-in users
        # For now, just return success
        if not data:
            return jsonify({'success': False, 'error': 'Invalid request: no JSON'}), 400
        else:
            return jsonify({
                'success': True,
                'message': 'Cart synced'
            })
    except Exception as e:
        logger.error(f"Sync cart error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/checkout', methods=['POST'])
def api_checkout():
    """Process checkout, create order, update stock, and send emails"""
    try:
        data = request.get_json() or {}

        # Log received data for debugging
        logger.info(f"Checkout data received: {data}")
        logger.info(f"Session cart: {session.get('cart', {})}")

        # Required fields validation
        required = ['customer', 'items', 'subtotal', 'delivery_fee', 'total', 'payment_method']
        for field in required:
            if field not in data:
                logger.error(f"Missing field: {field}")
                return jsonify({'success': False, 'error': f'Missing field: {field}'}), 400

        # Validate items
        if not data['items']:
            return jsonify({'success': False, 'error': 'No items in order'}), 400

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
        db.session.flush()  # Ensure order.id is available
        logger.info(f"Order created with ID: {order.id}")

        # Create order items and handle stock update
        for item in data['items']:
            product_id = item.get('product_id') or item.get('id')
            if not product_id:
                logger.error(f"Item missing product_id: {item}")
                return jsonify({'success': False, 'error': 'Item missing product_id'}), 400

            product = db.session.get(Product, product_id)
            if not product:
                logger.error(f"Product not found: {product_id}")
                return jsonify({'success': False, 'error': f'Product not found: {product_id}'}), 400

            # Validate stock BEFORE committing
            available, msg = product.check_size_availability(item['size'], item['quantity'])
            if not available:
                logger.error(f"Stock validation failed: {msg}")
                return jsonify({'success': False, 'error': msg}), 400

            # Create order item
            order_item = OrderItem(
                order_id=order.id,
                product_id=product_id,
                size=item['size'],
                quantity=item['quantity'],
                price=item['price']
            )
            db.session.add(order_item)
            logger.info(f"Order item added: {product.name} x {item['quantity']}")

            # Only deduct stock if payment is confirmed
            if data.get('payment_status') == 'paid':
                sizes = dict(product.sizes)
                sizes[item['size']] -= item['quantity']
                product.sizes = sizes
                product.stock = sum(sizes.values())
                product.sold_count += item['quantity']
                logger.info(f"Stock updated for {product.name}")

        db.session.commit()
        logger.info(f"Order {order.order_number} committed to database")

        # Clear the cart from session after successful order
        session.pop('cart', None)
        logger.info("Cart cleared from session")

        # Send emails after commit
        try:
            email_service = EmailService()
            email_service.send_order_confirmation(order, order.items)  # Customer
            email_service.send_admin_notification(order, order.items)  # Admin
            logger.info("Confirmation emails sent")
        except Exception as email_err:
            logger.error(f"Email sending failed: {email_err}")
            # Don't fail the order if emails don't send

        return jsonify({
            'success': True,
            'order_number': order.order_number,
            'message': 'Order created successfully'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Checkout error: {str(e)}", exc_info=True)  # exc_info gives full traceback
        return jsonify({'success': False, 'error': str(e)}), 400




@app.route('/api/register', methods=['POST'])
def api_register():
    """Register a new user from checkout"""
    try:
        data = request.get_json() or {}

        # Validate required fields
        if not data.get('email') or not data.get('password'):
            return jsonify({'success': False, 'error': 'Email and password required'}), 400

        # Check if user exists
        existing = User.query.filter_by(email=data['email'].lower()).first()
        if existing:
            return jsonify({'success': False, 'error': 'Email already registered'}), 400

        # Create user
        user = User(
            name=data.get('name', ''),
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
        logger.error(f"Register error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/admin/verify-token', methods=['POST'])
def verify_admin_token():
    """Verify if admin token is valid"""
    try:
        # Get token from Authorization header
        auth_header = request.headers.get('Authorization', '')
        token = None

        if auth_header.startswith('Bearer '):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
        elif request.is_json:
            # Also check if token is in JSON body
            token = request.json.get('token')

        if not token:
            return jsonify({'valid': False, 'error': 'No token provided'}), 401

        # Check if token matches session
        if session.get('admin_logged_in') and session.get('admin_token') == token:
            return jsonify({'valid': True, 'message': 'Token is valid'})

        # If token doesn't match session, it's invalid
        return jsonify({'valid': False, 'error': 'Invalid token'}), 401

    except Exception as e:
        logger.error(f"Token verification error: {str(e)}")
        return jsonify({'valid': False, 'error': str(e)}), 400

# -------------------- INITIALIZATION --------------------
@app.cli.command("init-db")
def init_db():
    """Initialize database with sample data"""
    try:
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

    except Exception as e:
        db.session.rollback()
        print(f"❌ Database initialization failed: {str(e)}")
        raise

# Remove the @app.before_request function entirely
# Instead, initialize once when the app starts
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=app.config['DEBUG'])
