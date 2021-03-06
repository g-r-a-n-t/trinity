from eth2.beacon.types.blocks import BaseSignedBeaconBlock
from eth2.beacon.types.states import BeaconState
from eth2.beacon.typing import Slot
from eth2.configs import Eth2Config

from .block_processing import process_block
from .block_validation import validate_proposer_signature
from .slot_processing import process_slots


def apply_state_transition(
    config: Eth2Config,
    state: BeaconState,
    signed_block: BaseSignedBeaconBlock = None,
    future_slot: Slot = None,
    check_proposer_signature: bool = True,
) -> BeaconState:
    """
    Callers should request a transition to some slot past the ``state.slot``.
    This can be done by providing either a ``block`` *or* a ``future_slot``.
    We enforce this invariant with the assertion on ``target_slot``.
    """
    target_slot = signed_block.message.slot if signed_block else future_slot
    assert target_slot is not None

    state = process_slots(state, target_slot, config)

    if signed_block:
        if check_proposer_signature:
            validate_proposer_signature(state, signed_block, config)
        state = process_block(state, signed_block.message, config)

    return state
