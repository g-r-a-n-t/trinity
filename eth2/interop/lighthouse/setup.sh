#!/bin/bash

#
# Produces a testnet specification and a genesis state where the genesis time
# is now.
#

source ./vars.env

echo "Creating beacon_node.ssz file from trinity config"

cat $TRINITY_CONFIG | sed -E 's/.*"genesis_state_ssz": "(\w*)".*/\1/' | xxd -r -p > $TESTNET_DIR/genesis.ssz

echo Created genesis state in $TESTNET_DIR
