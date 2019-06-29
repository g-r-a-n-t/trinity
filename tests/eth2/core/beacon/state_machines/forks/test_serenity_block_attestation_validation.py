import pytest
from hypothesis import (
    given,
    settings,
    strategies as st,
)

from eth_utils import (
    ValidationError,
)

from eth.constants import (
    ZERO_HASH32,
)
from eth2.beacon.helpers import (
    get_epoch_start_slot,
)
from eth2.beacon.state_machines.forks.serenity.block_validation import (
    validate_attestation_slot,
    validate_attestation,
)
from eth2.beacon.types.attestation_data import AttestationData
from eth2.beacon.types.crosslinks import Crosslink


@pytest.mark.parametrize(
    ('slots_per_epoch', 'min_attestation_inclusion_delay'),
    [
        (4, 2),
    ]
)
@pytest.mark.parametrize(
    (
        'attestation_slot,'
        'state_slot,'
        'is_valid,'
    ),
    [
        # in bounds at lower end
        (8, 2 + 8, True),
        # in bounds at high end
        (8, 8 + 4, True),
        # state_slot > attestation_slot + slots_per_epoch
        (8, 8 + 4 + 1, False),
        # attestation_slot + min_attestation_inclusion_delay > state_slot
        (8, 8 - 2, False),
    ]
)
def test_validate_attestation_slot(attestation_slot,
                                   state_slot,
                                   slots_per_epoch,
                                   min_attestation_inclusion_delay,
                                   is_valid):

    if is_valid:
        validate_attestation_slot(
            attestation_slot,
            state_slot,
            slots_per_epoch,
            min_attestation_inclusion_delay,
        )
    else:
        with pytest.raises(ValidationError):
            validate_attestation_slot(
                attestation_slot,
                state_slot,
                slots_per_epoch,
                min_attestation_inclusion_delay,
            )


@pytest.mark.parametrize(
    (
        'current_epoch',
        'previous_justified_epoch',
        'current_justified_epoch',
        'previous_justified_root',
        'current_justified_root',
        'slots_per_epoch',
    ),
    [
        (3, 1, 2, b'\x11' * 32, b'\x22' * 32, 8)
    ]
)
@pytest.mark.parametrize(
    (
        'attestation_slot',
        'attestation_source_epoch',
        'attestation_source_root',
        'is_valid',
    ),
    [
        # slot_to_epoch(attestation_data.slot, slots_per_epoch) >= current_epoch
        # attestation_data.source_epoch == state.current_justified_epoch
        (24, 2, b'\x22' * 32, True),
        # attestation_data.source_epoch != state.current_justified_epoch
        (24, 3, b'\x22' * 32, False),
        # attestation_data.source_root != state.current_justified_root
        (24, 2, b'\x33' * 32, False),
        # slot_to_epoch(attestation_data.slot, slots_per_epoch) < current_epoch
        # attestation_data.source_epoch == state.previous_justified_epoch
        (23, 1, b'\x11' * 32, True),
        # attestation_data.source_epoch != state.previous_justified_epoch
        (23, 2, b'\x11' * 32, False),
        # attestation_data.source_root != state.current_justified_root
        (23, 1, b'\x33' * 32, False),
    ]
)
def test_validate_attestation_source_epoch_and_root(
        genesis_state,
        sample_attestation_data_params,
        attestation_slot,
        attestation_source_epoch,
        attestation_source_root,
        current_epoch,
        previous_justified_epoch,
        current_justified_epoch,
        previous_justified_root,
        current_justified_root,
        slots_per_epoch,
        config,
        is_valid):
    state = genesis_state.copy(
        slot=get_epoch_start_slot(current_epoch, slots_per_epoch),
        previous_justified_epoch=previous_justified_epoch,
        current_justified_epoch=current_justified_epoch,
        previous_justified_root=previous_justified_root,
        current_justified_root=current_justified_root,
    )
    attestation_data = AttestationData(**sample_attestation_data_params).copy(
        slot=attestation_slot,
        source_epoch=attestation_source_epoch,
        source_root=attestation_source_root,
    )

    if is_valid:
        validate_attestation(
            state,
            attestation_data,
            config,
        )
    else:
        with pytest.raises(ValidationError):
            validate_attestation(
                state,
                attestation_data,
                config,
            )


def _crosslink_from_byte(byte):
    return Crosslink(
        shard=12,
        start_epoch=0,
        end_epoch=1,
        parent_root=b'\x00' * 32,
        data_root=byte * 32,
    )


