import pytest

from comet.playback.base import Actionability, ProviderStatus, Readiness


def test_provider_status_preserves_readiness_and_actionability_separately():
    status = ProviderStatus(Readiness.UNKNOWN, Actionability.SERVER_ON_DEMAND)
    assert status.readiness is Readiness.UNKNOWN
    assert status.actionability is Actionability.SERVER_ON_DEMAND


@pytest.mark.parametrize(
    ("readiness", "actionability"),
    [
        (Readiness.TERMINAL_FAILURE, Actionability.SERVER_ON_DEMAND),
        (Readiness.REQUIRES_PREPARE, Actionability.SERVER_ON_DEMAND),
    ],
)
def test_provider_status_rejects_inconsistent_states(readiness, actionability):
    with pytest.raises(ValueError):
        ProviderStatus(readiness, actionability)


def test_provider_status_rejects_nonterminal_auth_failure():
    with pytest.raises(ValueError):
        ProviderStatus(Readiness.READY, Actionability.NONE, auth_failed=True)
