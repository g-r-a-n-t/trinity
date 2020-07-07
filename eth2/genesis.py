from typing import Any, Dict

import ssz
from eth.constants import ZERO_HASH32
from eth_utils import encode_hex
from ssz.tools.dump import to_formatted_dict
from ssz.tools.parse import from_formatted_dict
from typing_extensions import Literal

from eth2.beacon.genesis import initialize_beacon_state_from_eth1
from eth2.beacon.state_machines.forks.serenity.configs import SERENITY_CONFIG, ALTONA_CONFIG
from eth2.beacon.state_machines.forks.skeleton_lake.configs import (
    MINIMAL_SERENITY_CONFIG,
)
from eth2.beacon.tools.builder.initializer import (
    create_genesis_deposits_from,
    create_key_pairs_for,
    mk_genesis_key_map,
    mk_withdrawal_credentials_from,
)
from eth2.beacon.tools.misc.ssz_vector import override_lengths
from eth2.beacon.types.states import BeaconState
from eth2.beacon.typing import Timestamp
from eth2.configs import Eth2Config


def update_genesis_config_with_time(
    config: Dict[str, Any], genesis_time: Timestamp
) -> Dict[str, Any]:
    config_profile = config["profile"]
    eth2_config = _get_eth2_config(config_profile)
    override_lengths(eth2_config)

    existing_state = from_formatted_dict(config["genesis_state"], BeaconState)

    genesis_state = existing_state.set("genesis_time", genesis_time)

    updates = {
        "genesis_state_root": encode_hex(genesis_state.hash_tree_root),
        "genesis_state": to_formatted_dict(genesis_state),
    }
    return {**config, **updates}


def generate_genesis_config(
    config_profile: Literal["minimal", "mainnet", "altona"], genesis_time: Timestamp
) -> (Dict[str, Any], BeaconState):
    eth2_config = _get_eth2_config(config_profile)
    override_lengths(eth2_config)

    with open("/home/grant/workshop/trinity/genesis.ssz", "rb") as genesis_state_file:
        genesis_state = BeaconState.deserialize(genesis_state_file.read())

    return {
        "profile": config_profile,
        "eth2_config": eth2_config.to_formatted_dict(),
        "genesis_validator_key_pairs": (),
        "genesis_state_root": encode_hex(genesis_state.hash_tree_root),
        "genesis_state": to_formatted_dict(genesis_state),
        "genesis_state_ssz": BeaconState.serialize(genesis_state).hex()
    }


def _get_eth2_config(profile: str) -> Eth2Config:
    return {
        "minimal": MINIMAL_SERENITY_CONFIG,
        "mainnet": SERENITY_CONFIG,
        "altona": ALTONA_CONFIG,
    }[profile]
