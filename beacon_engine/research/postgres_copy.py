"""Administrator COPY with bounded libpq output buffering on every platform."""

def bounded_copy_writer(cursor):
    # Import optional PostgreSQL support only for an explicitly selected operation.
    from psycopg.copy import LibpqWriter
    from psycopg.generators import copy_to

    class BoundedCopyWriter(LibpqWriter):
        def write(self, data):
            # Linux's default writer can accumulate a table in libpq while a slow
            # server catches up. Flush each bounded chunk before producing more.
            view = memoryview(data)
            for offset in range(0, len(view), 128 * 1024):
                self.connection.wait(copy_to(self.connection.pgconn,
                    view[offset:offset + 128 * 1024], flush=True))

    return BoundedCopyWriter(cursor)
