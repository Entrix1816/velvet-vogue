# models.py
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy.dialects.postgresql import ARRAY, JSON
from decimal import Decimal

db = SQLAlchemy()


# -------------------- DATABASE MODELS --------------------
class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    phone = db.Column(db.String(20), nullable=True)
    address = db.Column(db.Text, nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)

    # Relationships
    orders = db.relationship('Order', backref='user', lazy=True)
    cart_items = db.relationship('Cart', backref='user', lazy=True)


class Category(db.Model):
    __tablename__ = 'categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    products = db.relationship('Product', backref='category_ref', lazy=True)


class Product(db.Model):
    __tablename__ = 'products'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)

    # Foreign key to categories table
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True)
    category = db.relationship('Category', overlaps="category_ref,products")

    # Size-based inventory
    sizes = db.Column(JSON, default=dict)  # Format: {"S": 5, "M": 10, "L": 5, "XL": 2}

    # Total stock (calculated from sizes)
    stock = db.Column(db.Integer, default=0, nullable=False)

    sold_count = db.Column(db.Integer, default=0)
    image_urls = db.Column(ARRAY(db.String(500)), default=list)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def calculate_total_stock(self):
        """Calculate total stock from sizes"""
        if self.sizes and isinstance(self.sizes, dict):
            return sum(self.sizes.values())
        return 0

    @property
    def in_stock(self):
        return self.stock > 0

    @property
    def stock_status(self):
        if self.stock == 0:
            return "sold out"
        elif self.stock < 5:
            return f"only {self.stock} left"
        else:
            return "in stock"

    @property
    def available_sizes(self):
        """Return list of sizes with available stock"""
        if not self.sizes:
            return []
        return [size for size, qty in self.sizes.items() if qty > 0]

    def check_size_availability(self, size, quantity=1):
        """Check if specific size has enough stock"""
        if not self.sizes or size not in self.sizes:
            return False, "Size not available"
        if self.sizes[size] >= quantity:
            return True, "Available"
        return False, f"Only {self.sizes[size]} available in size {size}"

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'price': float(self.price),
            'category': self.category.name if self.category else None,
            'category_id': self.category_id,
            'sizes': self.sizes,
            'stock': self.stock,
            'available_sizes': self.available_sizes,
            'sold_count': self.sold_count,
            'image_urls': self.image_urls,
            'in_stock': self.in_stock
        }


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Customer information (store directly in order)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_email = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    shipping_address = db.Column(db.Text, nullable=False)

    # Order financials
    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    delivery_fee = db.Column(db.Numeric(10, 2), default=Decimal(2500.00))
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)

    # Payment details
    payment_method = db.Column(db.String(50), nullable=False)
    payment_status = db.Column(db.String(20), default='pending')
    transaction_ref = db.Column(db.String(100), unique=True, nullable=True)

    # Delivery
    delivery_status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')

    @property
    def order_number(self):
        """Generate order number from ID with padding"""
        return f"VV{str(self.id).zfill(4)}"


class OrderItem(db.Model):
    __tablename__ = 'order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id', ondelete='CASCADE'))
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'))
    size = db.Column(db.String(10))  # Store the size purchased
    quantity = db.Column(db.Integer, default=1)
    price = db.Column(db.Numeric(10, 2))

    # Relationships
    product = db.relationship('Product')


class Cart(db.Model):
    __tablename__ = 'carts'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'))
    size = db.Column(db.String(10))  # Store the size selected
    quantity = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    product = db.relationship('Product')


class FailedEmail(db.Model):
    __tablename__ = 'failed_emails'

    id = db.Column(db.Integer, primary_key=True)
    email_type = db.Column(db.String(50), nullable=False)  # 'order_confirmation', 'delivery_confirmation', etc.
    recipient = db.Column(db.String(100), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    html_content = db.Column(db.Text, nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    attempts = db.Column(db.Integer, default=0)
    max_attempts = db.Column(db.Integer, default=5)
    last_attempt = db.Column(db.DateTime, default=datetime.utcnow)
    next_attempt = db.Column(db.DateTime, default=datetime.utcnow)
    error_message = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'sending', 'sent', 'failed'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    order = db.relationship('Order', backref='failed_emails')