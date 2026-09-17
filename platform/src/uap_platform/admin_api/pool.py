"""Bounded connection pool enforcing the Admin API transaction contract."""

from __future__ import annotations

import queue
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row


class AdminApiPool:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        if min_size < 1 or max_size < min_size:
            raise ValueError("invalid admin API pool size")
        self._dsn = dsn
        self._max_size = max_size
        self._connections: queue.LifoQueue[Connection[dict[str, object]]] = queue.LifoQueue()
        self._created = 0
        self._lock = Lock()
        self._closed = False
        for _ in range(min_size):
            if not self._reserve_slot():
                raise RuntimeError("admin API pool could not reserve initial slot")
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
            if row is None or row["session_user"] != "uap_api":
                raise RuntimeError("admin API role verification failed")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            self._release_slot()
            raise

    def _checkout(self) -> Connection[dict[str, object]]:
        if self._closed:
            raise RuntimeError("admin API pool is closed")
        try:
            return self._connections.get_nowait()
        except queue.Empty:
            if self._reserve_slot():
                return self._open_reserved()
            return self._connections.get(timeout=10)

    def _gucs_clear(self, connection: Connection[dict[str, object]]) -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('uap.principal_id', true) AS principal")
                principal = cursor.fetchone()
                cursor.execute("SELECT current_setting('uap.request_id', true) AS request")
                request = cursor.fetchone()
            principal_value = None if principal is None else principal["principal"]
            request_value = None if request is None else request["request"]
            return not principal_value and not request_value
        finally:
            # GUC inspection starts an implicit transaction; leave IDLE before reuse.
            if (
                not connection.closed
                and connection.info.transaction_status != TransactionStatus.IDLE
            ):
                connection.rollback()

    def _release(self, connection: Connection[dict[str, object]], reusable: bool) -> None:
        if connection.closed:
            self._release_slot()
            return
        if connection.info.transaction_status != TransactionStatus.IDLE:
            connection.rollback()
            reusable = False
        gucs_clear = False
        if reusable and not self._closed:
            gucs_clear = self._gucs_clear(connection)
        if connection.info.transaction_status != TransactionStatus.IDLE:
            connection.rollback()
            reusable = False
            gucs_clear = False
        if reusable and not self._closed and gucs_clear:
            self._connections.put(connection)
            return
        connection.close()
        self._release_slot()

    @contextmanager
    def lookup_transaction(self) -> Iterator[Connection[dict[str, object]]]:
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
            self._release(connection, reusable)

    @contextmanager
    def read_transaction(self, principal_id: UUID) -> Iterator[Connection[dict[str, object]]]:
        connection = self._checkout()
        reusable = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(
                    "SELECT set_config('uap.principal_id', %s, true)",
                    (str(principal_id),),
                )
            yield connection
            connection.commit()
        except Exception:
            reusable = False
            connection.rollback()
            raise
        finally:
            self._release(connection, reusable)

    @contextmanager
    def write_transaction(
        self, principal_id: UUID, request_id: UUID
    ) -> Iterator[Connection[dict[str, object]]]:
        connection = self._checkout()
        reusable = True
        committed = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('uap.principal_id', %s, true)",
                    (str(principal_id),),
                )
                cursor.execute(
                    "SELECT set_config('uap.request_id', %s, true)",
                    (str(request_id),),
                )
            yield connection
            connection.commit()
            committed = True
        except Exception:
            reusable = False
            connection.rollback()
            raise
        finally:
            self._release(connection, reusable and committed)
        if not committed:
            raise RuntimeError("admin write transaction did not commit")

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                connection = self._connections.get_nowait()
            except queue.Empty:
                break
            connection.close()
            self._release_slot()
