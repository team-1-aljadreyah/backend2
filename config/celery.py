"""Celery application configuration."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("alnahrian_erp")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "sweep-expired-reservations-every-5m": {
        "task": "notifications.sweep_expired_reservations",
        "schedule": crontab(minute="*/5"),
    },
    "sweep-expiry-alerts-daily": {
        "task": "notifications.sweep_expiry_alerts",
        "schedule": crontab(hour=5, minute=0),
    },
    "process-outbox-events-every-1m": {
        "task": "notifications.process_outbox_events",
        "schedule": crontab(minute="*"),
    },
    "cleanup-realtime-tickets-every-15m": {
        "task": "realtime.cleanup_expired_tickets",
        "schedule": crontab(minute="*/15"),
    },
    "reconcile-online-payments-every-5m": {
        "task": "payments.reconcile_online_payments",
        "schedule": crontab(minute="*/5"),
    },
}
