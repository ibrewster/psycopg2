"""Connection pooling for psycopg2

This module implements thread-safe (and not) connection pools.
"""
# psycopg/pool.py - pooling code for psycopg
#
# Copyright (C) 2003-2010 Federico Di Gregorio  <fog@debian.org>
#
# psycopg2 is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# In addition, as a special exception, the copyright holders give
# permission to link this program with the OpenSSL library (or with
# modified versions of OpenSSL that use the same license as OpenSSL),
# and distribute linked combinations including the two.
#
# You must obey the GNU Lesser General Public License in all respects for
# all of the code used other than OpenSSL.
#
# psycopg2 is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public
# License for more details.

import psycopg2
import psycopg2.extensions as _ext


class PoolError(psycopg2.Error):
    pass


class AbstractConnectionPool(object):
    """Generic key-based pooling code."""

    def __init__(self, minconn, maxconn, *args, **kwargs):
        """Initialize the connection pool.

        New 'minconn' connections are created immediately calling 'connfunc'
        with given parameters. The connection pool will support a maximum of
        about 'maxconn' connections.
        """
        self.minconn = int(minconn)
        self.maxconn = int(maxconn)
        self.closed = False

        self._args = args
        self._kwargs = kwargs

        self._pool = []
        self._used = {}
        self._rused = {}    # id(conn) -> key map
        self._keys = 0

        for i in range(self.minconn):
            self._connect()

    def _connect(self, key=None):
        """Create a new connection and assign it to 'key' if not None."""
        conn = psycopg2.connect(*self._args, **self._kwargs)
        if key is not None:
            self._used[key] = conn
            self._rused[id(conn)] = key
        else:
            self._pool.append(conn)
        return conn

    def _getkey(self):
        """Return a new unique key."""
        self._keys += 1
        return self._keys

    def _getconn(self, key=None):
        """Get a free connection and assign it to 'key' if not None."""
        if self.closed:
            raise PoolError("connection pool is closed")
        if key is None:
            key = self._getkey()

        if key in self._used:
            return self._used[key]

        if self._pool:
            self._used[key] = conn = self._pool.pop()
            self._rused[id(conn)] = key
            return conn
        else:
            if len(self._used) == self.maxconn:
                raise PoolError("connection pool exhausted")
            return self._connect(key)

    def _putconn(self, conn, key=None, close=False):
        """Put away a connection."""
        if self.closed:
            raise PoolError("connection pool is closed")
        if key is None:
            key = self._rused.get(id(conn))

        if not key:
            raise PoolError("trying to put unkeyed connection")

        if len(self._pool) < self.minconn and not close:
            # Return the connection into a consistent state before putting
            # it back into the pool
            if not conn.closed:
                status = conn.get_transaction_status()
                if status == _ext.TRANSACTION_STATUS_UNKNOWN:
                    # server connection lost
                    conn.close()
                elif status != _ext.TRANSACTION_STATUS_IDLE:
                    # connection in error or in transaction
                    conn.rollback()
                    self._pool.append(conn)
                else:
                    # regular idle connection
                    self._pool.append(conn)
            # If the connection is closed, we just discard it.
        else:
            conn.close()

        # here we check for the presence of key because it can happen that a
        # thread tries to put back a connection after a call to close
        if not self.closed or key in self._used:
            del self._used[key]
            del self._rused[id(conn)]

    def _closeall(self):
        """Close all connections.

        Note that this can lead to some code fail badly when trying to use
        an already closed connection. If you call .closeall() make sure
        your code can deal with it.
        """
        if self.closed:
            raise PoolError("connection pool is closed")
        for conn in self._pool + list(self._used.values()):
            try:
                conn.close()
            except:
                pass
        self.closed = True


class SimpleConnectionPool(AbstractConnectionPool):
    """A connection pool that can't be shared across different threads."""

    getconn = AbstractConnectionPool._getconn
    putconn = AbstractConnectionPool._putconn
    closeall = AbstractConnectionPool._closeall


class ThreadedConnectionPool(AbstractConnectionPool):
    """A connection pool that works with the threading module."""

    def __init__(self, minconn, maxconn, *args, **kwargs):
        """Initialize the threading lock."""
        import threading
        AbstractConnectionPool.__init__(
            self, minconn, maxconn, *args, **kwargs)
        self._lock = threading.Lock()

    def getconn(self, key=None):
        """Get a free connection and assign it to 'key' if not None."""
        self._lock.acquire()
        try:
            return self._getconn(key)
        finally:
            self._lock.release()

    def putconn(self, conn=None, key=None, close=False):
        """Put away an unused connection."""
        self._lock.acquire()
        try:
            self._putconn(conn, key, close)
        finally:
            self._lock.release()

    def closeall(self):
        """Close all connections (even the one currently in use.)"""
        self._lock.acquire()
        try:
            self._closeall()
        finally:
            self._lock.release()


