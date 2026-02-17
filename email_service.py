import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Tuple, Dict, Any, Optional, List
from models import db, FailedEmail

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EmailService:
    def __init__(self):
        self.smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
        self.smtp_port = int(os.getenv('SMTP_PORT', 587))
        self.sender_email = os.getenv('SMTP_EMAIL')
        self.sender_password = os.getenv('SMTP_PASSWORD')
        self.admin_email = 'blessedsubarashi22@gmail.com'
        self.site_url = os.getenv('SITE_URL', 'http://127.0.0.1:5000')

        # Track email stats in memory (optional)
        self.email_log = []

    def send_email(self, to_email: str, subject: str, html_content: str,
                   email_type: str = 'general', order_id: Optional[int] = None) -> Tuple[bool, str]:
        """
        Try to send email immediately. If it fails, queue it for retry.
        """
        try:
            # Try to send immediately
            success, message = self._smtp_send(to_email, subject, html_content)

            if success:
                logger.info(f"✅ Email sent immediately to {to_email} - {email_type}")

                # Log success
                self.email_log.append({
                    'type': email_type,
                    'to': to_email,
                    'order_id': order_id,
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                })

                return True, "Email sent successfully"
            else:
                # If immediate send fails, queue it
                logger.warning(f"⚠️ Immediate send failed for {to_email}, queueing for retry: {message}")
                return self._queue_email(to_email, subject, html_content, email_type, order_id, message)

        except Exception as e:
            # Queue on any exception
            logger.error(f"❌ Error sending email to {to_email}: {str(e)}")
            return self._queue_email(to_email, subject, html_content, email_type, order_id, str(e))

    def _smtp_send(self, to_email: str, subject: str, html_content: str) -> Tuple[bool, str]:
        """Actual SMTP sending logic with detailed error handling"""
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['From'] = self.sender_email
            msg['To'] = to_email
            msg['Subject'] = subject

            # Attach HTML part
            html_part = MIMEText(html_content, 'html')
            msg.attach(html_part)

            # Send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.send_message(msg)

            return True, "Sent successfully"

        except smtplib.SMTPAuthenticationError as e:
            error_msg = f"SMTP Authentication failed - check email credentials: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

        except smtplib.SMTPException as e:
            error_msg = f"SMTP Error: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

        except ConnectionRefusedError:
            error_msg = f"Connection refused to {self.smtp_server}:{self.smtp_port}"
            logger.error(error_msg)
            return False, error_msg

        except TimeoutError:
            error_msg = "Connection timeout - server may be down"
            logger.error(error_msg)
            return False, error_msg

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def _queue_email(self, to_email: str, subject: str, html_content: str,
                     email_type: str, order_id: Optional[int], error_msg: str) -> Tuple[bool, str]:
        """Queue failed email for retry in database"""
        try:
            # Check if this email is already queued for this order
            existing = FailedEmail.query.filter_by(
                recipient=to_email,
                email_type=email_type,
                order_id=order_id,
                status='pending'
            ).first()

            if existing:
                # Update existing record
                existing.attempts += 1
                existing.last_attempt = datetime.utcnow()
                existing.error_message = error_msg
                existing.next_attempt = datetime.utcnow() + timedelta(minutes=5 * (2 ** existing.attempts))
                logger.info(f"📧 Updated existing queued email for {to_email}")
            else:
                # Create new failed email record
                failed_email = FailedEmail(
                    email_type=email_type,
                    recipient=to_email,
                    subject=subject,
                    html_content=html_content,
                    order_id=order_id,
                    error_message=error_msg,
                    attempts=1,
                    status='pending',
                    last_attempt=datetime.utcnow(),
                    next_attempt=datetime.utcnow() + timedelta(minutes=5)  # Retry in 5 minutes
                )
                db.session.add(failed_email)
                logger.info(f"📧 Email queued for retry: {to_email} - {email_type}")

            db.session.commit()

            # Log to memory
            self.email_log.append({
                'type': email_type,
                'to': to_email,
                'order_id': order_id,
                'success': False,
                'queued': True,
                'error': error_msg,
                'timestamp': datetime.utcnow().isoformat()
            })

            return False, f"Email queued for retry. Will attempt again later. (Error: {error_msg})"

        except Exception as e:
            logger.error(f"❌ Failed to queue email: {str(e)}")
            return False, f"Critical error: Could not send or queue email - {str(e)}"

    def retry_failed_emails(self, max_retries: int = 5) -> Dict[str, int]:
        """
        Retry all pending failed emails.
        Call this via a cron job or background task every 5-10 minutes.
        """
        stats = {
            'processed': 0,
            'sent': 0,
            'failed': 0,
            'permanent_failures': 0,
            'skipped': 0
        }

        try:
            # Get all pending emails that are due for retry
            pending = FailedEmail.query.filter(
                FailedEmail.status == 'pending',
                FailedEmail.attempts < FailedEmail.max_attempts,
                FailedEmail.next_attempt <= datetime.utcnow()
            ).all()

            logger.info(f"📧 Found {len(pending)} emails due for retry")

            for email in pending:
                stats['processed'] += 1
                email.attempts += 1
                email.last_attempt = datetime.utcnow()
                email.status = 'sending'
                db.session.commit()

                try:
                    # Attempt to send
                    success, message = self._smtp_send(email.recipient, email.subject, email.html_content)

                    if success:
                        # Email sent successfully - delete from queue
                        db.session.delete(email)
                        db.session.commit()
                        stats['sent'] += 1
                        logger.info(f"✅ Retry successful for email {email.id} to {email.recipient}")
                    else:
                        # Failed again - update for next retry
                        email.status = 'pending'
                        email.error_message = message

                        if email.attempts >= email.max_attempts:
                            email.status = 'failed'
                            stats['permanent_failures'] += 1
                            logger.error(
                                f"❌ Email {email.id} to {email.recipient} failed permanently after {email.attempts} attempts")
                        else:
                            # Exponential backoff: 5min, 25min, 2hr, 12hr, 24hr
                            backoff_minutes = 5 * (5 ** (email.attempts - 1))
                            email.next_attempt = datetime.utcnow() + timedelta(minutes=backoff_minutes)
                            stats['failed'] += 1
                            logger.info(f"⏳ Email {email.id} scheduled for retry in {backoff_minutes} minutes")

                        db.session.commit()

                except Exception as e:
                    email.status = 'pending'
                    email.error_message = str(e)
                    db.session.commit()
                    stats['failed'] += 1
                    logger.error(f"❌ Error during retry for email {email.id}: {str(e)}")

            return stats

        except Exception as e:
            logger.error(f"❌ Error in retry_failed_emails: {str(e)}")
            return stats

    def get_queue_stats(self) -> Dict[str, Any]:
        """Get statistics about the email queue"""
        try:
            total = FailedEmail.query.count()
            pending = FailedEmail.query.filter_by(status='pending').count()
            sending = FailedEmail.query.filter_by(status='sending').count()
            failed = FailedEmail.query.filter_by(status='failed').count()

            # Get oldest pending email
            oldest = FailedEmail.query.filter_by(status='pending').order_by(FailedEmail.created_at).first()

            return {
                'total': total,
                'pending': pending,
                'sending': sending,
                'failed': failed,
                'oldest_pending': oldest.created_at.isoformat() if oldest else None,
                'memory_log_count': len(self.email_log)
            }
        except Exception as e:
            logger.error(f"Error getting queue stats: {e}")
            return {'error': str(e)}

    def get_failed_emails(self, limit: int = 100) -> List[FailedEmail]:
        """Get list of failed emails for display"""
        return FailedEmail.query.order_by(FailedEmail.created_at.desc()).limit(limit).all()

    def clear_sent_emails(self, days: int = 7):
        """Clear successfully sent emails older than specified days"""
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            # Delete successful emails that are no longer pending
            deleted = FailedEmail.query.filter(
                FailedEmail.status == 'sent',
                FailedEmail.created_at < cutoff
            ).delete()
            db.session.commit()
            logger.info(f"🧹 Cleared {deleted} old sent emails")
            return deleted
        except Exception as e:
            logger.error(f"Error clearing old emails: {e}")
            return 0

    # Convenience methods for different email types
    def send_order_confirmation(self, order, order_items):
        """Send order confirmation to customer"""
        subject = f"Your Velvet Vogue Order Confirmation #{order.order_number}"
        html = self._build_order_email(order, order_items)
        return self.send_email(
            to_email=order.customer_email,
            subject=subject,
            html_content=html,
            email_type='order_confirmation',
            order_id=order.id
        )

    def send_admin_notification(self, order, order_items):
        """Send order notification to admin"""
        subject = f"🛍️ NEW ORDER #{order.order_number} - ₦{float(order.total_amount):,.0f}"
        html = self._build_admin_email(order, order_items)
        return self.send_email(
            to_email=self.admin_email,
            subject=subject,
            html_content=html,
            email_type='admin_notification',
            order_id=order.id
        )

    def send_delivery_confirmation(self, order):
        """Send delivery confirmation email to customer"""
        subject = f"Your Velvet Vogue Order #{order.order_number} Has Been Delivered! 🎉"
        html = self._build_delivery_email(order)
        return self.send_email(
            to_email=order.customer_email,
            subject=subject,
            html_content=html,
            email_type='delivery_confirmation',
            order_id=order.id
        )

    # Email builders (your existing HTML templates)
    def _build_order_email(self, order, order_items):
        """Build order confirmation email HTML"""
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: #f9f9f9; margin: 0; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 30px; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.1); }}
                .header {{ background: #0c0a0b; padding: 30px; text-align: center; }}
                .logo {{ font-size: 28px; color: #ffd2e6; letter-spacing: 4px; }}
                .logo span {{ color: #c88eaa; }}
                .content {{ padding: 30px; }}
                .order-details {{ background: #f5f5f5; border-radius: 20px; padding: 20px; margin: 20px 0; }}
                .item {{ display: flex; padding: 15px 0; border-bottom: 1px solid #e0e0e0; }}
                .item:last-child {{ border-bottom: none; }}
                .item-info {{ flex: 1; }}
                .item-name {{ font-weight: 600; margin-bottom: 5px; }}
                .item-size {{ color: #666; font-size: 14px; }}
                .item-price {{ font-weight: 600; color: #c88eaa; }}
                .total {{ font-size: 20px; text-align: right; margin-top: 20px; padding-top: 20px; border-top: 2px solid #c88eaa; }}
                .footer {{ text-align: center; padding: 20px; color: #999; font-size: 14px; }}
                .button {{ background: #c88eaa; color: black; text-decoration: none; padding: 12px 30px; border-radius: 30px; display: inline-block; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">VELVET<span>VOGUE</span></div>
                </div>
                <div class="content">
                    <h2 style="margin-top: 0;">Thank You for Your Order!</h2>
                    <p>Hello <strong>{order.customer_name}</strong>,</p>
                    <p>Your order <strong>#{order.order_number}</strong> has been confirmed and is being processed.</p>
                    <div class="order-details">
                        <h3 style="margin-top: 0;">Order Summary</h3>
                        {self._render_order_items(order_items)}
                        <div class="total">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 10px;">
                                <span>Subtotal:</span>
                                <span>₦{float(order.subtotal):,.0f}</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-bottom: 10px;">
                                <span>Delivery:</span>
                                <span>₦{float(order.delivery_fee):,.0f}</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; font-size: 24px; color: #c88eaa;">
                                <span><strong>TOTAL:</strong></span>
                                <span><strong>₦{float(order.total_amount):,.0f}</strong></span>
                            </div>
                        </div>
                    </div>
                    <div style="margin: 30px 0;">
                        <h3>Delivery Address</h3>
                        <p style="background: #f5f5f5; padding: 15px; border-radius: 15px;">
                            {order.shipping_address}<br>
                            <strong>Phone:</strong> {order.customer_phone}
                        </p>
                    </div>
                    <div style="margin: 30px 0;">
                        <h3>Payment Method</h3>
                        <p style="background: #f5f5f5; padding: 15px; border-radius: 15px;">
                            {order.payment_method}
                        </p>
                    </div>
                    <div style="text-align: center;">
                        <a href="{self.site_url}/account/orders" class="button">VIEW YOUR ORDERS</a>
                    </div>
                </div>
                <div class="footer">
                    <p>Questions? Contact us at hello@velvetvogue.ng</p>
                    <p>© 2026 Velvet Vogue. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

    def _build_admin_email(self, order, order_items):
        """Build admin notification email HTML"""
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: #f9f9f9; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 30px; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.1); }}
                .header {{ background: #c88eaa; padding: 20px; text-align: center; }}
                .header h2 {{ margin: 0; color: black; }}
                .content {{ padding: 30px; }}
                .customer-info {{ background: #f0f0f0; border-radius: 15px; padding: 20px; margin-bottom: 20px; }}
                .customer-info p {{ margin: 5px 0; }}
                .items-table {{ width: 100%; border-collapse: collapse; }}
                .items-table th {{ text-align: left; padding: 10px; background: #e0e0e0; }}
                .items-table td {{ padding: 15px 10px; border-bottom: 1px solid #e0e0e0; }}
                .total-row {{ font-size: 18px; font-weight: bold; background: #f8f0f5; }}
                .status {{ display: inline-block; padding: 5px 15px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
                .status.paid {{ background: #c8e6c9; color: #2e7d32; }}
                .status.pending {{ background: #fff3e0; color: #ef6c00; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>🛍️ NEW ORDER RECEIVED</h2>
                </div>
                <div class="content">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                        <h3 style="margin: 0;">Order #{order.order_number}</h3>
                        <span class="status {'paid' if order.payment_status == 'paid' else 'pending'}">
                            {order.payment_status.upper()}
                        </span>
                    </div>
                    <div class="customer-info">
                        <h4 style="margin-top: 0;">Customer Details</h4>
                        <p><strong>Name:</strong> {order.customer_name}</p>
                        <p><strong>Email:</strong> {order.customer_email}</p>
                        <p><strong>Phone:</strong> {order.customer_phone}</p>
                        <p><strong>Address:</strong> {order.shipping_address}</p>
                    </div>
                    <h4>Order Items</h4>
                    <table class="items-table">
                        <thead>
                            <tr>
                                <th>Product</th>
                                <th>Size</th>
                                <th>Qty</th>
                                <th>Price</th>
                                <th>Total</th>
                            </tr>
                        </thead>
                        <tbody>
                            {self._render_admin_items(order_items)}
                        </tbody>
                        <tfoot>
                            <tr>
                                <td colspan="4" style="text-align: right;"><strong>Subtotal:</strong></td>
                                <td><strong>₦{float(order.subtotal):,.0f}</strong></td>
                            </tr>
                            <tr>
                                <td colspan="4" style="text-align: right;"><strong>Delivery:</strong></td>
                                <td><strong>₦{float(order.delivery_fee):,.0f}</strong></td>
                            </tr>
                            <tr class="total-row">
                                <td colspan="4" style="text-align: right;"><strong>TOTAL:</strong></td>
                                <td><strong>₦{float(order.total_amount):,.0f}</strong></td>
                            </tr>
                        </tfoot>
                    </table>
                    <p style="margin-top: 20px; color: #666;">
                        <strong>Payment Method:</strong> {order.payment_method}<br>
                        <strong>Transaction Ref:</strong> {order.transaction_ref or 'N/A'}
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

    def _build_delivery_email(self, order):
        """Build delivery confirmation email HTML"""
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: #f9f9f9; margin: 0; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 30px; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.1); }}
                .header {{ background: #c88eaa; padding: 30px; text-align: center; }}
                .header h1 {{ margin: 0; color: black; font-size: 28px; }}
                .content {{ padding: 30px; }}
                .delivery-icon {{ font-size: 60px; text-align: center; margin-bottom: 20px; }}
                .order-details {{ background: #f5f5f5; border-radius: 20px; padding: 20px; margin: 20px 0; }}
                .footer {{ text-align: center; padding: 20px; color: #999; font-size: 14px; }}
                .button {{ background: #c88eaa; color: black; text-decoration: none; padding: 12px 30px; border-radius: 30px; display: inline-block; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>✨ DELIVERED! ✨</h1>
                </div>
                <div class="content">
                    <div class="delivery-icon">📦</div>
                    <h2 style="text-align: center;">Good news {order.customer_name}!</h2>
                    <p style="text-align: center; font-size: 18px;">Your order <strong>#{order.order_number}</strong> has been delivered.</p>
                    <div class="order-details">
                        <h3 style="margin-top: 0;">Order Summary</h3>
                        {self._render_order_items(order.items)}
                        <p style="text-align: right; font-size: 18px; margin-top: 20px;">
                            <strong>Total: ₦{float(order.total_amount):,.0f}</strong>
                        </p>
                    </div>
                    <p>We hope you love your new pieces! If you have any questions, feel free to reply to this email.</p>
                    <div style="text-align: center;">
                        <a href="{self.site_url}/account/orders" class="button">VIEW ORDER DETAILS</a>
                    </div>
                </div>
                <div class="footer">
                    <p>Thank you for shopping with Velvet Vogue!</p>
                    <p>© 2026 Velvet Vogue. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

    def _render_order_items(self, items):
        """Render order items HTML for customer emails"""
        html = ""
        for item in items:
            product = item.product
            html += f"""
            <div class="item">
                <div class="item-info">
                    <div class="item-name">{product.name}</div>
                    <div class="item-size">Size: {item.size} | Quantity: {item.quantity}</div>
                </div>
                <div class="item-price">₦{float(item.price) * item.quantity:,.0f}</div>
            </div>
            """
        return html

    def _render_admin_items(self, items):
        """Render order items HTML for admin emails"""
        html = ""
        for item in items:
            product = item.product
            total = float(item.price) * item.quantity
            html += f"""
            <tr>
                <td>{product.name}</td>
                <td>{item.size}</td>
                <td>{item.quantity}</td>
                <td>₦{float(item.price):,.0f}</td>
                <td>₦{total:,.0f}</td>
            </tr>
            """
        return html