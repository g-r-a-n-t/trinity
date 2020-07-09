from eth_typing import BLSPubkey, BLSSignature
from eth_utils import ValidationError, encode_hex

from eth2._utils.bls import bls
from eth2.beacon.attestation_helpers import (
    is_slashable_attestation_data,
    validate_indexed_attestation,
)
from eth2.beacon.committee_helpers import (
    get_beacon_committee,
    get_beacon_proposer_index,
    get_committee_count_at_slot,
)
from eth2.beacon.constants import FAR_FUTURE_EPOCH
from eth2.beacon.epoch_processing_helpers import get_indexed_attestation
from eth2.beacon.exceptions import SignatureError
from eth2.beacon.helpers import compute_epoch_at_slot, compute_signing_root, get_domain
from eth2.beacon.signature_domain import SignatureDomain
from eth2.beacon.types.attestation_data import AttestationData
from eth2.beacon.types.attestations import Attestation, IndexedAttestation
from eth2.beacon.types.attester_slashings import AttesterSlashing
from eth2.beacon.types.block_headers import SignedBeaconBlockHeader
from eth2.beacon.types.blocks import BaseBeaconBlock, BaseSignedBeaconBlock
from eth2.beacon.types.checkpoints import Checkpoint
from eth2.beacon.types.proposer_slashings import ProposerSlashing
from eth2.beacon.types.states import BeaconState
from eth2.beacon.types.validators import Validator
from eth2.beacon.types.voluntary_exits import SignedVoluntaryExit
from eth2.beacon.typing import CommitteeIndex, Epoch, Root, SerializableUint64, Slot
from eth2.configs import Eth2Config


def validate_correct_number_of_deposits(
    state: BeaconState, block: BaseBeaconBlock, config: Eth2Config
) -> None:
    body = block.body
    deposit_count_in_block = len(body.deposits)
    expected_deposit_count = min(
        config.MAX_DEPOSITS, state.eth1_data.deposit_count - state.eth1_deposit_index
    )

    if deposit_count_in_block != expected_deposit_count:
        raise ValidationError(
            f"Incorrect number of deposits ({deposit_count_in_block})"
            f" in block {encode_hex(block.hash_tree_root)};"
            f" expected {expected_deposit_count} based on"
            f" the state {encode_hex(state.hash_tree_root)}"
        )


#
# Block validatation
#
def validate_block_slot(state: BeaconState, block: BaseBeaconBlock) -> None:
    if block.slot != state.slot:
        raise ValidationError(
            f"block.slot ({block.slot}) is not equal to state.slot ({state.slot})"
        )


def validate_block_is_new(state: BeaconState, block: BaseBeaconBlock) -> None:
    if block.slot <= state.latest_block_header.slot:
        raise ValidationError(
            f"block.slot ({block.slot}) is not newer than the latest block header ({state.slot})"
        )


def validate_proposer_index(
    state: BeaconState, block: BaseBeaconBlock, config: Eth2Config
) -> None:
    expected_proposer = get_beacon_proposer_index(state, config)
    print(f"state slot {state.slot}")
    if block.proposer_index != expected_proposer:
        raise ValidationError(
            f"block.proposer_index "
            f"({block.proposer_index}) does not equal expected_proposer ({expected_proposer}) "
            f"at block.slot {state.slot}"
        )


def validate_block_parent_root(state: BeaconState, block: BaseBeaconBlock) -> None:
    expected_root = state.latest_block_header.hash_tree_root
    parent_root = block.parent_root
    if parent_root != expected_root:
        raise ValidationError(
            f"block.parent_root ({encode_hex(parent_root)}) is not equal to "
            f"state.latest_block_header.hash_tree_root ({encode_hex(expected_root)}"
        )


def validate_proposer_is_not_slashed(
    state: BeaconState, block_root: Root, config: Eth2Config
) -> None:
    proposer_index = get_beacon_proposer_index(state, config)
    proposer = state.validators[proposer_index]
    if proposer.slashed:
        raise ValidationError(f"Proposer for block {encode_hex(block_root)} is slashed")


