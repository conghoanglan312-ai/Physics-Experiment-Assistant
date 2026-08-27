from threading import Thread, Event
import time

class StreamTimeout:
    def __init__(
        self,
        initial_timeout=30,
        chunk_timeout=10
    ):

        self.initial_timeout = initial_timeout
        self.chunk_timeout = chunk_timeout

        self.last_event_time = None
        self.started = False

        self.stop_event = Event()

    def start(self):
        self.last_event_time = time.time()

    def update(self):
        self.last_event_time = time.time()
        self.started = True

    def isTimeout(self):
        if self.last_event_time is None: return False

        timeout = (self.chunk_timeout if self.started else self.initial_timeout)

        return (time.time() - self.last_event_time > timeout)

    def stop(self):
        self.stop_event.set()

    def stopped(self):
        return self.stop_event.is_set()