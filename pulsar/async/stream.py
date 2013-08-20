'''
The :mod:`pulsar.async.stream` implements classes and functions for
handling TCP streams.

SocketStreamTransport
============================

.. autoclass:: SocketStreamTransport
   :members:
   :member-order: bysource
'''
import os
import sys
import socket
try:
    import ssl
except ImportError:  # pragma: no cover
    ssl = None

from pulsar.utils.internet import (TRY_WRITE_AGAIN, TRY_READ_AGAIN,
                                   ACCEPT_ERRORS, EWOULDBLOCK, EPERM)
from pulsar.utils.structures import merge_prefix

from .consts import NUMBER_ACCEPTS
from .defer import multi_async, Deferred
from .internet import SocketTransport, WRITE_BUFFER_MAX_SIZE, AF_INET6
    

class SocketStreamTransport(SocketTransport):
    '''A :class:`pulsar.SocketTransport` for TCP streams.
    
The primary feature of a stream transport is sending bytes to a protocol
and receiving bytes from the underlying protocol. Writing to the transport
is done using the :meth:`write` and :meth:`writelines` methods.
The latter method is a performance optimisation, to allow software to take
advantage of specific capabilities in some transport mechanisms.'''
    _paused_reading = False
    _paused_writing = False
    
    def pause(self):
        """A :class:`SocketStreamTransport` can be paused and resumed.
Invoking this method will cause the transport to buffer data coming
from protocols but not sending it to the :attr:`protocol`. In other words,
no data will be passed to the :meth:`pulsar.Protocol.data_received` method
until :meth:`resume` is called."""
        if not self._paused_reading:
            self._paused_reading = True

    def resume(self):
        """Resume the receiving end. Data received will once again be
passed to the :meth:`pulsar.Protocol.data_received` method."""
        if self._paused_reading:
            self._paused_reading = False
            buffer = self._read_buffer
            self._read_buffer = []
            for data in buffer:
                self._data_received(chunk)
                
    def pause_writing(self):
        '''Suspend sending data to the network until a subsequent
:meth:`resume_writing` call. Between :meth:`pause_writing` and
:meth:`resume_writing` the transport's :meth:`write` method will just
be accumulating data in an internal buffer.'''
        if not self._paused_writing:
            self._paused_writing = True
            self._event_loop.remove_writer(self._sock_fd)

    def resume_writing(self):
        '''Restart sending data to the network.'''
        if self._paused_writing:
            if self._write_buffer:
                self._event_loop.add_writer(self._sock_fd, self._write_ready)
            self._paused_writing = False
            
    def write(self, data):
        '''Write chunk of ``data`` to the endpoint.'''
        if not data:
            return
        self._check_closed()
        writing = self.writing
        if data:
            assert isinstance(data, bytes)
            if len(data) > WRITE_BUFFER_MAX_SIZE:
                for i in range(0, len(data), WRITE_BUFFER_MAX_SIZE):
                    self._write_buffer.append(data[i:i+WRITE_BUFFER_MAX_SIZE])
            else:
                self._write_buffer.append(data)
        # Try to write only when not waiting for write callbacks
        if not writing and not self._paused_writing:
            self._ready_write()
            if self.writing:    # still writing
                self._event_loop.add_writer(self._sock_fd, self._ready_write)
            elif self._closing:
                self._event_loop.call_soon(self._shutdown)
                
    def writelines(self, list_of_data):
        """Write a list (or any iterable) of data bytes to the transport."""
        for data in list_of_data:
            self.write(data)
                 
    def _ready_write(self):
        # Do the actual writing
        buffer = self._write_buffer
        tot_bytes = 0
        if self._paused_writing:
            return tot_bytes
        if not buffer:
            self._event_loop.warning('handling write on a 0 length buffer')
        try:
            while buffer:
                try:
                    sent = self.sock.send(buffer[0])
                    if sent == 0:
                        # With OpenSSL, after send returns EWOULDBLOCK,
                        # the very same string object must be used on the
                        # next call to send.  Therefore we suppress
                        # merging the write buffer after an EWOULDBLOCK.
                        break
                    merge_prefix(buffer, sent)
                    buffer.popleft()
                    tot_bytes += sent
                except socket.error as e:
                    if e.args[0] in TRY_WRITE_AGAIN:
                        break
                    else:
                        raise
        except Exception as e:
            self.abort(exc=e)
        else:
            if not self.writing:
                self._event_loop.remove_writer(self._sock_fd)
                if self._closing:
                    self._event_loop.call_soon(self._shutdown)
            return tot_bytes
    
    def _read_ready(self):
        # Read from the socket until we get EWOULDBLOCK or equivalent.
        # If any other error occur, abort the connection and re-raise.
        passes = 0
        chunk = True
        try:
            while chunk:
                try:
                    chunk = self._sock.recv(self._read_chunk_size)
                except socket.error as e:
                    if e.args[0] == EWOULDBLOCK:
                        return
                    else:
                        raise
                if chunk:
                    if self._paused_reading:
                        self._read_buffer.append(chunk)
                    else:
                        self._protocol.data_received(chunk)
                elif not passes and chunk == b'':
                    # We got empty data. Close the socket
                    try:
                        self._protocol.eof_received()
                    finally:
                        self.close()
                passes += 1
        except Exception as exc:
            self.abort(exc)
    
    
