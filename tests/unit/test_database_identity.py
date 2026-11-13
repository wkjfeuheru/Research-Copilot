"""Existing host identities survive URL adapter and credential changes."""

from research_copilot.runtime.host_binding import database_identity_url, digest


def test_database_identity_retains_existing_hash_and_excludes_credentials():
    expected = digest(["postgresql", "localhost", 5432, "research"])
    for url in (
        "postgresql://first:old@LOCALHOST/research",
        "postgresql+psycopg://second:new@localhost:5432/research?sslmode=require",
    ):
        assert database_identity_url(url) == expected
    assert database_identity_url("postgresql://localhost/other") != expected
    assert database_identity_url("postgresql://localhost:5433/research") != expected
