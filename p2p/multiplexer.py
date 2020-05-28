import asyncio
import collections
import contextlib
import time
from typing import (
    Any,
    AsyncIterator,
    cast,
    DefaultDict,
    Dict,
    Sequence,
    Tuple,
    Type,
    Union,
)

from cached_property import cached_property

from eth_utils import ValidationError
from eth_utils.toolz import cons
import rlp

from p2p.abc import (
    CommandAPI,
    MultiplexerAPI,
    NodeAPI,
    ProtocolAPI,
    SessionAPI,
    TransportAPI,
    TProtocol,
)
from p2p.exceptions import (
    CorruptTransport,
    UnknownProtocol,
    UnknownProtocolCommand,
    MalformedMessage,
)
from p2p.p2p_proto import BaseP2PProtocol
from p2p.transport_state import TransportState
from p2p._utils import (
    get_logger,
    snappy_CompressedLengthError,
)


async def stream_transport_messages(transport: TransportAPI,
                                    base_protocol: BaseP2PProtocol,
                                    *protocols: ProtocolAPI,
                                    ) -> AsyncIterator[Tuple[ProtocolAPI, CommandAPI[Any]]]:
    """
    Streams 2-tuples of (Protocol, Command) over the provided `Transport`
    """
    # A cache for looking up the proper protocol instance for a given command
    # id.
    command_id_cache: Dict[int, ProtocolAPI] = {}

    while not transport.is_closing:
        msg = await transport.recv()
        command_id = msg.command_id

        if msg.command_id not in command_id_cache:
            if command_id < base_protocol.command_length:
                command_id_cache[command_id] = base_protocol
            else:
                for protocol in protocols:
                    if command_id < protocol.command_id_offset + protocol.command_length:
                        command_id_cache[command_id] = protocol
                        break
                else:
                    protocol_infos = '  '.join(tuple(
                        (
                            f"{proto.name}@{proto.version}"
                            f"[offset={proto.command_id_offset},"
                            f"command_length={proto.command_length}]"
                        )
                        for proto in cons(base_protocol, protocols)
                    ))
                    raise UnknownProtocolCommand(
                        f"No protocol found for command_id {command_id}: Available "
                        f"protocol/offsets are: {protocol_infos}"
                    )

        msg_proto = command_id_cache[command_id]
        command_type = msg_proto.get_command_type_for_command_id(command_id)

        try:
            cmd = command_type.decode(msg, msg_proto.snappy_support)
        except (rlp.exceptions.DeserializationError, snappy_CompressedLengthError) as err:
            raise MalformedMessage(f"Failed to decode {msg} for {command_type}") from err

        yield msg_proto, cmd

        # yield to the event loop for a moment to allow `transport.is_closing`
        # a chance to update.
        await asyncio.sleep(0)