class SocketStreamSslTransport(SocketStreamTransport):
    
    def __init__(self, event_loop, rawsock, protocol, sslcontext,
                 server_side=False, **kwargs):
        if server_side:
            assert isinstance(
                sslcontext, ssl.SSLContext), 'Must pass an SSLContext'
        else:
            # Client-side may pass ssl=True to use a default context.
            sslcontext = sslcontext or ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        sslsock = sslcontext.wrap_socket(rawsock, server_side=server_side,
                                         do_handshake_on_connect=False)

        super(SocketStreamSslTransport, self).__init__(loop, sslsock, protocol,
                                                       **kwargs)
        self._rawsock = rawsock


def create_connection(event_loop, protocol_factory, host, port, ssl,
                      family, proto, flags, sock, local_addr):
    if host is not None or port is not None:
        if sock is not None:
            raise ValueError(
                'host/port and sock can not be specified at the same time')

        fs = [event_loop.getaddrinfo(
                host, port, family=family,
                type=socket.SOCK_STREAM, proto=proto, flags=flags)]
        if local_addr is not None:
            fs.append(event_loop.getaddrinfo(
                *local_addr, family=family,
                type=socket.SOCK_STREAM, proto=proto, flags=flags))
        #
        fs = yield multi_async(fs)
        if len(fs) == 2:
            laddr_infos = fs[1]
            if not laddr_infos:
                raise socket.error('getaddrinfo() returned empty list')
        else:
            laddr_infos = None
        #
        socket_factory = getattr(event_loop, 'socket_factory', socket.socket)
        exceptions = []
        for family, type, proto, cname, address in fs[0]:
            try:
                sock = socket_factory(family=family, type=type, proto=proto)
                sock.setblocking(0)
                if laddr_infos is not None:
                    for _, _, _, _, laddr in laddr_infos:
                        try:
                            sock.bind(laddr)
                            break
                        except socket.error as exc:
                            exc = socket.error(
                                exc.errno, 'error while '
                                'attempting to bind on address '
                                '{!r}: {}'.format(
                                    laddr, exc.strerror.lower()))
                            exceptions.append(exc)
                    else:
                        sock.close()
                        sock = None
                        continue
                yield event_loop.sock_connect(sock, address)
            except socket.error as exc:
                if sock is not None:
                    sock.close()
                exceptions.append(exc)
            else:
                break
        else:
            if len(exceptions) == 1:
                raise exceptions[0]
            else:
                # If they all have the same str(), raise one.
                model = str(exceptions[0])
                if all(str(exc) == model for exc in exceptions):
                    raise exceptions[0]
                # Raise a combined exception so the user can see all
                # the various error messages.
                raise socket.error('Multiple exceptions: {}'.format(
                    ', '.join(str(exc) for exc in exceptions)))

    elif sock is None:
        raise ValueError(
            'host and port was not specified and no sock specified')

    sock.setblocking(False)
    protocol = protocol_factory()
    if ssl:
        sslcontext = None if isinstance(ssl, bool) else ssl
        transport = SocketStreamSslTransport(
            event_loop, sock, protocol, sslcontext, waiter, server_side=False)
    else:
        transport = SocketStreamTransport(event_loop, sock, protocol)
    yield transport, protocol
    
