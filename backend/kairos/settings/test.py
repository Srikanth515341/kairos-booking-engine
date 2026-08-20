import dj_database_url

from .base import *  # noqa: F403

DEBUG = False

ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1"]

# A dedicated database (RFC v1.0 §4.1; infra/init-test-db.sql) so the test
# suite never touches development data.
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL_TEST",
        default="postgresql://kairos:kairos@localhost:5432/kairos_test",
    )
}
