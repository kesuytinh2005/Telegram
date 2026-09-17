import asyncio

class BotState:
    def __init__(self):
        self.stop_event = asyncio.Event()
        self.pending_download = {}
        self.pending_getuid = {}
        self.sessions = {}

    def stop(self):
        self.stop_event.set()
        self.pending_download.clear()
        self.pending_getuid.clear()

    def start(self):
        self.stop_event.clear()

state = BotState()
