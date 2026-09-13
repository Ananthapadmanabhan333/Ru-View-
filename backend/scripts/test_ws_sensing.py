import asyncio
import json
import websockets

async def test_ws():
    url = "ws://localhost:8765/ws/sensing"
    print(f"Connecting to {url}...")
    async with websockets.connect(url) as ws:
        for i in range(3):
            msg = await ws.recv()
            data = json.loads(msg)
            print(f"[{i+1}] Received {data.get('type')}: tick={data.get('tick')}, source={data.get('source')}, vitals={data.get('vital_signs')}")
    print("SUCCESS: WebSocket streaming verified!")

if __name__ == "__main__":
    asyncio.run(test_ws())
