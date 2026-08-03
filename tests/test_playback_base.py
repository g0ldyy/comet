from comet.playback.base import Actionability, ProviderStatus, Readiness


def test_provider_status_preserves_readiness_and_actionability_separately():
    status = ProviderStatus(Readiness.UNKNOWN, Actionability.SERVER_ON_DEMAND)
    assert status.readiness is Readiness.UNKNOWN
    assert status.actionability is Actionability.SERVER_ON_DEMAND
