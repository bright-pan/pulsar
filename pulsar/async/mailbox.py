'''Actors communicate with each other by sending and receiving messages.
The :mod:`pulsar.async.mailbox` module implements the message passing layer
via a bidirectional socket connections between the :class:`pulsar.Arbiter`
and an :class:`pulsar.Actor`.

Message sending is asynchronous and safe, the message is guaranteed to
eventually reach the recipient, provided that the recipient exists.

The implementation details are outlined below:

* Messages are sent via the :func:`.send` function, which is a proxy for
  the :meth:`.Actor.send` method. Here is how you ping actor ``abc``::

      from pulsar import send

      send('abc', 'ping')

* The :class:`pulsar.Arbiter` mailbox is a :class:`.TcpServer`
  accepting connections from remote actors.
* The :attr:`.Actor.mailbox` is a :class:`.MailboxClient` of the arbiter
  mailbox server.
* When an actor sends a message to another actor, the arbiter mailbox behaves
  as a proxy server by routing the message to the targeted actor.
* Communication is bidirectional and there is **only one connection** between
  the arbiter and any given actor.
* Messages are encoded and decoded using the unmasked websocket protocol
  implemented in :class:`.FrameParser`.
* If, for some reasons, the connection between an actor and the arbiter
  get broken, the actor will eventually stop running and garbaged collected.


Implementation
=========================
  For the curious this is how the internal protocol is implemented

Protocol
~~~~~~~~~~~~

.. autoclass:: MailboxProtocol
  :members:
  :member-order: bysource

Client
~~~~~~~~~~~~

.. autoclass:: MailboxClient
  :members:
  :member-order: bysource

'''
import sys
import logging
from collections import namedtuple

from pulsar import ProtocolError, CommandError
from pulsar.utils.pep import pickle
from pulsar.utils.internet import nice_address
from pulsar.utils.websocket import FrameParser
from pulsar.utils.security import gen_unique_id

from .access import asyncio, get_actor
from .defer import Failure, Deferred, coroutine_return, in_loop
from .proxy import actorid, get_proxy, get_command, ActorProxy
from .protocols import Protocol
from .clients import BaseClient


LOGGER = logging.getLogger('pulsar.mailbox')
CommandRequest = namedtuple('CommandRequest', 'actor caller connection')


def command_in_context(command, caller, actor, args, kwargs):
    cmnd = get_command(command)
    if not cmnd:
        raise CommandError('unknown %s' % command)
    request = CommandRequest(actor, caller, None)
    return cmnd(request, args, kwargs)


class ProxyMailbox(object):
    '''A proxy for the arbiter :class:`Mailbox`.
    '''
    active_connections = 0

    def __init__(self, actor):
        mailbox = actor.monitor.mailbox
        if isinstance(mailbox, ProxyMailbox):
            mailbox = mailbox.mailbox
        self.mailbox = mailbox

    def __repr__(self):
        return self.mailbox.__repr__()

    def __str__(self):
        return self.mailbox.__str__()

    def __getattr__(self, name):
        return getattr(self.mailbox, name)

    def _run(self):
        pass

    def close(self):
        pass


class Message(object):
    '''A message which travels from actor to actor.
    '''
    def __init__(self, data, future=None):
        self.data = data
        self.future = future

    def __repr__(self):
        return self.data.get('command', 'unknown')
    __str__ = __repr__

    @classmethod
    def command(cls, command, sender, target, args, kwargs):
        command = get_command(command)
        data = {'command': command.__name__,
                'sender': actorid(sender),
                'target': actorid(target),
                'args': args if args is not None else (),
                'kwargs': kwargs if kwargs is not None else {}}
        if command.ack:
            future = Deferred()
            data['ack'] = gen_unique_id()[:8]
        else:
            future = None
        return cls(data, future)

    @classmethod
    def callback(cls, result, ack):
        data = {'command': 'callback', 'result': result, 'ack': ack}
        return cls(data)