def validate_proposer_signature(
    state: BeaconState, signed_block: BaseSignedBeaconBlock, config: Eth2Config
) -> None:
    # Get the public key of proposer
    beacon_proposer_index = get_beacon_proposer_index(state, config)
    proposer_pubkey = state.validators[beacon_proposer_index].pubkey
    domain = get_domain(
        state, SignatureDomain.DOMAIN_BEACON_PROPOSER, config.SLOTS_PER_EPOCH
    )
    signing_root = compute_signing_root(signed_block.message, domain)

    try:
        bls.validate(signing_root, signed_block.signature, proposer_pubkey)
    except SignatureError as error:
        raise ValidationError(
            f"Invalid Proposer Signature on block, beacon_proposer_index={beacon_proposer_index}",
            error,
        )


#
# RANDAO validatation
#
def validate_randao_reveal(
    state: BeaconState,
    proposer_index: int,
    epoch: Epoch,
    randao_reveal: BLSSignature,
    slots_per_epoch: int,
) -> None:
    proposer = state.validators[proposer_index]
    proposer_pubkey = proposer.pubkey
    domain = get_domain(state, SignatureDomain.DOMAIN_RANDAO, slots_per_epoch)

    signing_root = compute_signing_root(SerializableUint64(epoch), domain)

    try:
        bls.validate(signing_root, randao_reveal, proposer_pubkey)
    except SignatureError as error:
        raise ValidationError(
            f"RANDAO reveal is invalid for proposer index {proposer_index} at slot {state.slot}",
            error,
        )


#
# Proposer slashing validation
#
def validate_proposer_slashing(
    state: BeaconState, proposer_slashing: ProposerSlashing, slots_per_epoch: int
) -> None:
    """
    Validate the given ``proposer_slashing``.
    Raise ``ValidationError`` if it's invalid.
    """
    proposer = state.validators[
        proposer_slashing.signed_header_1.message.proposer_index
    ]

    validate_proposer_slashing_slot(proposer_slashing)

    validate_proposer_slashing_headers(proposer_slashing)

    validate_proposer_slashing_is_slashable(state, proposer, slots_per_epoch)

    validate_block_header_signature(
        state=state,
        header=proposer_slashing.signed_header_1,
        pubkey=proposer.pubkey,
        slots_per_epoch=slots_per_epoch,
    )

    validate_block_header_signature(
        state=state,
        header=proposer_slashing.signed_header_2,
        pubkey=proposer.pubkey,
        slots_per_epoch=slots_per_epoch,
    )


def validate_proposer_slashing_slot(proposer_slashing: ProposerSlashing) -> None:
    slot_1 = proposer_slashing.signed_header_1.message.slot
    slot_2 = proposer_slashing.signed_header_2.message.slot

    if slot_1 != slot_2:
        raise ValidationError(
            f"Slot of proposer_slashing.proposal_1 ({slot_1}) !="
            f" slot of proposer_slashing.proposal_2 ({slot_2})"
        )


def validate_proposer_slashing_headers(proposer_slashing: ProposerSlashing) -> None:
    header_1 = proposer_slashing.signed_header_1
    header_2 = proposer_slashing.signed_header_2

    if header_1.message.proposer_index != header_2.message.proposer_index:
        raise ValidationError(
            f"header_1.message.proposer_index ({header_1.message.proposer_index}) !="
            f" header_2.message.proposer_index ({header_2.message.proposer_index})"
        )

    if header_1 == header_2:
        raise ValidationError(f"header_1 ({header_1}) == header_2 ({header_2})")


def validate_proposer_slashing_is_slashable(
    state: BeaconState, proposer: Validator, slots_per_epoch: int
) -> None:
    current_epoch = state.current_epoch(slots_per_epoch)
    is_slashable = proposer.is_slashable(current_epoch)
    if not is_slashable:
        raise ValidationError(
            f"Proposer {encode_hex(proposer.pubkey)} is not slashable in epoch {current_epoch}."
        )


def validate_block_header_signature(
    state: BeaconState,
    header: SignedBeaconBlockHeader,
    pubkey: BLSPubkey,
    slots_per_epoch: int,
) -> None:
    domain = get_domain(
        state,
        SignatureDomain.DOMAIN_BEACON_PROPOSER,
        slots_per_epoch,
        compute_epoch_at_slot(header.message.slot, slots_per_epoch),
    )
    signing_root = compute_signing_root(header.message, domain)

    try:
        bls.validate(signing_root, header.signature, pubkey)
    except SignatureError as error:
        raise ValidationError("Header signature is invalid:", error)


