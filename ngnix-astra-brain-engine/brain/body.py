import asyncio
import secrets


class BodyHub:
    def __init__(self):
        self.connections = {}
        self.pending = {}
        self.locks = {}

    def attach(self, session_id, socket, capabilities):
        if session_id in self.connections:
            raise ValueError('A body is already paired to this session')
        self.connections[session_id] = (socket, set(capabilities))
        self.locks[session_id] = asyncio.Lock()

    def detach(self, session_id):
        self.connections.pop(session_id, None)
        self.locks.pop(session_id, None)
        for key in list(self.pending):
            if key[0] == session_id:
                future = self.pending.pop(key)
                if not future.done():
                    future.set_result({'status': 'disconnected', 'executed': False})

    async def gesture(self, session_id, name):
        connection = self.connections.get(session_id)
        if not connection:
            return {'status': 'not_connected', 'executed': False}
        socket, capabilities = connection
        if name not in capabilities:
            return {'status': 'unsupported_gesture', 'executed': False}
        command_id = secrets.token_hex(8)
        key = (session_id, command_id)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            async with self.locks[session_id]:
                await socket.send_json({'type': 'gesture', 'command_id': command_id, 'name': name, 'synchronized': True})
            return await asyncio.wait_for(future, 2)
        except TimeoutError:
            return {'status': 'sent_without_acknowledgement', 'executed': False}
        finally:
            self.pending.pop(key, None)

    def acknowledge(self, session_id, command_id, status):
        future = self.pending.get((session_id, command_id))
        if future and not future.done():
            future.set_result({'status': status, 'executed': status == 'completed', 'reported_by': 'body'})

    async def disconnect(self, session_id):
        connection = self.connections.get(session_id)
        self.detach(session_id)
        if connection:
            try:
                await connection[0].close(code=1000)
            except RuntimeError:
                pass
