"""State batching respects actual padded UTF-8 bytes as well as key counts."""

import asyncio

import pytest
from test_clu_shared_listener import Harness, keys

from custom_components.homeassistant_grenton.domain.api.clu import MAX_STATE_REQUEST_BYTES
from custom_components.homeassistant_grenton.domain.api.clu_messages import GrentonCluApiClientRegisterRequest
from custom_components.homeassistant_grenton.state import GrentonCluStateVariableKey


def test_long_unicode_keys_split_before_thirty_and_remain_under_encrypted_budget():
    api = Harness().api
    requested = [GrentonCluStateVariableKey("ą" * 45 + str(index)) for index in range(31)]
    batches = api.state_read_batches([*requested, requested[0]])
    assert [key for batch in batches for key in batch] == requested
    assert len(batches) > 2
    for batch in batches:
        request = GrentonCluApiClientRegisterRequest(batch, 65535, subscription_id=65535, report_ip="255.255.255.255")
        assert len(api.cipher.encrypt(request.raw.encode())) <= MAX_STATE_REQUEST_BYTES


def test_exact_padding_boundary_and_single_key_overflow():
    api = Harness().api
    # Find the last ASCII key whose full registration still fits. PKCS#7 must
    # add a whole block when the plaintext already ends on a block boundary.
    last = None
    for size in range(1050, 1201):
        key = GrentonCluStateVariableKey("v" * size)
        request = GrentonCluApiClientRegisterRequest([key], 65535, subscription_id=65535, report_ip="255.255.255.255")
        encrypted_bytes = len(api.cipher.encrypt(request.raw.encode()))
        if encrypted_bytes <= MAX_STATE_REQUEST_BYTES:
            assert api.state_read_batches([key]) == [[key]]
            last = size
        else:
            with pytest.raises(ValueError, match="byte budget"):
                api.state_read_batches([key])
    assert last is not None


def test_ordinary_keys_keep_thirty_key_cap_and_large_values_are_singletons():
    api = Harness().api
    requested = keys(65)
    api.large_value_keys.add(requested[2])
    batches = api.state_read_batches(requested)
    assert list(map(len, batches)) == [30, 30, 4, 1]
    assert batches[-1] == [requested[2]]


def test_invalid_new_mapping_preserves_existing_subscriptions():
    async def run():
        harness = Harness()
        api = harness.api
        requested = keys(2)
        with harness.patched():
            assert await api.connect()
            await api.register_component_states(requested)
            groups, listener = api._groups.copy(), api.report_protocol
            sent = len(harness.sent)
            with pytest.raises(ValueError, match="byte budget"):
                await api.register_component_states([
                    *requested, GrentonCluStateVariableKey("v" * 1200),
                ])
            assert api._groups == groups
            assert api.report_protocol is listener
            assert len(harness.sent) == sent
            await api.disconnect()
    asyncio.run(run())