#
# Attester slashing validation
#
def validate_is_slashable_attestation_data(
    attestation_1: IndexedAttestation, attestation_2: IndexedAttestation
) -> None:
    is_slashable_data = is_slashable_attestation_data(
        attestation_1.data, attestation_2.data
    )

    if not is_slashable_data:
        raise ValidationError(
            "The `AttesterSlashing` object doesn't meet the Casper FFG slashing conditions."
        )


def validate_attester_slashing(
    state: BeaconState,
    attester_slashing: AttesterSlashing,
    max_validators_per_committee: int,
    slots_per_epoch: int,
) -> None:
    attestation_1 = attester_slashing.attestation_1
    attestation_2 = attester_slashing.attestation_2

    validate_is_slashable_attestation_data(attestation_1, attestation_2)

    validate_indexed_attestation(state, attestation_1, slots_per_epoch)

    validate_indexed_attestation(state, attestation_2, slots_per_epoch)


def validate_some_slashing(
    slashed_any: bool, attester_slashing: AttesterSlashing
) -> None:
    if not slashed_any:
        raise ValidationError(
            f"Attesting slashing {attester_slashing} did not yield any slashable validators."
        )


#
# Attestation validation
#
def _validate_eligible_committee_index(
    state: BeaconState,
    attestation_slot: Slot,
    committee_index: CommitteeIndex,
    max_committees_per_slot: int,
    slots_per_epoch: int,
    target_committee_size: int,
) -> None:
    committees_per_slot = get_committee_count_at_slot(
        state,
        attestation_slot,
        max_committees_per_slot,
        slots_per_epoch,
        target_committee_size,
    )
    if committee_index >= committees_per_slot:
        raise ValidationError(
            f"Attestation with committee index ({committee_index}) must be"
            f" less than the calculated committee per slot ({committees_per_slot})"
            f" of slot {attestation_slot}"
        )


def _validate_eligible_target_epoch(
    target_epoch: Epoch, current_epoch: Epoch, previous_epoch: Epoch
) -> None:
    if target_epoch not in (previous_epoch, current_epoch):
        raise ValidationError(
            f"Attestation with target epoch {target_epoch} must be in either the"
            f" previous epoch {previous_epoch} or the current epoch {current_epoch}"
        )


def _validate_slot_matches_target_epoch(
    target_epoch: Epoch, attestation_slot: Slot, slots_per_epoch: int
) -> None:
    epoch = compute_epoch_at_slot(attestation_slot, slots_per_epoch)
    if target_epoch != epoch:
        raise ValidationError(
            f"Attestation at slot {attestation_slot} (epoch {epoch}) must be in"
            f" the same epoch with it's target epoch (epoch {target_epoch})"
        )


def validate_attestation_slot(
    attestation_slot: Slot,
    state_slot: Slot,
    slots_per_epoch: int,
    min_attestation_inclusion_delay: int,
) -> None:
    if attestation_slot + min_attestation_inclusion_delay > state_slot:
        raise ValidationError(
            f"Attestation at slot {attestation_slot} can only be included after the"
            f" minimum delay {min_attestation_inclusion_delay} with respect to the"
            f" state's slot {state_slot}."
        )

    if state_slot > attestation_slot + slots_per_epoch:
        raise ValidationError(
            f"Attestation at slot {attestation_slot} must be within {slots_per_epoch}"
            f" slots (1 epoch) of the state's slot {state_slot}"
        )


def _validate_checkpoint(
    checkpoint: Checkpoint, expected_checkpoint: Checkpoint
) -> None:
    if checkpoint != expected_checkpoint:
        raise ValidationError(
            f"Attestation with source checkpoint {checkpoint} did not match the expected"
            f" source checkpoint ({expected_checkpoint}) based on the specified ``target.epoch``."
        )


def _validate_attestation_data(
    state: BeaconState, data: AttestationData, config: Eth2Config
) -> None:
    slots_per_epoch = config.SLOTS_PER_EPOCH
    current_epoch = state.current_epoch(slots_per_epoch)
    previous_epoch = state.previous_epoch(slots_per_epoch)

    attestation_slot = data.slot

    if data.target.epoch == current_epoch:
        expected_checkpoint = state.current_justified_checkpoint
    else:
        expected_checkpoint = state.previous_justified_checkpoint

    _validate_eligible_committee_index(
        state,
        attestation_slot,
        data.index,
        config.MAX_COMMITTEES_PER_SLOT,
        config.SLOTS_PER_EPOCH,
        config.TARGET_COMMITTEE_SIZE,
    )

    _validate_eligible_target_epoch(data.target.epoch, current_epoch, previous_epoch)
    _validate_slot_matches_target_epoch(
        data.target.epoch, attestation_slot, slots_per_epoch
    )
    validate_attestation_slot(
        attestation_slot,
        state.slot,
        slots_per_epoch,
        config.MIN_ATTESTATION_INCLUSION_DELAY,
    )
    _validate_checkpoint(data.source, expected_checkpoint)


