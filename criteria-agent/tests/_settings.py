"""Test settings."""

from app.core import config

TEST = config.Settings(
    openai_api_key="test",
    typesafe_api_key="test",
    mistral_api_key=None,
    langfuse_public_key=None,
    langfuse_secret_key=None,
    langfuse_host="",
    environment="test",
    database_schema="test",
    run_schema="test_run",
)


def settings() -> config.Settings:
    """The test settings."""
    return TEST


config.settings = settings
