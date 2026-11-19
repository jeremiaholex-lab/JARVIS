import asyncio, json, sys
import websockets
async def click_btn():
    async with websockets.connect('ws://127.0.0.1:8010/ws') as ws:
        await ws.send(json.dumps({'op': 'settings', 'audioDebug': true}))
        for _ in range(200):
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            if isinstance(raw, (bytes, bytearray)):
                continue
            m = json.loads(raw)
            if m.get('op') == 'settings_ok':
                print('settings ok')
                break
asyncio.run(click_btn())
