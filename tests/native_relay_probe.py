import asyncio, base64, hashlib, hmac, importlib.util, json, sys, tempfile, types
from pathlib import Path
from enum import Enum
from typing import Callable
from websockets.asyncio.server import serve
import argparse
parser = argparse.ArgumentParser(description='Test pinned Hermes transport with data-class facades, no model/OAuth calls')
parser.add_argument('--source-dir', type=Path, required=True, help='Directory containing official ws_transport.py, auth.py, descriptor.py and event.py')
SOURCE = parser.parse_args().source_dir
PLUGIN = Path(__file__).resolve().parents[1]
# Only data/registry facades are stubbed. Transport, auth, descriptor and MessageEvent
# execute unmodified code from the pinned official Hermes source.
for name in ['gateway','gateway.platforms','gateway.relay']:
    module = types.ModuleType(name); module.__path__ = []; sys.modules[name] = module
session = types.ModuleType('gateway.session'); session.SessionSource = types.SimpleNamespace;sys.modules[session.__name__]=session
config = types.ModuleType('gateway.config')
class Platform(str, Enum):
    RELAY='relay'; SLACK='slack'; SENLER='senler-io'
config.Platform=Platform;sys.modules[config.__name__]=config
contract = types.ModuleType('gateway.relay.transport');contract.InboundHandler=Callable;sys.modules[contract.__name__]=contract

def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path);mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod);return mod
for name,file in [('gateway.platforms.event','event.py'),('gateway.relay.auth','auth.py'),('gateway.relay.descriptor','descriptor.py'),('gateway.relay.ws_transport','ws_transport.py')]:
    load(name,SOURCE/file)
storemod=load('senler_probe_store',PLUGIN/'senler_relay_store.py')
Native=sys.modules['gateway.relay.ws_transport'].WebSocketRelayTransport
ID='123456789012345678901234';PROJECT='4e576fd4-f48d-4d49-af17-4fda06c82479';KEY='test-key-'+ 'x'*32
async def send_line(ws, value):
    await ws.send(json.dumps(value)+'\n')

DESCRIPTOR = {'contract_version':1,'platform':'senler-io','label':'Senler','max_message_length':16000,'supports_draft_streaming':False,'supports_edit':False,'supports_threads':False,'markdown_dialect':'plain','len_unit':'chars','supported_ops':['senler_start','senler_result']}

