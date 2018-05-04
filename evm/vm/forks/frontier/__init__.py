from __future__ import absolute_import

from evm.vm import VM
from evm.rlp.receipts import (
    Receipt,
)
from evm.rlp.logs import (
    Log,
)

from .state import FrontierState
from .headers import (
    create_frontier_header_from_parent,
    compute_frontier_difficulty,
    configure_frontier_header,
)
from .validation import validate_frontier_transaction_against_header


def make_frontier_receipt(base_header, transaction, computation, state):
    # Reusable for other forks

    logs = [
        Log(address, topics, data)
        for address, topics, data
        in computation.get_log_entries()
    ]

    gas_remaining = computation.get_gas_remaining()
    gas_refund = computation.get_gas_refund()
    tx_gas_used = (
        transaction.gas - gas_remaining
    ) - min(
        gas_refund,
        (transaction.gas - gas_remaining) // 2,
    )
    gas_used = base_header.gas_used + tx_gas_used

    receipt = Receipt(
        state_root=state.state_root,
        gas_used=gas_used,
        logs=logs,
    )

    return receipt


class FrontierVM(VM):
    # fork name
    fork = 'frontier'

    # classes
    _state_class = FrontierState

    # methods
    create_header_from_parent = staticmethod(create_frontier_header_from_parent)
    compute_difficulty = staticmethod(compute_frontier_difficulty)
    configure_header = configure_frontier_header
    make_receipt = staticmethod(make_frontier_receipt)
    validate_transaction_against_header = validate_frontier_transaction_against_header
