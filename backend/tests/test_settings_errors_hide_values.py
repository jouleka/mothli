"""A refused setting stops startup with a message that names the setting. It must never carry a
value: the message goes to the server's log, and the settings hold keys and passwords.

Found by the deployment part (ADR 0041 part 9): the error printed a shortened copy of everything
the settings were read from, so a secret near either end could reach the log.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings

SECRET = "sk-live-THIS-MUST-NOT-APPEAR-0123456789abcdef"


def refused(**values):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **values)
    return caught.value


def test_a_refused_hosted_setting_names_what_is_missing_and_prints_no_value():
    # Hosted mode without its sign-in settings is refused. Breaks if the message shows any of
    # what was read: here a model key, a database address with a password, and a sign-in secret.
    error = refused(deployment_mode="hosted", trusted_hosts="mothli.example",
                    openai_api_key=SECRET,
                    hosted_tenant_dsn=f"postgresql://tenant:{SECRET}@db/mothli",
                    oidc_client_secret=SECRET)
    for text in (str(error), repr(error)):
        assert SECRET not in text and "THIS-MUST-NOT-APPEAR" not in text
    assert "hosted mode requires" in str(error)          # the owner can still see what to fix


def test_a_malformed_value_is_refused_without_being_echoed():
    # A value of the wrong kind is refused by the field itself. Breaks if the bad value is quoted
    # back: a secret pasted into the wrong line of the environment file would be printed.
    error = refused(cost_max_usd_per_book=SECRET)
    for text in (str(error), repr(error)):
        assert SECRET not in text and "THIS-MUST-NOT-APPEAR" not in text
    assert "cost_max_usd_per_book" in str(error)
