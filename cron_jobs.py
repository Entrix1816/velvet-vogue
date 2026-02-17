# cron_jobs.py - Run this every 5-10 minutes via a scheduler
from app import app, db
from email_service import EmailService

def retry_emails_job():
    """Run this every 5 minutes via cron or task scheduler"""
    with app.app_context():
        email_service = EmailService()
        stats = email_service.retry_failed_emails()
        print(f"Email retry job completed: {stats}")

if __name__ == '__main__':
    retry_emails_job()