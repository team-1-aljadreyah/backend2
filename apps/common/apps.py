"""Common app configuration including SQLite connection pragmas."""
from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite_connection(sender, connection, **kwargs) -> None:
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.execute("PRAGMA busy_timeout = 30000;")


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"

    def ready(self) -> None:
        connection_created.connect(_configure_sqlite_connection, dispatch_uid="common_sqlite_pragmas")
