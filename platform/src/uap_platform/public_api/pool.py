"""Small bounded connection pool enforcing the public-reader transaction contract."""

from __future__ import annotations

import queue
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

import psycopg
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row


class PublicReaderPool:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        if min_size < 1 or max_size < min_size:
            raise ValueError("invalid public reader pool size")
        self._dsn = dsn
        self._max_size = max_size
        self._connections: queue.LifoQueue[Connection[dict[str, object]]] = queue.LifoQueue()
        self._created = 0
        self._lock = Lock()
        self._closed = False
        for _ in range(min_size):
            if not self._reserve_slot():
                raise RuntimeError("public reader pool could not reserve initial slot")
            self._connections.put(self._open_reserved())

    def _reserve_slot(self) -> bool:
        with self._lock:
            if self._closed or self._created >= self._max_size:
                return False
            self._created += 1
            return True

    def _release_slot(self) -> None:
        with self._lock:
            self._created -= 1

    def _open_reserved(self) -> Connection[dict[str, object]]:
        connection: Connection[dict[str, object]] | None = None
        try:
            connection = psycopg.connect(self._dsn, row_factory=dict_row)
            with connection.cursor() as cursor:
                cursor.execute("SELECT session_user")
                row = cursor.fetchone()
            connection.rollback()
            if row is None or row["session_user"] != "uap_public_reader":
                raise RuntimeError("public reader role verification failed")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            self._release_slot()
            raise

    def _checkout(self) -> Connection[dict[str, object]]:
        if self._closed:
            raise RuntimeError("public reader pool is closed")
        try:
            return self._connections.get_nowait()
        except queue.Empty:
            if self._reserve_slot():
                return self._open_reserved()
            return self._connections.get(timeout=10)

    @contextmanager
    def transaction(self) -> Iterator[Connection[dict[str, object]]]:
        connection = self._checkout()
        reusable = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield connection
            connection.commit()
        except Exception:
            reusable = False
            connection.rollback()
            raise
        finally:
            if connection.info.transaction_status != TransactionStatus.IDLE:
                connection.rollback()
                reusable = False
            if reusable and not connection.closed and not self._closed:
                self._connections.put(connection)
            else:
                connection.close()
                self._release_slot()

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                connection = self._connections.get_nowait()
            except queue.Empty:
                break
            connection.close()
            self._release_slot()