class MailboxProtocol(Protocol):
    '''The :class:`.Protocol` for internal message passing between actors.

    Encoding and decoding uses the unmasked websocket protocol.
    '''
    def __init__(self, **kw):
        super(MailboxProtocol, self).__init__(**kw)
        self._pending_responses = {}
        self._parser = FrameParser(kind=2)
        actor = get_actor()
        if actor.is_arbiter():
            self.bind_event('connection_lost', None, self._connection_lost)

    def request(self, command, sender, target, args, kwargs):
        '''Used by the server to send messages to the client.'''
        req = Message.command(command, sender, target, args, kwargs)
        self._start(req)
        return req.future

    def data_received(self, data):
        # Feed data into the parser
        msg = self._parser.decode(data)
        while msg:
            try:
                message = pickle.loads(msg.body)
            except Exception as e:
                raise ProtocolError('Could not decode message body: %s' % e)
            self._on_message(message)
            msg = self._parser.decode()

    ########################################################################
    ##    INTERNALS
    def _start(self, req):
        if req.future and 'ack' in req.data:
            self._pending_responses[req.data['ack']] = req.future
            try:
                self._write(req)
            except Exception:
                req.future.callback(sys.exc_info())
        else:
            self._write(req)

    def _connection_lost(self, failure):
        actor = get_actor()
        if actor.is_running():
            failure.log(msg='Connection lost with actor.', level='warning')
        else:
            failure.mute()
        return failure

    @in_loop
    def _on_message(self, message):
        actor = get_actor()
        command = message.get('command')
        ack = message.get('ack')
        if command == 'callback':
            if not ack:
                raise ProtocolError('A callback without id')
            try:
                pending = self._pending_responses.pop(ack)
            except KeyError:
                raise KeyError('Callback %s not in pending callbacks' % ack)
            pending.callback(message.get('result'))
        else:
            failure = None
            try:
                target = actor.get_actor(message['target'])
                if target is None:
                    raise CommandError('Cannot execute "%s" in %s. Unknown '
                                       'actor %s' % (command, actor,
                                                     message['target']))
                # Get the caller proxy without throwing
                caller = get_proxy(actor.get_actor(message['sender']),
                                   safe=True)
                if isinstance(target, ActorProxy):
                    # route the message to the actor proxy
                    if caller is None:
                        raise CommandError(
                            "'%s' got message from unknown '%s'"
                            % (actor, message['sender']))
                    result = yield actor.send(target, command,
                                              *message['args'],
                                              **message['kwargs'])
                else:
                    actor = target
                    command = get_command(command)
                    req = CommandRequest(target, caller, self.connection)
                    result = yield command(req, message['args'],
                                           message['kwargs'])
            except Exception:
                failure = sys.exc_info()
            if failure:
                result = Failure(failure)
            if ack:
                self.start(Message.callback(result, ack))

    def _write(self, req):
        obj = pickle.dumps(req.data, protocol=2)
        data = self._parser.encode(obj, opcode=0x2).msg
        try:
            self.transport.write(data)
        except IOError:
            actor = get_actor()
            if actor.is_running():
                raise


class MailboxClient(BaseClient):
    '''Used by actors to send messages to other actors via the arbiter.
    '''
    def __init__(self, address, actor, loop):
        self._loop = loop
        self.address = address
        self.name = 'Mailbox for %s' % actor
        self._connection = None

    def response(self, request):
        resp = super(MailboxClient, self).response
        self._consumer = resp(request, self._consumer, False)
        return self._consumer

    def __repr__(self):
        return '%s %s' % (self.name, nice_address(self.address))

    @in_loop
    def request(self, command, sender, target, args, kwargs):
        # the request method
        if self._connection is None:
            if isinstance(self.address, tuple):
                host, port = self.address
                _, connection = yield loop.create_connection(
                    MailboxProtocol, host, port)
            else:
                raise NotImplementedError
            self._connection = connection
        req = Message.command(command, sender, target, args, kwargs)
        self._connection._start(req)
        response = yield req.future
        coroutine_return(response)

    def close(self, async=False, timeout=None):
        if self._connection:
            self._connection.close(async=async)