@pytest.mark.parametrize(
    (
        'attestation_previous_crosslink,'
        'attestation_crosslink_data_root,'
        'state_latest_crosslink,'
        'is_valid,'
    ),
    [
        (
            _crosslink_from_byte(b'\x11'),
            b'\x33' * 32,
            _crosslink_from_byte(b'\x22'),
            False,
        ),
        (
            _crosslink_from_byte(b'\x33'),
            b'\x33' * 32,
            _crosslink_from_byte(b'\x11'),
            False,
        ),
        (
            _crosslink_from_byte(b'\x11'),
            b'\x33' * 32,
            _crosslink_from_byte(b'\x33'),
            True,
        ),
        (
            _crosslink_from_byte(b'\x33'),
            b'\x22' * 32,
            _crosslink_from_byte(b'\x33'),
            True,
        ),
        (
            _crosslink_from_byte(b'\x33'),
            b'\x33' * 32,
            _crosslink_from_byte(b'\x33'),
            True,
        ),
    ]
)
def test_validate_attestation_latest_crosslink(sample_attestation_data_params,
                                               attestation_previous_crosslink,
                                               attestation_crosslink_data_root,
                                               state_latest_crosslink,
                                               slots_per_epoch,
                                               is_valid):
    sample_attestation_data_params['previous_crosslink'] = attestation_previous_crosslink
    sample_attestation_data_params['crosslink_data_root'] = attestation_crosslink_data_root
    attestation_data = AttestationData(**sample_attestation_data_params).copy(
        previous_crosslink=attestation_previous_crosslink,
        crosslink_data_root=attestation_crosslink_data_root,
    )

    if is_valid:
        validate_attestation_previous_crosslink_or_root(
            attestation_data,
            state_latest_crosslink,
            slots_per_epoch=slots_per_epoch,
        )
    else:
        with pytest.raises(ValidationError):
            validate_attestation_previous_crosslink_or_root(
                attestation_data,
                state_latest_crosslink,
                slots_per_epoch=slots_per_epoch,
            )


@pytest.mark.parametrize(
    (
        'attestation_crosslink_data_root,'
        'is_valid,'
    ),
    [
        (ZERO_HASH32, True),
        (b'\x22' * 32, False),
        (b'\x11' * 32, False),
    ]
)
def test_validate_attestation_crosslink_data_root(sample_attestation_data_params,
                                                  attestation_crosslink_data_root,
                                                  is_valid):
    attestation_data = AttestationData(**sample_attestation_data_params).copy(
        crosslink_data_root=attestation_crosslink_data_root,
    )

    if is_valid:
        validate_attestation_crosslink_data_root(
            attestation_data,
        )
    else:
        with pytest.raises(ValidationError):
            validate_attestation_crosslink_data_root(
                attestation_data,
            )


# TODO(ralexstokes) moved to indexed attestation signature in attestation_helpers
# @settings(max_examples=1)
# @given(random=st.randoms())
# @pytest.mark.parametrize(
#     (
#         'validator_count,'
#         'slots_per_epoch,'
#         'target_committee_size,'
#         'shard_count,'
#         'is_valid,'
#     ),
#     [
#         (10, 2, 2, 2, True),
#         (40, 4, 3, 5, True),
#         (20, 5, 3, 2, True),
#         (20, 5, 3, 2, False),
#     ],
# )
# def test_validate_attestation_aggregate_signature(genesis_state,
#                                                   slots_per_epoch,
#                                                   random,
#                                                   sample_attestation_data_params,
#                                                   is_valid,
#                                                   target_committee_size,
#                                                   shard_count,
#                                                   keymap,
#                                                   committee_config):
#     state = genesis_state

#     # choose committee
#     slot = 0
#     crosslink_committee = get_crosslink_committees_at_slot(
#         state=state,
#         slot=slot,
#         committee_config=committee_config,
#     )[0]
#     committee, shard = crosslink_committee
#     committee_size = len(committee)
#     assert committee_size > 0

#     # randomly select 3/4 participants from committee
#     votes_count = len(committee) * 3 // 4
#     assert votes_count > 0

#     attestation_data = AttestationData(**sample_attestation_data_params).copy(
#         slot=slot,
#         shard=shard,
#     )

#     attestation = create_mock_signed_attestation(
#         state,
#         attestation_data,
#         committee,
#         votes_count,
#         keymap,
#         slots_per_epoch,
#     )

#     if is_valid:
#         validate_attestation_aggregate_signature(
#             state,
#             attestation,
#             committee_config,
#         )
#     else:
#         # mess up signature
#         attestation = attestation.copy(
#             aggregate_signature=(
#                 attestation.aggregate_signature[0] + 10,
#                 attestation.aggregate_signature[1] - 1
#             )
#         )
#         with pytest.raises(ValidationError):
#             validate_attestation_aggregate_signature(
#                 state,
#                 attestation,
#                 committee_config,
#             )
