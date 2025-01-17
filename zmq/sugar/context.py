"""Python bindings for 0MQ."""

# Copyright (C) PyZMQ Developers
# Distributed under the terms of the Modified BSD License.

import atexit
import os
import warnings
from threading import Lock
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar
from weakref import WeakSet

from zmq.backend import Context as ContextBase
from zmq.constants import ContextOption, Errno, SocketOption
from zmq.error import ZMQError

from .attrsettr import AttributeSetter, OptValT
from .socket import Socket

# notice when exiting, to avoid triggering term on exit
_exiting = False


def _notice_atexit() -> None:
    global _exiting
    _exiting = True


atexit.register(_notice_atexit)

T = TypeVar('T', bound='Context')
ST = TypeVar('ST', bound='Socket', covariant=True)


class Context(ContextBase, AttributeSetter, Generic[ST]):
    """Create a zmq Context

    A zmq Context creates sockets via its ``ctx.socket`` method.
    """

    sockopts: Dict[int, Any]
    _instance: Any = None
    _instance_lock = Lock()
    _instance_pid: Optional[int] = None
    _shadow = False
    _sockets: WeakSet
    # mypy doesn't like a default value here
    _socket_class: Type[ST] = Socket  # type: ignore

    def __init__(self: "Context[Socket]", io_threads: int = 1, **kwargs: Any) -> None:
        super().__init__(io_threads=io_threads, **kwargs)
        if kwargs.get('shadow', False):
            self._shadow = True
        else:
            self._shadow = False
        self.sockopts = {}
        self._sockets = WeakSet()

    def __del__(self) -> None:
        """deleting a Context should terminate it, without trying non-threadsafe destroy"""

        # Calling locals() here conceals issue #1167 on Windows CPython 3.5.4.
        locals()

        if not self._shadow and not _exiting and not self.closed:
            warnings.warn(
                f"unclosed context {self}",
                ResourceWarning,
                stacklevel=2,
                source=self,
            )
            self.term()

    _repr_cls = "zmq.Context"

    def __repr__(self) -> str:
        cls = self.__class__
        # look up _repr_cls on exact class, not inherited
        _repr_cls = cls.__dict__.get("_repr_cls", None)
        if _repr_cls is None:
            _repr_cls = f"{cls.__module__}.{cls.__name__}"

        closed = ' closed' if self.closed else ''
        if self._sockets:
            n_sockets = len(self._sockets)
            s = 's' if n_sockets > 1 else ''
            sockets = f"{n_sockets} socket{s}"
        else:
            sockets = ""
        return f"<{_repr_cls}({sockets}) at {hex(id(self))}{closed}>"

    def __enter__(self: T) -> T:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.term()

    def __copy__(self: T, memo: Any = None) -> T:
        """Copying a Context creates a shadow copy"""
        return self.__class__.shadow(self.underlying)

    __deepcopy__ = __copy__

    @classmethod
    def shadow(cls: Type[T], address: int) -> T:
        """Shadow an existing libzmq context

        address is the integer address of the libzmq context
        or an FFI pointer to it.

        .. versionadded:: 14.1
        """
        from zmq.utils.interop import cast_int_addr

        address = cast_int_addr(address)
        return cls(shadow=address)

    @classmethod
    def shadow_pyczmq(cls: Type[T], ctx: Any) -> T:
        """Shadow an existing pyczmq context

        ctx is the FFI `zctx_t *` pointer

        .. versionadded:: 14.1
        """
        from pyczmq import zctx  # type: ignore

        from zmq.utils.interop import cast_int_addr

        underlying = zctx.underlying(ctx)
        address = cast_int_addr(underlying)
        return cls(shadow=address)

    # static method copied from tornado IOLoop.instance
    @classmethod
    def instance(cls: Type[T], io_threads: int = 1) -> T:
        """Returns a global Context instance.

        Most single-threaded applications have a single, global Context.
        Use this method instead of passing around Context instances
        throughout your code.

        A common pattern for classes that depend on Contexts is to use
        a default argument to enable programs with multiple Contexts
        but not require the argument for simpler applications::

            class MyClass(object):
                def __init__(self, context=None):
                    self.context = context or Context.instance()

        .. versionchanged:: 18.1

            When called in a subprocess after forking,
            a new global instance is created instead of inheriting
            a Context that won't work from the parent process.
        """
        if (
            cls._instance is None
            or cls._instance_pid != os.getpid()
            or cls._instance.closed
        ):
            with cls._instance_lock:
                if (
                    cls._instance is None
                    or cls._instance_pid != os.getpid()
                    or cls._instance.closed
                ):
                    cls._instance = cls(io_threads=io_threads)
                    cls._instance_pid = os.getpid()
        return cls._instance

    def term(self) -> None:
        """Close or terminate the context.

        Context termination is performed in the following steps:

        - Any blocking operations currently in progress on sockets open within context shall
          raise :class:`zmq.ContextTerminated`.
          With the exception of socket.close(), any further operations on sockets open within this context
          shall raise :class:`zmq.ContextTerminated`.
        - After interrupting all blocking calls, term shall block until the following conditions are satisfied:
            - All sockets open within context have been closed.
            - For each socket within context, all messages sent on the socket have either been
              physically transferred to a network peer,
              or the socket's linger period set with the zmq.LINGER socket option has expired.

        For further details regarding socket linger behaviour refer to libzmq documentation for ZMQ_LINGER.

        This can be called to close the context by hand. If this is not called,
        the context will automatically be closed when it is garbage collected.
        """
        super().term()

    # -------------------------------------------------------------------------
    # Hooks for ctxopt completion
    # -------------------------------------------------------------------------

    def __dir__(self) -> List[str]:
        keys = dir(self.__class__)
        keys.extend(ContextOption.__members__)
        return keys

    # -------------------------------------------------------------------------
    # Creating Sockets
    # -------------------------------------------------------------------------

    def _add_socket(self, socket: Any) -> None:
        """Add a weakref to a socket for Context.destroy / reference counting"""
        self._sockets.add(socket)

    def _rm_socket(self, socket: Any) -> None:
        """Remove a socket for Context.destroy / reference counting"""
        # allow _sockets to be None in case of process teardown
        if getattr(self, "_sockets", None) is not None:
            self._sockets.discard(socket)

    def destroy(self, linger: Optional[float] = None) -> None:
        """Close all sockets associated with this context and then terminate
        the context.

        .. warning::

            destroy involves calling ``zmq_close()``, which is **NOT** threadsafe.
            If there are active sockets in other threads, this must not be called.

        Parameters
        ----------

        linger : int, optional
            If specified, set LINGER on sockets prior to closing them.
        """
        if self.closed:
            return

        sockets = self._sockets
        self._sockets = WeakSet()
        for s in sockets:
            if s and not s.closed:
                if linger is not None:
                    s.setsockopt(SocketOption.LINGER, linger)
                s.close()

        self.term()

    def socket(self: T, socket_type: int, **kwargs: Any) -> ST:
        """Create a Socket associated with this Context.

        Parameters
        ----------
        socket_type : int
            The socket type, which can be any of the 0MQ socket types:
            REQ, REP, PUB, SUB, PAIR, DEALER, ROUTER, PULL, PUSH, etc.

        kwargs:
            will be passed to the __init__ method of the socket class.
        """
        if self.closed:
            raise ZMQError(Errno.ENOTSUP)
        s: ST = self._socket_class(  # set PYTHONTRACEMALLOC=2 to get the calling frame
            self, socket_type, **kwargs
        )
        for opt, value in self.sockopts.items():
            try:
                s.setsockopt(opt, value)
            except ZMQError:
                # ignore ZMQErrors, which are likely for socket options
                # that do not apply to a particular socket type, e.g.
                # SUBSCRIBE for non-SUB sockets.
                pass
        self._add_socket(s)
        return s

    def setsockopt(self, opt: int, value: Any) -> None:
        """set default socket options for new sockets created by this Context

        .. versionadded:: 13.0
        """
        self.sockopts[opt] = value

    def getsockopt(self, opt: int) -> OptValT:
        """get default socket options for new sockets created by this Context

        .. versionadded:: 13.0
        """
        return self.sockopts[opt]

    def _set_attr_opt(self, name: str, opt: int, value: OptValT) -> None:
        """set default sockopts as attributes"""
        if name in ContextOption.__members__:
            return self.set(opt, value)
        elif name in SocketOption.__members__:
            self.sockopts[opt] = value
        else:
            raise AttributeError(f"No such context or socket option: {name}")

    def _get_attr_opt(self, name: str, opt: int) -> OptValT:
        """get default sockopts as attributes"""
        if name in ContextOption.__members__:
            return self.get(opt)
        else:
            if opt not in self.sockopts:
                raise AttributeError(name)
            else:
                return self.sockopts[opt]

    def __delattr__(self, key: str) -> None:
        """delete default sockopts as attributes"""
        key = key.upper()
        try:
            opt = getattr(SocketOption, key)
        except AttributeError:
            raise AttributeError(f"No such socket option: {key!r}")
        else:
            if opt not in self.sockopts:
                raise AttributeError(key)
            else:
                del self.sockopts[opt]


__all__ = ['Context']
