#!/bin/bash

#
# Starts a beacon node based upon a genesis state created by
# `./local_testnet_genesis_state`.
#

source ./vars.env

DEBUG_LEVEL=${2:-debug}

exec lighthouse \
	--debug-level $DEBUG_LEVEL \
	bn \
  --spec minimal \
	--datadir $BEACON_DIR \
	--testnet-dir $TESTNET_DIR \
	--dummy-eth1 \
	--http \
	--enr-address 127.0.0.1 \
	--enr-udp-port 9000 \
	--enr-tcp-port 9000 \
#  --libp2p-addresses /ip4/127.0.0.1/tcp/13000 \
