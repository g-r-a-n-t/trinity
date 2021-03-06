from eth2.beacon.chains.base import BeaconChain
from eth2.beacon.constants import GENESIS_SLOT
from eth2.beacon.state_machines.forks.skeleton_lake import SkeletonLakeStateMachine


class SkeletonLakeChain(BeaconChain):
    sm_configuration = ((GENESIS_SLOT, SkeletonLakeStateMachine),)