def start_serving(event_loop, protocol_factory, host, port, ssl,
                  family, flags, sock, backlog, reuse_address):
    #Coroutine which starts socket servers
    if host is not None or port is not None:
        if sock is not None:
            raise ValueError(
                'host/port and sock can not be specified at the same time')
        if reuse_address is None:
            reuse_address = os.name == 'posix' and sys.platform != 'cygwin'
        sockets = []
        if host == '':
            host = None

        infos = yield event_loop.getaddrinfo(
            host, port, family=family,
            type=socket.SOCK_STREAM, proto=0, flags=flags)
        if not infos:
            raise socket.error('getaddrinfo() returned empty list')
        #
        socket_factory = getattr(event_loop, 'socket_factory', socket.socket)
        completed = False
        try:
            for af, socktype, proto, canonname, sa in infos:
                sock = socket_factory(af, socktype, proto)
                sockets.append(sock)
                if reuse_address:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,
                                    True)
                # Disable IPv4/IPv6 dual stack support (enabled by
                # default on Linux) which makes a single socket
                # listen on both address families.
                if af == AF_INET6 and hasattr(socket, 'IPPROTO_IPV6'):
                    sock.setsockopt(socket.IPPROTO_IPV6,
                                    socket.IPV6_V6ONLY,
                                    True)
                try:
                    sock.bind(sa)
                except socket.error as err:
                    raise socket.error(err.errno, 'error while attempting '
                                       'to bind on address %r: %s'
                                       % (sa, err.strerror.lower()))
            completed = True
        finally:
            if not completed:
                for sock in sockets:
                    sock.close()
    else:
        if sock is None:
            raise ValueError(
                'host and port was not specified and no sock specified')
        sockets = [sock]

    for sock in sockets:
        sock.listen(backlog)
        sock.setblocking(False)
        event_loop.add_reader(sock.fileno(), sock_accept_connection,
                              event_loop, protocol_factory, sock, ssl)
    yield sockets

def sock_connect(event_loop, sock, address, future=None):
    fd = sock.fileno()
    connect = False
    if future is None:
        def canceller(d):
            event_loop.remove_writer(fd)
            d._suppressAlreadyCalled = True
        #
        future = Deferred(canceller=canceller, event_loop=event_loop)
        connect = True
    try:
        if connect:
            try:
                sock.connect(address)
            except socket.error as e:
                if e.args[0] in TRY_WRITE_AGAIN:
                    event_loop.add_writer(fd, sock_connect, event_loop, sock,
                                          address, future)
                    return future
                else:
                    raise
        else:   # This is the callback from the event loop
            event_loop.remove_writer(fd)
            if future.cancelled():
                return
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err != 0:
                # Jump to the except clause below.
                raise socket.error(err, 'Connect call failed')
            future.callback(None)
    except Exception as exc:
        return future.callback(exc)
        
def sock_accept(event_loop, sock, future=None):
    fd = sock.fileno()
    if future is None:
        future = Deferred()
    else:
        event_loop.remove_reader(fd)
    if not future.cancelled():
        try:
            conn, address = sock.accept()
            conn.setblocking(False)
            future.set_result((conn, address))
        except socket.error as e:
            if e.args[0] in TRY_READ_AGAIN:
                event_loop.add_reader(fd, sock_accept, event_loop, sock, future)
            elif e.args[0] == EPERM:
                # Netfilter on Linux may have rejected the
                # connection, but we get told to try to accept() anyway.
                return sock_accept(event_loop, sock, future)
            else:
                future.callback(e)
        except Exception as e:
            future.callback(e)
    return future

def sock_accept_connection(event_loop, protocol_factory, sock, ssl):
    try:
        for i in range(NUMBER_ACCEPTS):
            try:
                conn, address = sock.accept()
            except socket.error as e:
                if e.args[0] in TRY_READ_AGAIN:
                    break
                elif e.args[0] == EPERM:
                    # Netfilter on Linux may have rejected the
                    # connection, but we get told to try to accept() anyway.
                    continue
                elif e.args[0] in ACCEPT_ERRORS:
                    event_loop.logger.info('Could not accept new connection')
                    break
                raise
            protocol = protocol_factory()
            if ssl:
                SocketStreamSslTransport(event_loop, conn, protocol, ssl,
                                         extra={'addr': address})
            else:
                SocketStreamTransport(event_loop, conn, protocol,
                                      extra={'addr': address})
    except Exception:
        event_loop.logger.exception('Could not accept new connection')