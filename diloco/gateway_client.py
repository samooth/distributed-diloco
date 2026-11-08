"""WebSocket client for DiLoCo transport via the Node.js gateway."""
import asyncio
import json

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except Exception:
    AIOHTTP_AVAILABLE = False


class DiLoCoGatewayClient:
    def __init__(self, url, room, player_id, identity_key=None):
        self.url = url
        self.room = room
        self.player_id = player_id
        self.identity_key = identity_key
        self.ws = None
        self.session = None

    async def connect(self):
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError('aiohttp not available')
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url)
        await self.ws.send_json({'type': 'join', 'room': self.room, 'playerId': self.player_id,
                                 'identityKey': self.identity_key})
        return self.ws

    async def send(self, msg):
        if self.ws is None:
            await self.connect()
        await self.ws.send_json(msg)

    async def recv(self):
        if self.ws is None:
            await self.connect()
        msg = await self.ws.receive_json()
        return msg

    async def broadcast_checkpoint(self, round_num, checkpoint_url, checkpoint_hash):
        await self.send({
            'type': 'diloco-checkpoint',
            'room': self.room,
            'round': round_num,
            'url': checkpoint_url,
            'hash': checkpoint_hash,
        })

    async def relay_delta(self, round_num, delta_hash):
        await self.send({
            'type': 'diloco-delta',
            'room': self.room,
            'round': round_num,
            'hash': delta_hash,
        })

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()
        self.ws = None
        self.session = None


async def run_gateway_smoke(url, room='diloco', player_id='worker-0'):
    gc = GatewayClient(url, room, player_id)
    await gc.connect()
    await gc.relay_delta(1, 'abc123')
    await gc.broadcast_checkpoint(1, 'file:///tmp/ckpt', 'hash')
    await gc.close()
    print('gateway smoke ok')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='ws://127.0.0.1:8080')
    ap.add_argument('--room', default='diloco')
    ap.add_argument('--player-id', default='worker-0')
    args = ap.parse_args()
    asyncio.run(run_gateway_smoke(args.url, args.room, args.player_id))
PY