class Multiplexer(MultiplexerAPI):

    _transport: TransportAPI
    _msg_counts: DefaultDict[Type[CommandAPI[Any]], int]
    _last_msg_time: float

    _protocol_locks: Dict[Type[ProtocolAPI], asyncio.Lock]
    _protocol_queues: Dict[Type[ProtocolAPI], 'asyncio.Queue[CommandAPI[Any]]']

    def __init__(self,
                 transport: TransportAPI,
                 base_protocol: BaseP2PProtocol,
                 protocols: Sequence[ProtocolAPI],
                 max_queue_size: int = 4096) -> None:
        self.logger = get_logger('p2p.multiplexer.Multiplexer')
        self._transport = transport
        # the base `p2p` protocol instance.
        self._base_protocol = base_protocol

        # the sub-protocol instances
        self._protocols = protocols

        # Lock to ensure that multiple call sites cannot concurrently stream
        # messages.
        self._multiplex_lock = asyncio.Lock()

        # Lock management on a per-protocol basis to ensure we only have one
        # stream consumer for each protocol.
        self._protocol_locks = {
            type(protocol): asyncio.Lock()
            for protocol
            in self.get_protocols()
        }

        # Each protocol gets a queue where messages for the individual protocol
        # are placed when streamed from the transport
        self._protocol_queues = {
            type(protocol): asyncio.Queue(max_queue_size)
            for protocol
            in self.get_protocols()
        }

        self._msg_counts = collections.defaultdict(int)
        self._last_msg_time = 0

    def __str__(self) -> str:
        protocol_infos = ','.join(tuple(
            f"{proto.name}:{proto.version}"
            for proto
            in self.get_protocols()
        ))
        return f"Multiplexer[{protocol_infos}]"

    def __repr__(self) -> str:
        return f"<{self}>"

    #
    # Transport API
    #
    def get_transport(self) -> TransportAPI:
        return self._transport

    #
    # Message Counts
    #
    def get_total_msg_count(self) -> int:
        return sum(self._msg_counts.values())

    @property
    def last_msg_time(self) -> float:
        return self._last_msg_time

    #
    # Proxy Transport methods
    #
    @cached_property
    def remote(self) -> NodeAPI:
        return self._transport.remote

    @cached_property
    def session(self) -> SessionAPI:
        return self._transport.session

    @property
    def is_closing(self) -> bool:
        return self._transport.is_closing

    async def close(self) -> None:
        await self._transport.close()

    #
    # Protocol API
    #
    def has_protocol(self, protocol_identifier: Union[ProtocolAPI, Type[ProtocolAPI]]) -> bool:
        try:
            if isinstance(protocol_identifier, ProtocolAPI):
                self.get_protocol_by_type(type(protocol_identifier))
                return True
            elif isinstance(protocol_identifier, type):
                self.get_protocol_by_type(protocol_identifier)
                return True
            else:
                raise TypeError(
                    f"Unsupported protocol value: {protocol_identifier} of type "
                    f"{type(protocol_identifier)}"
                )
        except UnknownProtocol:
            return False

    def get_protocol_by_type(self, protocol_class: Type[TProtocol]) -> TProtocol:
        if issubclass(protocol_class, BaseP2PProtocol):
            return cast(TProtocol, self._base_protocol)

        for protocol in self._protocols:
            if type(protocol) is protocol_class:
                return cast(TProtocol, protocol)
        raise UnknownProtocol(f"No protocol found with type {protocol_class}")

    def get_base_protocol(self) -> BaseP2PProtocol:
        return self._base_protocol

    def get_protocols(self) -> Tuple[ProtocolAPI, ...]:
        return tuple(cons(self._base_protocol, self._protocols))

    def get_protocol_for_command_type(self, command_type: Type[CommandAPI[Any]]) -> ProtocolAPI:
        supported_protocols = tuple(
            protocol
            for protocol in self.get_protocols()
            if protocol.supports_command(command_type)
        )

        if len(supported_protocols) == 1:
            return supported_protocols[0]
        elif not supported_protocols:
            raise UnknownProtocol(
                f"Connection does not have any protocols that support the "
                f"request command: {command_type}"
            )
        elif len(supported_protocols) > 1:
            raise ValidationError(
                f"Could not determine appropriate protocol for command: "
                f"{command_type}.  Command was found in the "
                f"protocols {supported_protocols}"
            )
        else:
            raise Exception("This code path should be unreachable")

    #
    # Streaming API
    #
    def stream_protocol_messages(self,
                                 protocol_identifier: Union[ProtocolAPI, Type[ProtocolAPI]],
                                 ) -> AsyncIterator[CommandAPI[Any]]:
        """
        Stream the messages for the specified protocol.
        """
        if isinstance(protocol_identifier, ProtocolAPI):
            protocol_class = type(protocol_identifier)
        elif isinstance(protocol_identifier, type) and issubclass(protocol_identifier, ProtocolAPI):
            protocol_class = protocol_identifier
        else:
            raise TypeError("Unknown protocol identifier: {protocol}")

        if not self.has_protocol(protocol_class):
            raise UnknownProtocol(f"Unknown protocol '{protocol_class}'")

        if self._protocol_locks[protocol_class].locked():
            raise Exception(f"Streaming lock for {protocol_class} is not free.")
        elif not self._multiplex_lock.locked():
            raise Exception("Not multiplexed.")

        return self._stream_protocol_messages(protocol_class)

    async def _stream_protocol_messages(self,
                                        protocol_class: Type[ProtocolAPI],
                                        ) -> AsyncIterator[CommandAPI[Any]]:
        """
        Stream the messages for the specified protocol.
        """
        async with self._protocol_locks[protocol_class]:
            msg_queue = self._protocol_queues[protocol_class]
            while not self.is_closing:
                try:
                    # We use an optimistic strategy here of using
                    # `get_nowait()` to reduce the number of times we yield to
                    # the event loop.  Since this is an async generator it will
                    # yield to the loop each time it returns a value so we
                    # don't have to worry about this blocking other processes.
                    yield msg_queue.get_nowait()
                except asyncio.QueueEmpty:
                    yield await msg_queue.get()

    #
    # Message reading and streaming API
    #
    @contextlib.asynccontextmanager
    async def multiplex(self) -> AsyncIterator[None]:
        """
        API for running the background task that feeds individual protocol
        queues that allows each individual protocol to stream only its own
        messages.
        """
        async with self._multiplex_lock:
            stop = asyncio.Event()
            fut = asyncio.ensure_future(self._do_multiplexing(stop))
            # wait for the multiplexing to actually start
            try:
                yield
            finally:
                #
                # Prevent corruption of the Transport:
                #
                # On exit the `Transport` can be in a few states:
                #
                # 1. IDLE: between reads
                # 2. HEADER: waiting to read the bytes for the message header
                # 3. BODY: already read the header, waiting for body bytes.
                #
                # In the IDLE case we get a clean shutdown by simply signaling
                # to `_do_multiplexing` that it should exit which is done with
                # an `asyncio.EVent`
                #
                # In the HEADER case we can issue a hard stop either via
                # cancellation or the cancel token.  The read *should* be
                # interrupted without consuming any bytes from the
                # `StreamReader`.
                #
                # In the BODY case we want to give the `Transport.recv` call a
                # moment to finish reading the body after which it will be IDLE
                # and will exit via the IDLE exit mechanism.
                stop.set()

                # If the transport is waiting to read the body of the message
                # we want to give it a moment to finish that read.  Otherwise
                # this leaves the transport in a corrupt state.
                if self._transport.read_state is TransportState.BODY:
                    try:
                        await asyncio.wait_for(fut, timeout=1)
                    except asyncio.TimeoutError:
                        pass
                    except CorruptTransport as exc:
                        self.logger.error("Corrupt transport, waiting on body %s: %r", self, exc)
                        self.logger.debug("Corrupt transport, body trace: %s", self, exc_info=True)

                # After giving the transport an opportunity to shutdown cleanly, we issue a hard
                # shutdown via cancellation. This should only end up corrupting the transport in
                # the case where the header data is read but the body data takes too long to
                # arrive which should be very rare and would likely indicate a malicious or broken
                # peer.
                if fut.done():
                    fut.result()
                else:
                    fut.cancel()
                    try:
                        await fut
                    except asyncio.CancelledError:
                        pass

    async def _do_multiplexing(self, stop: asyncio.Event) -> None:
        """
        Background task that reads messages from the transport and feeds them
        into individual queues for each of the protocols.
        """
        msg_stream = stream_transport_messages(
            self._transport,
            self._base_protocol,
            *self._protocols,
        )
        try:
            await self._handle_commands(msg_stream, stop)
        except asyncio.TimeoutError as exc:
            self.logger.warning(
                "Timed out waiting for command from %s, Stop: %r, exiting...",
                self,
                stop.is_set(),
            )
            self.logger.debug("Timeout %r: %s", self, exc, exc_info=True)
        except CorruptTransport as exc:
            self.logger.error("Corrupt transport, while multiplexing %s: %r", self, exc)
            self.logger.debug("Corrupt transport, multiplexing trace: %s", self, exc_info=True)

    async def _handle_commands(
            self,
            msg_stream: AsyncIterator[Tuple[ProtocolAPI, CommandAPI[Any]]],
            stop: asyncio.Event) -> None:

        async for protocol, cmd in msg_stream:
            self._last_msg_time = time.monotonic()
            # track total number of messages received for each command type.
            self._msg_counts[type(cmd)] += 1

            queue = self._protocol_queues[type(protocol)]
            try:
                # We must use `put_nowait` here to ensure that in the event
                # that a single protocol queue is full that we don't block
                # other protocol messages getting through.
                queue.put_nowait(cmd)
            except asyncio.QueueFull:
                self.logger.error(
                    (
                        "Multiplexing queue for protocol '%s' full. "
                        "discarding message: %s"
                    ),
                    protocol,
                    cmd,
                )

            if stop.is_set():
                break
