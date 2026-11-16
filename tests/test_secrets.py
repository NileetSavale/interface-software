"""Each bank's operator credentials are its own (D26): a tenant never gets another bank's, and a missing one fails
before anything runs."""
import asyncio

import pytest

from interface.artifact.templates import TemplateError, render, secrets_for
from interface.replay.executor import Replayer
from interface.replay.result import Failed
from tests.conftest import OPERATOR

SOURCE = {"APP_USER": "base-teller", "APP_USER__B": "b-teller"}


def test_a_tenant_reads_its_own_name_only():
    assert render("{{secrets.APP_USER}}", {}, secrets_for(SOURCE, "b")) == "b-teller"
    assert render("{{secrets.APP_USER}}", {}, secrets_for(SOURCE, None)) == "base-teller"
    # Bank C has nothing of its own: it fails, rather than borrowing the base operator
    with pytest.raises(TemplateError, match="APP_USER__C"):
        render("{{secrets.APP_USER}}", {}, secrets_for(SOURCE, "c"))
    assert list(secrets_for(SOURCE, "b")) == ["APP_USER"]


def test_a_bank_without_credentials_fails_before_the_browser_opens(base_url, tmp_path):
    from tests.test_handoff import artifact_without
    artifact = artifact_without(base_url)  # the documented example, approved for this app
    result = asyncio.run(Replayer(artifact, base_url, {"memberId": "12345"}, env=OPERATOR, tenant="b",
                                  pending_dir=tmp_path).run())
    assert isinstance(result, Failed) and result.error_class == "MISSING_SECRET", result
    assert result.at_step == "preflight" and "APP_USER__B" in result.observed and "teller01" not in result.observed