class PersistentConnectionPool(AbstractConnectionPool):
    """A pool that assigns persistent connections to different threads.

    Note that this connection pool generates by itself the required keys
    using the current thread id.  This means that until a thread puts away
    a connection it will always get the same connection object by successive
    `!getconn()` calls. This also means that a thread can't use more than one
    single connection from the pool.
    """

    def __init__(self, minconn, maxconn, *args, **kwargs):
        """Initialize the threading lock."""
        import warnings
        warnings.warn("deprecated: use ZPsycopgDA.pool implementation",
            DeprecationWarning)

        import threading
        AbstractConnectionPool.__init__(
            self, minconn, maxconn, *args, **kwargs)
        self._lock = threading.Lock()

        # we we'll need the thread module, to determine thread ids, so we
        # import it here and copy it in an instance variable
        import thread as _thread  # work around for 2to3 bug - see ticket #348
        self.__thread = _thread

    def getconn(self):
        """Generate thread id and return a connection."""
        key = self.__thread.get_ident()
        self._lock.acquire()
        try:
            return self._getconn(key)
        finally:
            self._lock.release()

    def putconn(self, conn=None, close=False):
        """Put away an unused connection."""
        key = self.__thread.get_ident()
        self._lock.acquire()
        try:
            if not conn:
                conn = self._used[key]
            self._putconn(conn, key, close)
        finally:
            self._lock.release()

    def closeall(self):
        """Close all connections (even the one currently in use.)"""
        self._lock.acquire()
        try:
            self._closeall()
        finally:
            self._lock.release()

class CachingConnectionPool(AbstractConnectionPool):
    """A connection pool that works with the threading module and caches connections"""

    #---------------------------------------------------------------------------
    def __init__(self, minconn, maxconn, lifetime = 3600, *args, **kwargs):
        """Initialize the threading lock."""
        import threading
        from datetime import datetime, timedelta

        AbstractConnectionPool.__init__(
            self, minconn, maxconn, *args, **kwargs)
        self._lock = threading.Lock()
        self._lifetime = lifetime

        #Initalize function to get expiration time.
        self._expiration_time = lambda: datetime.now() + timedelta(seconds = lifetime)

        # A dictionary to hold connection ID's and when they should be removed from the pool
        # Keys are id(connection) and vlaues are expiration time
        # Storing the expiration time on the connection object itself might be
        # preferable, if possible.
        self._expirations = {}

    # Override the _putconn function to put the connection back into the pool even if we are over minconn, and to run the _prune command.
    #---------------------------------------------------------------------------
    def _putconn(self, conn, key=None, close=False):
        """Put away a connection."""
        if self.closed:
            raise PoolError("connection pool is closed")
        if key is None:
            key = self._rused.get(id(conn))

        if not key:
            raise PoolError("trying to put unkeyed connection")

        if len(self._pool) < self.maxconn and not close:
            # Return the connection into a consistent state before putting
            # it back into the pool
            if not conn.closed:
                status = conn.get_transaction_status()
                if status == _ext.TRANSACTION_STATUS_UNKNOWN:
                    # server connection lost
                    conn.close()
                    try:
                        del self._expirations[id(conn)]
                    except KeyError:
                        pass
                elif status != _ext.TRANSACTION_STATUS_IDLE:
                    # connection in error or in transaction
                    conn.rollback()
                    self._pool.append(conn)
                else:
                    # regular idle connection
                    self._pool.append(conn)
            # If the connection is closed, we just discard it.
            else:
                try:
                    del self._expirations[id(conn)]
                except KeyError:
                    pass
        else:
            conn.close()
            #remove this connection from the expiration list
            try:
                del self._expirations[id(conn)]
            except KeyError:
                pass  #not in the expiration list for some reason, can't remove it.

        # here we check for the presence of key because it can happen that a
        # thread tries to put back a connection after a call to close
        if not self.closed or key in self._used:
            del self._used[key]
            del self._rused[id(conn)]

        # remove any expired connections from the pool
        self._prune()

    #---------------------------------------------------------------------------
    def getconn(self, key=None):
        """Get a free connection and assign it to 'key' if not None."""
        self._lock.acquire()
        try:
            conn = self._getconn(key)
            #Add expiration time
            self._expirations[id(conn)] = self._expiration_time()
            return conn
        finally:
            self._lock.release()

    #---------------------------------------------------------------------------
    def putconn(self, conn=None, key=None, close=False):
        """Put away an unused connection."""
        self._lock.acquire()
        try:
            self._putconn(conn, key, close)
        finally:
            self._lock.release()

    #---------------------------------------------------------------------------
    def closeall(self):
        """Close all connections (even the one currently in use.)"""
        self._lock.acquire()
        try:
            self._closeall()
        finally:
            self._lock.release()

    #---------------------------------------------------------------------------
    def _prune(self):
        """Remove any expired connections from the connection pool."""
        from datetime import datetime
        junk_expirations = []
        for obj_id, exp_time in self._expirations.items():
            if exp_time > datetime.now():  # Not expired, move on.
                continue;

            del_idx = None
            #find index of connection in _pool. May not be there if connection is in use
            for index, conn in enumerate(self._pool):
                if id(conn) == obj_id:
                    conn.close()
                    junk_expirations.append(obj_id)
                    del_idx = index
                    break
            else:
                # See if this connection is used. If not, we need to remove
                # the reference to it.
                for conn in self._used.values():
                    if id(conn) == obj_id:
                        break  #found it, so just move on. Don't expire the
                    # connection till we are done with it.
                    else:
                        # This connection doesn't exist any more, so get rid
                        # of the reference to the expiration.
                        # Can't delete here because we'd be changing the item
                        # we are itterating over.
                        junk_expirations.append(obj_id)

            # Delete connection from pool if expired
            if del_idx is not None:
                del self._pool[del_idx]

        # Remove any junk expirations
        for item in junk_expirations:
            try:
                del self._expirations[item]
            except KeyError:
                pass  #expiration doesn't exist??

        # Make sure we still have at least minconn connections
        # Connections may be available or used
        total_conns = len(self._pool) + len(self._used)
        if  total_conns < self.minconn:
            for i in range(self.minconn - total_conns):
                self._connect()