async def main():
    acknowledged=asyncio.Event();received=asyncio.Event();probes=[]
    with tempfile.TemporaryDirectory() as directory:
        store=storemod.RelayStore(directory)
        async def connector(ws):
            token=ws.request.headers['Authorization'].removeprefix('Bearer ')
            identity,exp,sig=base64.urlsafe_b64decode(token+'='*(-len(token)%4)).decode().split(':')
            assert identity==ID and hmac.compare_digest(sig,hmac.new(KEY.encode(),f'{ID}:{exp}'.encode(),hashlib.sha256).hexdigest())
            assert json.loads(await ws.recv())=={'type':'hello','platform':'senler-io','botId':ID}
            await send_line(ws, {'type':'descriptor','descriptor':DESCRIPTOR})
            async def inbound(body,buffer):
                await send_line(ws, {'type':'inbound','platform':'senler-io','botId':ID,'bufferId':buffer,'event':{'text':json.dumps(body),'message_type':'text','message_id':buffer,'source':{'platform':'senler-io','chat_id':ID,'scope_id':PROJECT}}})
            common={'webhook_id':ID,'project_id':PROJECT,'revision':0}
            for event in ['message_new','lead_created']:
                await inbound({**common,'kind':'probe','events':['message_new','lead_created'],'event_type':event},'probe:'+event)
                ack=json.loads(await ws.recv());assert ack=={'type':'inbound_ack','bufferId':'probe:'+event};probes.append(event)
            payload={'event_id':'event-1','event_type':'message_new','project_id':PROJECT,'data':{'content':'/reset'}}
            await inbound({**common,'kind':'event','payload':payload,'expires_at':'2099-01-01T00:00:00Z'},ID+':event-1')
            assert json.loads(await ws.recv())=={'type':'inbound_ack','bufferId':ID+':event-1'}
            assert store.status()[0]['pending']==1
            acknowledged.set()
            for op in ['senler_start','senler_result']:
                request=json.loads(await ws.recv());assert request['type']=='outbound' and request['action']['op']==op
                assert request['action']['attempt_id'] == store.next_event(store.subscriptions())['attempt_id']
                result = {'success': True, **({'start_status': 'granted'} if op == 'senler_start' else {})}
                await send_line(ws, {'type':'outbound_result','requestId':request['requestId'],'result':result})
            await ws.wait_closed()
        async with serve(connector,'127.0.0.1',0) as server:
            connection={'webhook_id':ID,'project_id':PROJECT,'gateway_id':ID,'gateway_secret':KEY,'relay_url':f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/hermes-relay/{ID}','events':['message_new','lead_created'],'revision':0}
            store.configure(connection,'Draft only')
            saved=store.subscriptions()[ID]
            transport=Native(connection['relay_url'],'senler-io',ID,gateway_id=ID,upgrade_secret=KEY)
            async def handoff(event):
                assert event.source.delivered_via_upstream_relay is True
                kind=store.accept(saved,json.loads(event.text))
                if kind=='event':received.set()
            transport.set_inbound_handler(handoff)
            try:
                await transport.connect()
                descriptor = await transport.handshake()
                assert descriptor.contract_version == 1 and descriptor.supports_op('senler_start') and descriptor.supports_op('senler_result')
                await asyncio.wait_for(acknowledged.wait(),10)
                assert received.is_set() and len(probes)==2
                attempt=store.next_event(store.subscriptions())['attempt_id']
                started=await transport.send_outbound({'op':'senler_start','event_id':'event-1','attempt_id':attempt})
                assert started['success'] is True and started['start_status'] == 'granted'
                result=await transport.send_outbound({'op':'senler_result','event_id':'event-1','attempt_id':attempt,'result':'Synthetic draft','failed':False})
                assert result['success'] is True
            finally:await transport.disconnect()
    await report_only()
    print('PASS: pinned native Hermes Relay transport: HMAC, descriptor, 2 event probes, durable receipt ACK, processing result, going_idle/ACK and report-only delivery without inbound ACKs. No model/OAuth/customer calls.')


async def report_only():
    frames = []
    async def connector(ws):
        assert json.loads(await ws.recv())['type'] == 'hello'
        await send_line(ws, {'type': 'descriptor', 'descriptor': DESCRIPTOR})
        # An arrival between hello and the idle ACK must remain on the server.
        await send_line(ws, {'type': 'inbound', 'bufferId': 'unacknowledged-event', 'event': {}})
        async for text in ws:
            frame = json.loads(text)
            frames.append(frame)
            if frame['type'] == 'going_idle':
                await send_line(ws, {'type': 'going_idle_ack'})
            elif frame['type'] == 'outbound':
                assert frame['action']['op'] == 'senler_result'
                await send_line(ws, {'type': 'outbound_result', 'requestId': frame['requestId'],
                                     'result': {'success': True}})

    async with serve(connector, '127.0.0.1', 0) as server:
        address = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/hermes-relay/{ID}'
        transport = Native(address, 'senler-io', ID, gateway_id=ID, upgrade_secret=KEY, reconnect=False)
        try:
            await transport.connect()
            await transport.handshake()
            assert await transport.go_idle(timeout_s=2)
            result = await asyncio.wait_for(transport.send_outbound({
                'op': 'senler_result', 'event_id': 'completed-event', 'attempt_id': 'a' * 32,
                'failed': True, 'result': 'Stopped by operator; saved partial report'}), 2)
            assert result['success']
        finally:
            await transport.disconnect()
    assert [frame['type'] for frame in frames] == ['going_idle', 'outbound'], frames

asyncio.run(main())