def _validate_aggregation_bits(
    state: BeaconState, attestation: Attestation, config: Eth2Config
) -> None:
    data = attestation.data
    committee = get_beacon_committee(state, data.slot, data.index, config)

    if len(attestation.aggregation_bits) != len(committee):
        raise ValidationError(
            f"The attestation bit lengths not match:"
            f"\tlen(attestation.aggregation_bits)={len(attestation.aggregation_bits)}\n"
            f"\tlen(committee)={len(committee)}"
        )


def validate_attestation(
    state: BeaconState, attestation: Attestation, config: Eth2Config
) -> None:
    """
    Validate the given ``attestation``.
    Raise ``ValidationError`` if it's invalid.
    """
    _validate_attestation_data(state, attestation.data, config)
    _validate_aggregation_bits(state, attestation, config)
    validate_indexed_attestation(
        state,
        get_indexed_attestation(state, attestation, config),
        config.SLOTS_PER_EPOCH,
    )


#
# Voluntary Exit validation
#
def _validate_validator_is_active(validator: Validator, target_epoch: Epoch) -> None:
    is_active = validator.is_active(target_epoch)
    if not is_active:
        raise ValidationError(
            f"Validator trying to exit in {target_epoch} is not active."
        )


def _validate_validator_has_not_exited(validator: Validator) -> None:
    if validator.exit_epoch != FAR_FUTURE_EPOCH:
        raise ValidationError(
            f"Validator {validator} in voluntary exit has already exited."
        )


def _validate_eligible_exit_epoch(exit_epoch: Epoch, current_epoch: Epoch) -> None:
    if current_epoch < exit_epoch:
        raise ValidationError(
            f"Validator in voluntary exit with exit epoch {exit_epoch}"
            f" is before the current epoch {current_epoch}."
        )


def _validate_validator_minimum_lifespan(
    validator: Validator, current_epoch: Epoch, shard_committee_period: int
) -> None:
    if current_epoch < validator.activation_epoch + shard_committee_period:
        raise ValidationError(
            f"Validator in voluntary exit has not completed the minimum number of epochs"
            f" {shard_committee_period} since activation in {validator.activation_epoch}"
            f" relative to the current epoch {current_epoch}."
        )


def _validate_voluntary_exit_signature(
    state: BeaconState,
    signed_voluntary_exit: SignedVoluntaryExit,
    validator: Validator,
    slots_per_epoch: int,
) -> None:
    voluntary_exit = signed_voluntary_exit.message
    domain = get_domain(
        state,
        SignatureDomain.DOMAIN_VOLUNTARY_EXIT,
        slots_per_epoch,
        voluntary_exit.epoch,
    )
    signing_root = compute_signing_root(voluntary_exit, domain)

    try:
        bls.validate(signing_root, signed_voluntary_exit.signature, validator.pubkey)
    except SignatureError as error:
        raise ValidationError(
            f"Invalid VoluntaryExit signature, validator_index={voluntary_exit.validator_index}",
            error,
        )


def validate_voluntary_exit(
    state: BeaconState,
    signed_voluntary_exit: SignedVoluntaryExit,
    slots_per_epoch: int,
    shard_committee_period: int,
) -> None:
    voluntary_exit = signed_voluntary_exit.message
    validator = state.validators[voluntary_exit.validator_index]
    current_epoch = state.current_epoch(slots_per_epoch)

    _validate_validator_is_active(validator, current_epoch)
    _validate_validator_has_not_exited(validator)
    _validate_eligible_exit_epoch(voluntary_exit.epoch, current_epoch)
    _validate_validator_minimum_lifespan(
        validator, current_epoch, shard_committee_period
    )
    _validate_voluntary_exit_signature(
        state, signed_voluntary_exit, validator, slots_per_epoch
    )
